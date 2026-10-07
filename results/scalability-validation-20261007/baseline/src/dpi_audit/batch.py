"""Batch orchestration: validation, resume decisions, worker isolation, run manifest.

Each project runs in its own subprocess (``python -m dpi_audit.worker``) with a hard
timeout. Results are written to ``<out>/projects/<project_id>/`` atomically, so a resumed
run can never produce duplicate rows: aggregate tables are always rebuilt from the
per-project folders by ``report``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__
from .codebook import frozen_state, load_codebook, validate_codebook
from .config import config_hash, semantic_config
from .inputs import parse_list
from .metadata import check_join, get_record, load_metadata
from .pipeline import code_hash, fingerprint, source_revisions, write_failure
from .util import atomic_write_json, sha256_file, utc_now


class BatchError(RuntimeError):
    pass


def _log(out_dir: Path, msg: str) -> None:
    line = f"{utc_now()} {msg}"
    print(line, flush=True)
    (out_dir / "logs").mkdir(parents=True, exist_ok=True)
    with open(out_dir / "logs" / "audit.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run_batch(input_path: str, out_dir: str, cfg: dict, cfg_path: str | None, overrides: dict, metadata_path: str | None,
              codebook_path: str | None, limit: int | None, resume: bool, fresh: bool) -> dict:
    out = Path(out_dir)
    mode = cfg["run"]["mode"]
    parsed = parse_list(input_path)
    if parsed.errors:
        msg = "\n".join(f"  line {e['line']}: {e['error']}" for e in parsed.errors)
        raise BatchError(f"{input_path}: {len(parsed.errors)} invalid line(s); fix them (see `dpi-audit validate`):\n{msg}")
    meta_all, meta_warn = load_metadata(metadata_path)
    meta_warn += check_join(meta_all, [p.project_id for p in parsed.projects])
    cb = load_codebook(codebook_path)
    errs = validate_codebook(cb)
    if errs:
        raise BatchError("codebook invalid:\n" + "\n".join(errs))
    fz = frozen_state(cb)
    if out.exists() and (out / "projects").exists() and any((out / "projects").iterdir()):
        if fresh:
            shutil.rmtree(out)
        elif not resume:
            raise BatchError(f"{out} already contains results; use --resume to continue or --fresh to discard them")
    out.mkdir(parents=True, exist_ok=True)
    (out / "projects").mkdir(exist_ok=True)
    projects = parsed.projects[: limit] if limit else parsed.projects
    manifest = {
        "tool": "dpi-audit", "version": __version__, "code_hash": code_hash(), "started": utc_now(), "mode": mode,
        "input": {"path": str(Path(input_path).resolve()), "sha256": sha256_file(Path(input_path)), "lines": parsed.n_lines,
                  "entries": parsed.n_entries, "projects_total": len(parsed.projects), "duplicates": parsed.duplicates,
                  "warnings": parsed.warnings},
        "limit": limit, "projects": [p.project_id for p in projects],
        "metadata": {"path": str(Path(metadata_path).resolve()) if metadata_path else None,
                     "sha256": sha256_file(Path(metadata_path)) if metadata_path else None, "warnings": meta_warn},
        "config": {"path": cfg_path, "overrides": overrides, "semantic_hash": config_hash(cfg)},
        "codebook": {"path": str(cb.path), "version": cb.version, "status": cb.status, "content_sha256": cb.content_hash, "frozen": fz},
    }
    atomic_write_json(out / "config.resolved.json", semantic_config(cfg) | {"run": cfg["run"]})
    if not fz["frozen"]:
        _log(out, f"WARNING codebook {cb.version} is not frozen ({fz['reason']}); label outputs as pilot results")
    for w in meta_warn:
        _log(out, f"WARNING {w}")
    plan, skipped = [], []
    for p in projects:
        revs = source_revisions(p, cfg) if mode != "runtime" else {}
        fp = fingerprint(p, get_record(meta_all, p.project_id), cfg, cb, mode, revs)
        ck_path = out / "projects" / p.project_id / "checkpoint.json"
        if resume and ck_path.exists():
            ck = json.loads(ck_path.read_text("utf-8"))
            if ck.get("fingerprint") == fp and ck.get("status") in ("complete", "complete_with_errors"):
                skipped.append({"project_id": p.project_id, "reason": "checkpoint up to date"})
                continue
            if ck.get("fingerprint") == fp and ck.get("status") == "failed" and not cfg["run"].get("retry_failed_on_resume", True):
                skipped.append({"project_id": p.project_id, "reason": "failed previously; retry disabled"})
                continue
            if ck.get("fingerprint") != fp:
                _log(out, f"{p.project_id}: stale checkpoint (inputs, source revision, rules, code or config changed) -> re-audit")
        plan.append((p, fp, revs))
    manifest["resume"] = {"requested": resume, "skipped_up_to_date": skipped, "scheduled": [p.project_id for p, _, _ in plan]}
    atomic_write_json(out / "run_manifest.json", manifest)
    _log(out, f"mode={mode} projects={len(projects)} scheduled={len(plan)} skipped={len(skipped)} workers={cfg['run']['workers']}")
    worker_args = {"input": str(Path(input_path).resolve()), "out": str(out.resolve()), "config": cfg_path,
                   "overrides": overrides, "metadata": str(Path(metadata_path).resolve()) if metadata_path else None,
                   "codebook": str(Path(codebook_path).resolve()) if codebook_path else None}
    results = []
    timeout = int(cfg["run"]["project_timeout_s"])

    def one(item):
        p, fp, revs = item
        log_path = out / "logs" / f"{p.project_id}.log"
        args = dict(worker_args, project_id=p.project_id, fingerprint=fp, revisions=revs)
        t0 = time.time()
        launched = utc_now()
        with open(log_path, "w", encoding="utf-8") as lf:
            try:
                proc = subprocess.run([sys.executable, "-m", "dpi_audit.worker", json.dumps(args)], stdout=lf, stderr=subprocess.STDOUT,
                                      timeout=timeout, cwd=os.getcwd(), env=_worker_env())
                rc = proc.returncode
                reason = "" if rc == 0 else f"worker exited with code {rc} (see logs/{p.project_id}.log)"
            except subprocess.TimeoutExpired:
                rc, reason = -1, f"worker exceeded project_timeout_s={timeout}"
        ck_file = out / "projects" / p.project_id / "checkpoint.json"
        ck0 = json.loads(ck_file.read_text("utf-8")) if ck_file.exists() else {}
        wrote = ck0.get("fingerprint") == fp and str(ck0.get("finished", "")) >= launched
        if rc != 0 and not wrote:
            write_failure(out, p, fp, reason, meta_all, cfg, codebook_path, mode)
        ck = json.loads((out / "projects" / p.project_id / "checkpoint.json").read_text("utf-8"))
        return {"project_id": p.project_id, "status": ck.get("status"), "seconds": round(time.time() - t0, 1), "reason": reason}

    with ThreadPoolExecutor(max_workers=max(1, int(cfg["run"]["workers"]))) as ex:
        futs = {ex.submit(one, item): item[0].project_id for item in plan}
        for fut in as_completed(futs):
            pid = futs[fut]
            try:
                r = fut.result()
            except Exception as exc:  # orchestration error for this project only
                r = {"project_id": pid, "status": "failed", "reason": f"orchestrator: {type(exc).__name__}: {exc}"}
            results.append(r)
            _log(out, f"{r['project_id']}: {r['status']} {r.get('seconds', '')}s {r.get('reason', '')}")
    manifest["finished"] = utc_now()
    manifest["results"] = sorted(results, key=lambda r: r["project_id"])
    atomic_write_json(out / "run_manifest.json", manifest)
    return manifest


def _worker_env() -> dict:
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env
