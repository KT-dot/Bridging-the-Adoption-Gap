"""Small shared helpers: hashing, canonical JSON, atomic writes, slugs, time."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def short_hash(text: str, n: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


_SLUG_RX = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 48) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_RX.sub("-", norm).strip("-")
    return (slug[:max_len].strip("-")) or "project"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, obj: Any, indent: int | None = 1) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=False, default=str) + "\n")


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    n = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            n += 1
    os.replace(tmp, path)
    return n


def iter_jsonl(path: Path) -> Iterable[dict]:
    """Read one record at a time, including files too large to hold in memory."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_jsonl(path: Path) -> list[dict]:
    return list(iter_jsonl(path))


def replace_dir(src: Path, dst: Path) -> None:
    """Atomically-ish replace directory dst with src (both on the same filesystem)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        trash = dst.with_name(f".old-{dst.name}-{os.getpid()}")
        if trash.exists():
            shutil.rmtree(trash, ignore_errors=True)
        os.replace(dst, trash)
        os.replace(src, dst)
        shutil.rmtree(trash, ignore_errors=True)
    else:
        os.replace(src, dst)


def truncate(text: str | None, n: int) -> str:
    if text is None:
        return ""
    text = str(text)
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def package_root() -> Path:
    return Path(__file__).resolve().parent


def repo_root() -> Path:
    """Project checkout root (where config/, codebook/, node_modules/ live)."""
    env = os.environ.get("DPI_AUDIT_HOME")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]
