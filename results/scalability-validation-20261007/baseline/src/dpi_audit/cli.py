"""dpi-audit command-line interface.

    dpi-audit audit   --input list.txt --output results --mode static|runtime|hybrid
                      [--config study.yaml] [--metadata metadata.yaml] [--limit N] [--resume | --fresh]
                      [--workers N] [--max-pages N] [--timeout SECONDS]
    dpi-audit report  --results results [--strata project_type,country] [--allow-mixed-versions]
    dpi-audit validate --input list.txt [--metadata metadata.yaml]
    dpi-audit codebook validate|freeze|show
    dpi-audit review  agreement|metrics|apply --results results [--file manual_review.csv]
    dpi-audit fixtures serve [--port-base 18700]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _cmd_audit(a) -> int:
    from .batch import BatchError, run_batch
    from .config import cli_overrides, load_config
    from .report import build
    ov = cli_overrides(a.mode, a.workers, a.max_pages, a.timeout)
    cfg = load_config(a.config, ov)
    try:
        m = run_batch(a.input, a.output, cfg, a.config, ov, a.metadata, a.codebook, a.limit, a.resume, a.fresh)
    except BatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    statuses = {}
    for r in m.get("results", []):
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    print(f"audited: {statuses or 'nothing scheduled'}; skipped up-to-date: {len(m['resume']['skipped_up_to_date'])}")
    if not a.no_report:
        out = build(a.output, cfg, codebook_path=a.codebook)
        print(f"report: {json.dumps(out)}")
    return 0


def _cmd_report(a) -> int:
    from .report import build
    strata = [s.strip() for s in a.strata.split(",")] if a.strata else None
    try:
        out = build(a.results, None, strata, a.allow_mixed_versions, a.codebook)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0


def _cmd_validate(a) -> int:
    from .inputs import parse_list, project_ui_applicability
    from .metadata import check_join, load_metadata
    r = parse_list(a.input)
    print(f"{a.input}: {r.n_lines} lines, {r.n_entries} entries, {len(r.projects)} unique projects, "
          f"{len(r.errors)} error(s), {len(r.duplicates)} duplicate line(s) ignored")
    for e in r.errors:
        print(f"  ERROR line {e['line']}: {e['error']}")
    for d in r.duplicates:
        print(f"  duplicate line {d['line']}: {d.get('note') or 'same as line ' + str(d.get('duplicate_of_line'))}")
    for w in r.warnings:
        print(f"  WARNING {w}")
    if a.verbose:
        for p in r.projects:
            ui = project_ui_applicability(p)
            print(f"  {p.project_id} ({p.id_source}): " + ", ".join(f"{i.interface_type}={i.location.url or i.location.path}" for i in p.interfaces)
                  + f" | ui={ui[0]}" + (f" | warnings: {p.warnings}" if p.warnings else ""))
    if a.metadata:
        meta, warn = load_metadata(a.metadata)
        warn += check_join(meta, [p.project_id for p in r.projects])
        missing = [p.project_id for p in r.projects if p.project_id not in meta]
        print(f"{a.metadata}: {len(meta)} record(s); {len(missing)} project(s) without metadata (fields stay unknown)")
        for w in warn:
            print(f"  WARNING {w}")
    return 1 if r.errors else 0


def _cmd_codebook(a) -> int:
    from .codebook import freeze, frozen_state, load_codebook, validate_codebook
    cb = load_codebook(a.codebook)
    if a.action == "validate":
        errs = validate_codebook(cb)
        print(f"{cb.path}: version {cb.version}, {len(cb.features)} features, status {cb.status}, content {cb.content_hash[:16]}")
        print(f"frozen: {frozen_state(cb)}")
        for e in errs:
            print(f"  ERROR {e}")
        return 1 if errs else 0
    if a.action == "freeze":
        print(json.dumps(freeze(cb, a.note or ""), indent=1))
        return 0
    for f in cb.features:
        st = [d["status"] for side in ("static", "runtime") for d in (f.get("detectors", {}) or {}).get(side, []) or []]
        print(f"{f['id']:9} {f['label']:22} {f['requirement_level']:22} detectors={','.join(sorted(set(st)))}  {f['name']}")
    return 0


def _cmd_review(a) -> int:
    from .codebook import load_codebook
    from .config import load_config
    from .review import agreement, apply_reviews, mark_disagreements, metrics, read_review
    res = Path(a.results)
    path = Path(a.file) if a.file else res / "manual_review.csv"
    rows = mark_disagreements(read_review(path))
    if a.action == "agreement":
        print(json.dumps(agreement(rows), indent=1))
    elif a.action == "metrics":
        cfg = load_config(None)
        print(json.dumps(metrics(rows, int(cfg["review"]["min_reference_per_class"])), indent=1))
    else:
        print(json.dumps(apply_reviews(res, path), indent=1))
    return 0


def _cmd_fixtures(a) -> int:
    from .fixtures import serve
    servers = serve(port_base=a.port_base)
    for name, port, _ in servers:
        print(f"{name:24} http://127.0.0.1:{port}/", flush=True)
    print("serving fixture sites; Ctrl+C to stop", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dpi-audit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codebook", default=None, help="codebook YAML (default: codebook/codebook.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    au = sub.add_parser("audit", help="audit projects listed in an input file")
    au.add_argument("--input", required=True)
    au.add_argument("--output", required=True)
    au.add_argument("--mode", choices=["static", "runtime", "hybrid"])
    au.add_argument("--config")
    au.add_argument("--metadata")
    au.add_argument("--limit", type=int)
    au.add_argument("--resume", action="store_true", help="skip projects whose checkpoint fingerprint is unchanged")
    au.add_argument("--fresh", action="store_true", help="delete existing results in --output first")
    au.add_argument("--workers", type=int)
    au.add_argument("--max-pages", type=int)
    au.add_argument("--timeout", type=float, help="navigation timeout in seconds")
    au.add_argument("--no-report", action="store_true", help="do not build reports after auditing")
    au.set_defaults(fn=_cmd_audit)
    rp = sub.add_parser("report", help="(re)build all outputs from a results folder")
    rp.add_argument("--results", required=True)
    rp.add_argument("--strata", help="comma-separated metadata fields to stratify feature_summary by")
    rp.add_argument("--allow-mixed-versions", action="store_true")
    rp.set_defaults(fn=_cmd_report)
    va = sub.add_parser("validate", help="validate list.txt (and metadata) without auditing")
    va.add_argument("--input", required=True)
    va.add_argument("--metadata")
    va.add_argument("-v", "--verbose", action="store_true")
    va.set_defaults(fn=_cmd_validate)
    cb = sub.add_parser("codebook", help="validate, freeze or list the codebook")
    cb.add_argument("action", choices=["validate", "freeze", "show"])
    cb.add_argument("--note")
    cb.set_defaults(fn=_cmd_codebook)
    rv = sub.add_parser("review", help="manual review: agreement, metrics, apply")
    rv.add_argument("action", choices=["agreement", "metrics", "apply"])
    rv.add_argument("--results", required=True)
    rv.add_argument("--file")
    rv.set_defaults(fn=_cmd_review)
    fx = sub.add_parser("fixtures", help="serve bundled fixture sites")
    fx.add_argument("action", choices=["serve"])
    fx.add_argument("--port-base", type=int, default=18700)
    fx.set_defaults(fn=_cmd_fixtures)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
