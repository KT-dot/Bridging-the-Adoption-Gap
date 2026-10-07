"""Worker subprocess entry point: audit exactly one project (internal; used by batch.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import load_config
from .inputs import parse_list
from .metadata import load_metadata
from .pipeline import audit_project


def main(argv: list[str]) -> int:
    args = json.loads(argv[0])
    cfg = load_config(args.get("config"), args.get("overrides"))
    parsed = parse_list(args["input"])
    proj = next((p for p in parsed.projects if p.project_id == args["project_id"]), None)
    if proj is None:
        print(f"project {args['project_id']} not found in {args['input']}", file=sys.stderr)
        return 2
    meta_all, _ = load_metadata(args.get("metadata"))
    res = audit_project(proj, meta_all, cfg, args.get("codebook"), Path(args["out"]), cfg["run"]["mode"],
                        args["fingerprint"], args.get("revisions") or {}, log=lambda m: print(m, flush=True))
    print(json.dumps(res))
    return 0 if res["status"] != "failed" else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
