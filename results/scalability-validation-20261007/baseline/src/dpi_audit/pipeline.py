"""Audit one project end-to-end and write its result folder atomically.

Called inside an isolated worker subprocess (see batch.py) so that a crash, hang or
browser failure in one project cannot stop the batch.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import traceback
import urllib.request
from pathlib import Path

from . import __version__
from .acquisition import acquire_local, acquire_remote, remote_head
from .aggregate import Aggregator
from .codebook import load_codebook
from .config import config_hash
from .inputs import NON_WEB_TYPES, Project, project_ui_applicability
from .metadata import get_record
from .observations import Sink
from .util import atomic_write_json, package_root, replace_dir, sha256_obj, sha256_text, utc_now, write_jsonl

DETECTOR_MODULES = ["static/analyze.py", "static/i18n.py", "static/css.py", "static/parse.py", "static/scanner.py",
                    "runtime/rules.py", "runtime/checks.py", "runtime/lang_switch.py", "runtime/discovery.py",
                    "runtime/runner.py", "runtime/net.py", "runtime/browser.py", "runtime/js/lib.js", "runtime/js/init.js",
                    "aggregate.py", "observations.py", "lang.py", "inputs.py", "acquisition.py", "metadata.py"]


def code_hash() -> str:
    root = package_root()
    return sha256_text("".join(sha256_text((root / m).read_text("utf-8")) for m in DETECTOR_MODULES))[:16]


def source_revisions(project: Project, cfg: dict) -> dict[str, str]:
    """Cheap revision keys used for resume invalidation (no clone)."""
    revs = {}
    for i in project.sources:
        loc = i.location
        if loc.kind == "local_path":
            c = acquire_local(loc.path)
            revs[i.interface_id] = c.revision_key or "unavailable"
        elif cfg["acquisition"].get("allow_remote_clone", True):
            revs[i.interface_id] = remote_head(loc.url, loc.ref) or "unresolved"
        else:
            revs[i.interface_id] = "clone_disabled"
    return revs


def fingerprint(project: Project, meta: dict, cfg: dict, codebook, mode: str, revisions: dict) -> str:
    return sha256_obj({"entry": project.entry_fingerprint(), "meta": meta, "config": config_hash(cfg),
                       "codebook": codebook.content_hash, "code": code_hash(), "mode": mode, "revisions": revisions,
                       "version": __version__})[:24]


def _deployment_relationship(meta: dict, checkouts: dict, webs: list, cfg: dict) -> dict:
    rel = dict(meta.get("deployment_repository_relationship") or {"status": "unknown"})
    rel.setdefault("status", "unknown")
    rel["tool_check"] = "not attempted"
    commits = [c.commit for c in checkouts.values() if c and c.commit]
    if not commits or not webs:
        rel["tool_check"] = "not applicable (needs both a repository commit and a website)"
        return rel
    ua = "Mozilla/5.0 " + cfg["runtime"]["user_agent_suffix"]
    for w in webs:
        try:
            req = urllib.request.Request(w.location.url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read(2_000_000).decode("utf-8", "replace")
        except Exception as exc:
            rel["tool_check"] = f"deployment HTML not retrievable ({type(exc).__name__})"
            continue
        for c in commits:
            if re.search(re.escape(c[:12]), html):
                rel.update(status="verified", tool_check=f"commit {c[:12]} found in deployed HTML of {w.location.url}", status_source="tool")
                return rel
        rel["tool_check"] = "no repository commit identifier found in deployed HTML (relationship remains as supplied/unknown)"
    return rel


def audit_project(project: Project, meta_all: dict, cfg: dict, codebook_path: str | None, out_dir: Path,
                  mode: str, fp: str, revisions: dict, log=print) -> dict:
    cb = load_codebook(codebook_path)
    meta = get_record(meta_all, project.project_id)
    projects_dir = out_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{project.project_id}-", dir=str(projects_dir)))
    evidence = tmp / "evidence"
    cache = Path(cfg["acquisition"]["cache_dir"]).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    sink = Sink(project.project_id)
    started = utc_now()
    iface_infos: list[dict] = []
    runtime_results: dict = {}
    checkouts: dict = {}
    tool_versions = {"dpi_audit": __version__, "code_hash": code_hash()}
    status = "complete"
    errors: list[str] = []
    ui_status = project_ui_applicability(project)
    try:
        # ---------------------------------------------------------- sources
        for i in project.sources:
            info = {"interface_id": i.interface_id, "kind": "source", "interface_type": "source_repository",
                    "location": i.location.to_dict()}
            loc = i.location
            co = acquire_local(loc.path) if loc.kind == "local_path" else acquire_remote(loc.url, loc.ref, loc.subdir, cfg, cache)
            checkouts[i.interface_id] = co
            info.update(status=co.status, reason=co.error, commit=co.commit, dirty=co.dirty, revision_key=co.revision_key,
                        acquisition=co.source, size_mb=co.size_mb)
            if co.status == "ok" and mode in ("static", "hybrid"):
                from .static.analyze import StaticDetectors, build_index, static_coverage
                root = co.path
                if loc.subdir:
                    root = str(Path(co.path) / loc.subdir)
                    if not Path(root).is_dir():
                        info.update(status="failed", reason=f"subdirectory {loc.subdir!r} not found in checkout")
                        iface_infos.append(info)
                        continue
                idx = build_index(root, cfg)
                det = StaticDetectors(idx, project.project_id, i.interface_id, cfg, meta, co.commit)
                sink.extend(det.run_all())
                cov = static_coverage(idx)
                info["static_coverage"] = cov
                info["ui_source_detected"] = cov["ui_source_detected"]
                if cov["file_budget_truncated"]:
                    info["coverage_note"] = f"static file budget reached: {cov['excluded_by_reason'].get('file_budget_exhausted', 0)} first-party files not inspected"
                if not cov["ui_source_detected"]:
                    info["coverage_note"] = (info.get("coverage_note", "") + "; " if info.get("coverage_note") else "") + \
                        "no web UI templates/components/styles found in inspected scope (UI applicability not inferred)"
            elif co.status == "ok":
                info["static_coverage"] = {"not_run": f"mode={mode}"}
            iface_infos.append(info)
        # ---------------------------------------------------------- web interfaces
        webs = [w for w in project.webs]
        run_webs = [w for w in webs if w.interface_type not in NON_WEB_TYPES]
        if run_webs and mode in ("runtime", "hybrid"):
            from .runtime.browser import BrowserSession
            from .runtime.net import HostRateLimiter
            from .runtime.runner import InterfaceRunner
            limiter = HostRateLimiter(out_dir / ".ratelimit", float(cfg["runtime"]["per_host_min_delay_s"]))
            with BrowserSession(cfg, limiter, log) as sess:
                tool_versions.update(sess.versions)
                for w in run_webs:
                    sess.blocked.clear()
                    runner = InterfaceRunner(sess, project.project_id, w, meta, cfg, cb, evidence, log)
                    res = runner.run()
                    sink.extend(runner.obs)
                    runtime_results[w.interface_id] = res
        for w in webs:
            info = {"interface_id": w.interface_id, "kind": "web", "interface_type": w.interface_type, "location": w.location.to_dict()}
            if w.interface_type in NON_WEB_TYPES:
                info.update(status="not_applicable", reason=f"declared {w.interface_type}: no browser UI checks")
            elif mode == "static":
                info.update(status="not_run", reason="mode=static")
            else:
                r = runtime_results.get(w.interface_id, {})
                info.update(status=r.get("status", "failed"), reason=r.get("reason", ""), home=r.get("home"),
                            coverage=r.get("coverage"), states_run=r.get("states_run"), states_skipped=r.get("states_skipped"),
                            errors=r.get("errors"), elapsed_s=r.get("elapsed_s"))
                cov = r.get("coverage") or {}
                notes = []
                if cov:
                    notes.append(f"{cov.get('pages_rendered', 0)} of max {cov.get('max_pages')} pages rendered; "
                                 f"{cov.get('pages_skipped', 0)} skipped, {cov.get('pages_blocked', 0)} blocked, {cov.get('pages_failed', 0)} failed")
                if r.get("states_skipped"):
                    notes.append("states skipped: " + ", ".join(f"{k} ({v})" for k, v in r["states_skipped"].items()))
                info["coverage_note"] = "; ".join(notes)
            iface_infos.append(info)
        if mode == "static" and webs:
            for w in webs:
                if w.interface_type not in NON_WEB_TYPES:
                    pass
        relationship = _deployment_relationship(meta, checkouts, run_webs, cfg) if mode != "static" else dict(meta.get("deployment_repository_relationship") or {})
        if any(i.get("status") in ("failed",) for i in iface_infos):
            status = "complete_with_errors"
    except Exception as exc:
        status = "failed"
        errors.append(traceback.format_exc(limit=6))
        relationship = dict(meta.get("deployment_repository_relationship") or {})
    # ---------------------------------------------------------- aggregate + write
    run_ctx = {"codebook_version": cb.version, "rule_version": cb.content_hash[:12], "config_hash": config_hash(cfg)[:12],
               "code_hash": code_hash(), "audit_timestamp": started}
    agg = Aggregator(cb, cfg, run_ctx)
    rows = []
    obs = sink.all()
    for info in iface_infos:
        rows += agg.interface_rows(project, info, [o for o in obs if o.interface_id == info["interface_id"]], mode)
    rows += agg.project_rows(project, ui_status, iface_infos, obs, mode)
    obs_run_ctx = {"codebook_version": cb.version, "rule_version": cb.content_hash[:12], "config_hash": config_hash(cfg)[:12],
                   "source_commit": ";".join(f"{k.split('::')[-1]}={v.commit}" for k, v in checkouts.items() if v and v.commit) or None,
                   "tool_versions": {k: v for k, v in tool_versions.items() if k in ("browser_version", "playwright", "axe_core", "dpi_audit", "code_hash")}}
    write_jsonl(tmp / "observations.jsonl", (o.to_dict(obs_run_ctx) for o in obs))
    pages = []
    for iid, r in runtime_results.items():
        for p in r.get("pages", []):
            pages.append({"interface_id": iid, **{k: v for k, v in p.items() if k != "load"}})
    inventory = {
        "project_id": project.project_id, "id_source": project.id_source, "lines": project.lines, "raw_lines": project.raw_lines,
        "declared_types": project.declared_types, "warnings": project.warnings, "ui_applicability": ui_status[0],
        "ui_applicability_reason": ui_status[1], "metadata": meta, "deployment_repository_relationship": relationship,
        "interfaces": iface_infos, "status": status, "errors": errors, "mode": mode, "started": started, "finished": utc_now(),
        "tool_versions": tool_versions, "fingerprint": fp, "revisions": revisions, **run_ctx,
    }
    atomic_write_json(tmp / "inventory.json", inventory)
    atomic_write_json(tmp / "features.json", rows)
    atomic_write_json(tmp / "pages.json", pages)
    atomic_write_json(tmp / "checkpoint.json", {"project_id": project.project_id, "status": status, "fingerprint": fp,
                                                "finished": utc_now(), "n_observations": len(obs), "n_rows": len(rows)})
    final = projects_dir / project.project_id
    replace_dir(tmp, final)
    return {"project_id": project.project_id, "status": status, "observations": len(obs), "errors": errors[:1]}


def write_failure(out_dir: Path, project: Project, fp: str, reason: str, meta_all: dict, cfg: dict, codebook_path, mode: str) -> None:
    """Record a project whose worker crashed or timed out, without observations."""
    cb = load_codebook(codebook_path)
    meta = get_record(meta_all, project.project_id)
    d = out_dir / "projects" / project.project_id
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{project.project_id}-", dir=str(out_dir / "projects")))
    run_ctx = {"codebook_version": cb.version, "rule_version": cb.content_hash[:12], "config_hash": config_hash(cfg)[:12],
               "code_hash": code_hash(), "audit_timestamp": utc_now()}
    infos = [{"interface_id": i.interface_id, "kind": i.kind, "interface_type": i.interface_type, "location": i.location.to_dict(),
              "status": "failed", "reason": reason} for i in project.interfaces]
    agg = Aggregator(cb, cfg, run_ctx)
    rows = []
    for info in infos:
        rows += agg.interface_rows(project, info, [], mode)
    rows += agg.project_rows(project, project_ui_applicability(project), infos, [], mode)
    for r in rows:
        if r["availability"] == "unknown":
            r["coverage_limits"] = (r["coverage_limits"] + "; " if r["coverage_limits"] else "") + f"project worker failed: {reason}"
    inventory = {"project_id": project.project_id, "id_source": project.id_source, "lines": project.lines, "raw_lines": project.raw_lines,
                 "declared_types": project.declared_types, "warnings": project.warnings,
                 "ui_applicability": project_ui_applicability(project)[0], "ui_applicability_reason": project_ui_applicability(project)[1],
                 "metadata": meta, "deployment_repository_relationship": meta.get("deployment_repository_relationship"),
                 "interfaces": infos, "status": "failed", "errors": [reason], "mode": mode, "fingerprint": fp, **run_ctx}
    atomic_write_json(tmp / "inventory.json", inventory)
    atomic_write_json(tmp / "features.json", rows)
    atomic_write_json(tmp / "pages.json", [])
    write_jsonl(tmp / "observations.jsonl", [])
    atomic_write_json(tmp / "checkpoint.json", {"project_id": project.project_id, "status": "failed", "fingerprint": fp,
                                                "finished": utc_now(), "reason": reason})
    replace_dir(tmp, d)
