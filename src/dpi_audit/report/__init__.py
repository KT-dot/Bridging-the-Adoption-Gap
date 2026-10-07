"""Build all study outputs from per-project result folders (idempotent; safe to rerun)."""
from __future__ import annotations

import csv
import json
import shutil
import statistics
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..codebook import frozen_state, load_codebook, wcag_refs
from ..review import build_queue, write_queue
from ..util import atomic_write_json, atomic_write_text, iter_jsonl, repo_root, utc_now

TEMPLATES = Path(__file__).parent / "templates"
AV_STATES = ("present", "not_found_in_scope", "unknown", "not_applicable")
VER_STATES = ("pass", "partial", "fail", "not_tested")


def _write_csv(path: Path, rows: Iterable[dict], fields: list[str] | None = None) -> int:
    """Write single-pass iterables without retaining every row in memory.

    When columns are discovered dynamically, spool cells to disk first so late
    columns and their original ordering remain identical to the previous writer.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    count = 0
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", dir=path.parent) as spool:
            if fields is None:
                fields, seen = [], set()
                for row in rows:
                    for key in row:
                        if key not in seen:
                            fields.append(key)
                            seen.add(key)
                    spool.write(json.dumps({k: _cell(v) for k, v in row.items()}, ensure_ascii=False) + "\n")
                spool.seek(0)
                rows = (json.loads(line) for line in spool)
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for row in rows:
                    w.writerow({k: _cell(row.get(k)) for k in fields})
                    count += 1
        tmp.replace(path)
        return count
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _cell(v):
    if isinstance(v, (list, tuple)):
        return ";".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    if isinstance(v, bool):
        return "true" if v else "false"
    return "" if v is None else v


def load_results(results: Path, allow_mixed: bool = False) -> dict:
    manifest = json.loads((results / "run_manifest.json").read_text("utf-8"))
    projects, stale, missing = [], [], []
    ref = None
    for pid in manifest["projects"]:
        d = results / "projects" / pid
        if not (d / "inventory.json").exists():
            missing.append(pid)
            continue
        inv = json.loads((d / "inventory.json").read_text("utf-8"))
        rows = json.loads((d / "features.json").read_text("utf-8"))
        key = (inv.get("codebook_version"), inv.get("rule_version"), inv.get("config_hash"), inv.get("code_hash"))
        ref = ref or key
        if key != ref and not allow_mixed:
            stale.append({"project_id": pid, "versions": key, "expected": ref})
        projects.append({"dir": d, "inventory": inv, "rows": rows})
    others = sorted(p.name for p in (results / "projects").iterdir() if p.is_dir() and not p.name.startswith(".") and p.name not in manifest["projects"])
    return {"manifest": manifest, "projects": projects, "version_conflicts": stale, "missing": missing, "not_in_manifest": others}


def build(results: str | Path, cfg: dict | None = None, strata: list[str] | None = None, allow_mixed: bool = False,
          codebook_path: str | None = None) -> dict:
    results = Path(results)
    data = load_results(results, allow_mixed)
    cb = load_codebook(codebook_path)
    manifest = data["manifest"]
    if cfg is None:
        from ..config import load_config
        cfg = load_config(manifest["config"].get("path"), manifest["config"].get("overrides"))
    label = cfg["run"].get("label", "research")
    if data["version_conflicts"] and not allow_mixed:
        raise RuntimeError("projects were audited with different codebook/config/code versions; rerun them (audit --resume) "
                           f"or pass --allow-mixed-versions: {data['version_conflicts'][:3]}")
    projects = data["projects"]
    proj_rows = [r for p in projects for r in p["rows"] if r["scope"] == "project"]
    iface_rows = [r for p in projects for r in p["rows"] if r["scope"] == "interface"]
    for r in proj_rows + iface_rows:
        r["run_label"] = label
    # ------------------------------------------------------------ inventory
    inv_rows = [inventory_row(p["inventory"], label) for p in projects]
    _write_csv(results / "project_inventory.csv", inv_rows)
    # ------------------------------------------------------------ observations (concatenated)
    n_obs = 0
    with open(results / "observations.jsonl.tmp", "w", encoding="utf-8") as fh:
        for p in projects:
            for o in iter_jsonl(p["dir"] / "observations.jsonl"):
                o["run_label"] = label
                fh.write(json.dumps(o, ensure_ascii=False) + "\n")
                n_obs += 1
    (results / "observations.jsonl.tmp").replace(results / "observations.jsonl")
    # ------------------------------------------------------------ features
    feat_fields = ["run_label", "project_id", "scope", "interface_id", "interface_type", "feature_id", "feature_name", "category", "label",
                   "requirement_level", "availability_semantics", "availability", "availability_static", "availability_runtime",
                   "verification", "verification_method", "units_eligible", "units_tested", "units_pass", "units_partial", "units_fail",
                   "units_not_tested", "method", "evidence_grade", "applicability_reason", "rationale", "coverage_limits",
                   "rollup_basis", "heterogeneity", "manual_review_flag", "n_observations", "pages_with_observations", "obs_ids",
                   "wcag_refs", "codebook_version", "rule_version", "config_hash", "code_hash", "audit_timestamp"]
    _write_csv(results / "project_features.csv", proj_rows + iface_rows, feat_fields)
    # ------------------------------------------------------------ matrix
    by_proj = defaultdict(dict)
    for r in proj_rows:
        by_proj[r["project_id"]][r["feature_id"]] = r
    fids = cb.ids()
    mfields = ["run_label", "project_id"] + [f"{fid}.{k}" for fid in fids for k in ("availability", "verification", "method")]
    mrows = []
    for p in projects:
        pid = p["inventory"]["project_id"]
        row = {"run_label": label, "project_id": pid}
        for fid in fids:
            r = by_proj[pid].get(fid, {})
            row[f"{fid}.availability"] = r.get("availability", "")
            row[f"{fid}.verification"] = r.get("verification", "")
            row[f"{fid}.method"] = r.get("method", "")
        mrows.append(row)
    _write_csv(results / "feature_matrix.csv", mrows, mfields)
    # ------------------------------------------------------------ summaries
    enrolled = [p["inventory"]["project_id"] for p in projects]
    summary = feature_summary(cb, proj_rows, enrolled, label)
    _write_csv(results / "feature_summary.csv", summary)
    meta_by = {p["inventory"]["project_id"]: p["inventory"].get("metadata") or {} for p in projects}
    strata = strata if strata is not None else (cfg.get("report", {}).get("strata") or [])
    strata_files = []
    for s in strata:
        rows_s = []
        groups = defaultdict(list)
        for pid in enrolled:
            v = meta_by[pid].get(s)
            groups["unknown" if v in (None, "", []) else (";".join(map(str, v)) if isinstance(v, list) else str(v))].append(pid)
        for g, pids in sorted(groups.items()):
            for r in feature_summary(cb, [x for x in proj_rows if x["project_id"] in set(pids)], pids, label):
                rows_s.append({"stratum": s, "stratum_value": g, **r})
        fn = f"feature_summary_by_{s}.csv"
        _write_csv(results / fn, rows_s)
        strata_files.append(fn)
    method_rows = []
    for g in ("runtime_assessed", "static_only", "none"):
        sel = [r for r in proj_rows if _method_group(r) == g]
        pids = sorted({r["project_id"] for r in sel})
        for r in feature_summary(cb, sel, None, label, per_feature_enrolled=True):
            method_rows.append({"method_group": g, **r})
    _write_csv(results / "feature_summary_by_method.csv", method_rows)
    cat_rows = category_counts(cb, proj_rows, label)
    _write_csv(results / "category_counts.csv", cat_rows)
    _write_csv(results / "measurements.csv", measurement_rows(projects, label))
    def page_rows():
        for p in projects:
            for pg in json.loads((p["dir"] / "pages.json").read_text("utf-8")):
                yield {"run_label": label, "project_id": p["inventory"]["project_id"], **{k: v for k, v in pg.items() if not isinstance(v, dict)}}
    _write_csv(results / "pages.csv", page_rows())
    # ------------------------------------------------------------ review queue
    queue = build_queue(proj_rows, cb, cfg)
    write_queue(results / "manual_review.csv", queue)
    # ------------------------------------------------------------ per-project reports
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html", "j2"]))
    md_env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=False, trim_blocks=True, lstrip_blocks=True)
    for p in projects:
        inv = p["inventory"]
        rows = sorted([r for r in p["rows"] if r["scope"] == "project"], key=lambda r: fids.index(r["feature_id"]))
        rep = {"run_label": label, "inventory": inv, "features": rows,
               "interface_features": [r for r in p["rows"] if r["scope"] == "interface"], "generated": utc_now()}
        atomic_write_json(p["dir"] / "report.json", rep)
        atomic_write_text(p["dir"] / "report.md", md_env.get_template("project_report.md.j2").render(r=rep, cb=cb, label=label))
    # ------------------------------------------------------------ methodology, codebook, html
    shutil.copyfile(cb.path, results / "codebook.yaml")
    fz = frozen_state(cb)
    meth_static = (repo_root() / "docs" / "methodology.md").read_text("utf-8")
    run_params = md_env.get_template("run_parameters.md.j2").render(m=manifest, cfg=cfg, cb=cb, fz=fz, label=label, n_projects=len(projects),
                                                                      tool_versions=_tool_versions(projects), generated=utc_now())
    atomic_write_text(results / "methodology.md", meth_static + "\n\n" + run_params)
    warnings = coverage_warnings(projects, data)
    html = env.get_template("summary.html.j2").render(
        label=label, manifest=manifest, cb=cb, fz=fz, summary=summary, inventory=inv_rows, warnings=warnings,
        features=[{**f, "wcag": wcag_refs(f)} for f in cb.features], categories=cb.categories, generated=utc_now(),
        data_json=json.dumps({"summary": summary, "rows": [{k: r.get(k) for k in ("project_id", "feature_id", "category", "availability", "verification", "method", "manual_review_flag")} for r in proj_rows]}, ensure_ascii=False),
        tool_versions=_tool_versions(projects), strata_files=strata_files)
    atomic_write_text(results / "summary.html", html)
    out = {"projects": len(projects), "observations": n_obs, "project_rows": len(proj_rows), "interface_rows": len(iface_rows),
           "review_items": len(queue), "warnings": len(warnings), "not_in_manifest": data["not_in_manifest"], "missing": data["missing"]}
    atomic_write_json(results / "report_manifest.json", out | {"generated": utc_now(), "run_label": label})
    return out


def _method_group(r: dict) -> str:
    m = r.get("method", "")
    if "runtime" in m:
        return "runtime_assessed"
    if m == "static":
        return "static_only"
    return "none"


def _tool_versions(projects) -> dict:
    tv = {}
    for p in projects:
        for k, v in (p["inventory"].get("tool_versions") or {}).items():
            tv.setdefault(k, set()).add(str(v))
    return {k: sorted(v) for k, v in tv.items()}


def inventory_row(inv: dict, label: str) -> dict:
    srcs = [i for i in inv["interfaces"] if i["kind"] == "source"]
    webs = [i for i in inv["interfaces"] if i["kind"] == "web"]
    meta = inv.get("metadata") or {}
    rel = inv.get("deployment_repository_relationship") or {}
    cov = [(i.get("coverage") or {}) for i in webs]
    excl = []
    if inv.get("ui_applicability") == "not_applicable":
        excl.append("browser UI checks not applicable (declared non-web)")
    for w in webs:
        if w.get("status") not in ("ok",):
            excl.append(f"{w['interface_id'].split('::')[-1]}: {w.get('status')} {w.get('reason') or ''}".strip())
    for s in srcs:
        if s.get("status") != "ok":
            excl.append(f"{s['interface_id'].split('::')[-1]}: {s.get('status')} {s.get('reason') or ''}".strip())
    return {
        "run_label": label, "project_id": inv["project_id"], "id_source": inv.get("id_source"), "audit_status": inv.get("status"),
        "ui_applicability": inv.get("ui_applicability"), "ui_applicability_reason": inv.get("ui_applicability_reason"),
        "declared_interface_types": inv.get("declared_types"), "n_interfaces": len(inv["interfaces"]),
        "interfaces": [f"{i['interface_type']}={(i['location'].get('url') or i['location'].get('path'))}" for i in inv["interfaces"]],
        "repositories": [s["location"].get("url") or s["location"].get("path") for s in srcs],
        "repository_status": [s.get("status") for s in srcs], "source_commit": [s.get("commit") for s in srcs],
        "source_dirty": [s.get("dirty") for s in srcs],
        "static_files_inspected": sum((s.get("static_coverage") or {}).get("files_inspected", 0) or 0 for s in srcs),
        "ui_source_detected": [s.get("ui_source_detected") for s in srcs],
        "websites": [w["location"].get("url") for w in webs], "website_types": [w["interface_type"] for w in webs],
        "website_status": [w.get("status") for w in webs],
        "website_http_status": [((w.get("home") or {}).get("status")) for w in webs],
        "website_final_url": [((w.get("home") or {}).get("final_url")) for w in webs],
        "pages_rendered": sum(c.get("pages_rendered", 0) for c in cov), "pages_sampled": sum(c.get("pages_sampled", 0) for c in cov),
        "pages_skipped": sum(c.get("pages_skipped", 0) for c in cov), "pages_blocked": sum(c.get("pages_blocked", 0) for c in cov),
        "pages_failed": sum(c.get("pages_failed", 0) for c in cov), "blocked_mutating_requests": sum(c.get("blocked_requests", 0) for c in cov),
        "deployment_repository_relationship": rel.get("status", "unknown"), "relationship_tool_check": rel.get("tool_check"),
        "metadata_supplied": meta.get("metadata_supplied", False), "project_type": meta.get("project_type") or "unknown",
        "country": meta.get("country") or "unknown", "government_level": meta.get("government_level") or "unknown",
        "operator": meta.get("operator") or "unknown", "service_sector": meta.get("service_sector") or "unknown",
        "classification_labels": [f"{c['label']}" + (f" ({c['source']})" if c.get("source") else "") for c in (meta.get("classification_labels") or [])] or "unknown",
        "expected_languages": meta.get("expected_languages") or "unknown", "warnings": inv.get("warnings"),
        "exclusions_and_limits": excl, "errors": [e.splitlines()[-1] if e else "" for e in (inv.get("errors") or [])][:3],
        "mode": inv.get("mode"), "codebook_version": inv.get("codebook_version"), "config_hash": inv.get("config_hash"),
        "audit_started": inv.get("started"), "fingerprint": inv.get("fingerprint"),
    }


def feature_summary(cb, proj_rows: list[dict], enrolled: list[str] | None, label: str, per_feature_enrolled: bool = False) -> list[dict]:
    out = []
    by_f = defaultdict(list)
    for r in proj_rows:
        by_f[r["feature_id"]].append(r)
    for f in cb.features:
        rows = by_f.get(f["id"], [])
        n_enr = len(rows) if (per_feature_enrolled or enrolled is None) else len(enrolled)
        av = Counter(r["availability"] for r in rows)
        ve = Counter(r["verification"] for r in rows)
        assess = av["present"] + av["not_found_in_scope"]
        verified = ve["pass"] + ve["partial"] + ve["fail"]
        rt = [r for r in rows if "runtime" in r.get("method", "")]
        pages = [int(r.get("pages_with_observations") or 0) for r in rt]
        note = []
        if f["availability_semantics"] == "subject_presence":
            note.append("availability = subject present & assessed (not a prevalence measure)")
        if f["requirement_level"] in ("conditional_indicator", "descriptive"):
            note.append("absence is not a barrier by itself")
        if any(d.get("status") == "manual_only" for side in ("static", "runtime") for d in (f.get("detectors", {}) or {}).get(side, []) or []):
            note.append("manual-only: machine verification always not_tested")
        out.append({
            "run_label": label, "feature_id": f["id"], "feature_name": f["name"], "category": f["category"], "label": f["label"],
            "requirement_level": f["requirement_level"], "availability_semantics": f["availability_semantics"],
            "wcag_refs": ";".join(str(m["sc"]) for m in f.get("wcag", []) or []),
            "n_enrolled": n_enr, "n_present": av["present"], "n_not_found_in_scope": av["not_found_in_scope"], "n_unknown": av["unknown"],
            "n_not_applicable": av["not_applicable"], "availability_denominator": assess,
            "pct_present_of_assessable": round(100 * av["present"] / assess, 1) if assess else "",
            "n_pass": ve["pass"], "n_partial": ve["partial"], "n_fail": ve["fail"], "n_not_tested": ve["not_tested"],
            "verification_denominator": verified, "pct_pass_of_verified": round(100 * ve["pass"] / verified, 1) if verified else "",
            "n_present_runtime_confirmed": sum(1 for r in rows if r["availability"] == "present" and "runtime" in r.get("method", "") and r.get("availability_runtime") == "present"),
            "n_present_static_only": sum(1 for r in rows if r["availability"] == "present" and r.get("availability_runtime") != "present"),
            "n_runtime_assessed": len(rt), "median_pages_with_observations": statistics.median(pages) if pages else "",
            "n_manual_review_flagged": sum(1 for r in rows if r.get("manual_review_flag") in (True, "true")),
            "interpretation_notes": "; ".join(note),
        })
    return out


def category_counts(cb, proj_rows: list[dict], label: str) -> list[dict]:
    out = []
    by = defaultdict(list)
    for r in proj_rows:
        by[(r["project_id"], r["category"])].append(r)
    for (pid, cat), rows in sorted(by.items()):
        av = Counter(r["availability"] for r in rows)
        ve = Counter(r["verification"] for r in rows)
        out.append({"run_label": label, "project_id": pid, "category": cat, "category_name": cb.categories[cat], "n_features": len(rows),
                    **{f"availability_{k}": av[k] for k in AV_STATES}, **{f"verification_{k}": ve[k] for k in VER_STATES}})
    return out


def measurement_rows(projects, label: str) -> Iterable[dict]:
    for p in projects:
        for o in iter_jsonl(p["dir"] / "observations.jsonl"):
            for k, v in (o.get("measurements") or {}).items():
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                u = o.get("unit") or {}
                yield {"run_label": label, "project_id": o["project_id"], "interface_id": o["interface_id"], "feature_id": o["feature_id"],
                            "check_id": o["check_id"], "method": o["method"], "page_url": u.get("page_url") or u.get("source"),
                            "viewport": u.get("viewport"), "state": u.get("state"), "metric": k, "value": v, "obs_id": o["obs_id"]}


def coverage_warnings(projects, data) -> list[dict]:
    w = []
    for p in projects:
        inv = p["inventory"]
        pid = inv["project_id"]
        if inv.get("status") == "failed":
            w.append({"project_id": pid, "level": "error", "message": f"audit failed: {(inv.get('errors') or [''])[0].splitlines()[-1] if inv.get('errors') else ''}"})
        for i in inv["interfaces"]:
            st = i.get("status")
            if st not in ("ok", "not_applicable", "not_run"):
                w.append({"project_id": pid, "level": "warning", "message": f"{i['interface_id'].split('::')[-1]} {st}: {i.get('reason', '')}"})
            if i.get("coverage_note"):
                w.append({"project_id": pid, "level": "info", "message": f"{i['interface_id'].split('::')[-1]}: {i['coverage_note']}"})
        for x in inv.get("warnings") or []:
            w.append({"project_id": pid, "level": "warning", "message": x})
        if inv.get("ui_applicability") == "not_applicable":
            w.append({"project_id": pid, "level": "info", "message": f"UI features not applicable: {inv.get('ui_applicability_reason')}"})
    for pid in data["missing"]:
        w.append({"project_id": pid, "level": "error", "message": "no result folder (not yet audited or interrupted)"})
    for pid in data["not_in_manifest"]:
        w.append({"project_id": pid, "level": "info", "message": "result folder exists but project is not in the current run manifest (excluded)"})
    return w
