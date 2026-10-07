"""Acquire source checkouts read-only and identify their revision.

Remote repositories are shallow-cloned into a content-addressed cache
(``<cache>/repos/<url-hash>/<commit>``) with a scrubbed git environment (no user
gitconfig, credential helpers, hooks, LFS smudging or prompts) and then made read-only.
Local directories are read in place and never written to; git metadata is read with
``--no-optional-locks`` so not even the index is refreshed.
Repository content is treated as data: nothing from the checkout is executed.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .util import sha256_text, short_hash


@dataclass
class Checkout:
    status: str                  # ok | failed | skipped
    path: str | None = None
    commit: str | None = None
    ref: str | None = None
    dirty: bool | None = None
    revision_key: str | None = None   # what resume compares (commit or content hash)
    source: str = ""             # remote_clone | cache | local_git | local_dir
    error: str = ""
    size_mb: float | None = None
    subdir: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")}
    env.update({
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_LFS_SKIP_SMUDGE": "1", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
        "HOME": tempfile.gettempdir(), "GIT_ALLOW_PROTOCOL": "https:http:file",
    })
    return env


def _git(args: list[str], cwd: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    base = ["git", "-c", "credential.helper=", "-c", "core.hooksPath=" + os.devnull,
            "-c", "protocol.file.allow=always", "-c", "advice.detachedHead=false",
            "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false"]
    return subprocess.run(base + args, cwd=cwd, env=_git_env(), capture_output=True, text=True, timeout=timeout)


def remote_head(url: str, ref: str | None = None, timeout: int = 60) -> str | None:
    try:
        r = _git(["ls-remote", url, ref or "HEAD"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        if ref:
            r2 = _git(["ls-remote", url, f"refs/heads/{ref}", f"refs/tags/{ref}"], timeout=timeout)
            if r2.returncode == 0 and r2.stdout.strip():
                return r2.stdout.split()[0]
        return None
    return r.stdout.split()[0]


def _make_readonly(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in files:
            p = os.path.join(root, name)
            try:
                if not os.path.islink(p):
                    os.chmod(p, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            except OSError:
                pass
        for name in dirs:
            p = os.path.join(root, name)
            try:
                os.chmod(p, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            except OSError:
                pass


def _make_writable(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            p = os.path.join(root, name)
            try:
                if not os.path.islink(p):
                    os.chmod(p, os.stat(p).st_mode | stat.S_IWUSR)
            except OSError:
                pass
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
    except OSError:
        pass


def _du_mb(path: Path) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return round(total / 1e6, 1)


def acquire_remote(url: str, ref: str | None, subdir: str | None, cfg: dict, cache_dir: Path) -> Checkout:
    acq = cfg["acquisition"]
    if not acq.get("allow_remote_clone", True):
        return Checkout("skipped", error="remote cloning disabled (acquisition.allow_remote_clone=false)")
    head = remote_head(url, ref)
    if not head:
        return Checkout("failed", error=f"git ls-remote could not resolve {ref or 'HEAD'} (repository unreachable, private, or ref missing)")
    dest = cache_dir / "repos" / short_hash(url.lower(), 16) / head
    if dest.exists() and (dest / ".dpi-audit-complete").exists():
        return Checkout("ok", str(dest), head, ref, False, head, "cache", size_mb=_du_mb(dest), subdir=subdir)
    tmp = Path(tempfile.mkdtemp(prefix="clone-", dir=str(cache_dir)))
    try:
        args = ["clone", "--depth", str(acq.get("clone_depth", 1)), "--no-tags", "--single-branch", "--no-recurse-submodules"]
        if ref:
            args += ["--branch", ref]
        r = _git(args + [url, str(tmp / "src")], timeout=int(acq.get("clone_timeout_s", 300)))
        if r.returncode != 0:
            return Checkout("failed", error=f"git clone failed: {r.stderr.strip()[-300:]}")
        got = _git(["rev-parse", "HEAD"], cwd=str(tmp / "src")).stdout.strip()
        if ref is None and got != head:
            head = got  # branch moved between ls-remote and clone
        dest = cache_dir / "repos" / short_hash(url.lower(), 16) / got
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            _make_writable(dest)
            shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp / "src", dest)
        (dest / ".dpi-audit-complete").write_text(url + "\n")
        size = _du_mb(dest)
        _make_readonly(dest)
        return Checkout("ok", str(dest), got, ref, False, got, "remote_clone", size_mb=size, subdir=subdir)
    except subprocess.TimeoutExpired:
        return Checkout("failed", error="git clone timed out")
    finally:
        if tmp.exists():
            _make_writable(tmp)
            shutil.rmtree(tmp, ignore_errors=True)


def content_fingerprint(path: Path, max_files: int = 20000) -> str:
    """Cheap content fingerprint of a non-git directory (paths, sizes, mtimes)."""
    parts = []
    n = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "node_modules"))
        for f in sorted(files):
            p = os.path.join(root, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            parts.append(f"{os.path.relpath(p, path)}:{st.st_size}:{int(st.st_mtime)}")
            n += 1
            if n >= max_files:
                break
    return "dir:" + sha256_text("\n".join(parts))[:32]


def acquire_local(path: str) -> Checkout:
    p = Path(path)
    if not p.is_dir():
        return Checkout("failed", error=f"local path not found: {path}")
    commit = dirty = None
    try:
        r = _git(["--no-optional-locks", "rev-parse", "HEAD"], cwd=str(p), timeout=30)
        if r.returncode == 0:
            commit = r.stdout.strip()
            st = _git(["--no-optional-locks", "status", "--porcelain", "--untracked-files=no"], cwd=str(p), timeout=60)
            dirty = bool(st.stdout.strip()) if st.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    top = None
    if commit:
        t = _git(["--no-optional-locks", "rev-parse", "--show-toplevel"], cwd=str(p), timeout=30)
        top = t.stdout.strip() if t.returncode == 0 else None
    if commit and top and Path(top).resolve() == p.resolve():
        key = commit + (":" + content_fingerprint(p) if dirty else "")
        return Checkout("ok", str(p), commit, None, dirty, key, "local_git", size_mb=_du_mb(p))
    # not a repository root (or not git): fingerprint the directory contents
    return Checkout("ok", str(p), commit if top else None, None, None, content_fingerprint(p), "local_dir", size_mb=_du_mb(p))
