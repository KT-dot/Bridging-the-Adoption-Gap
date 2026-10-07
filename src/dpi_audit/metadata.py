"""Optional per-project metadata (YAML or CSV) keyed by project_id.

Missing values stay ``None`` and are reported as ``unknown``; nothing is inferred.
See docs/input_format.md for the schema and metadata.example.yaml / .csv for examples.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import yaml

FIELDS = {
    "project_name": str,
    "classification_labels": list,     # [{label, source}] preserved verbatim (e.g. "DPG", "DPI")
    "project_type": str,
    "country": str,
    "government_level": str,
    "operator": str,
    "service_sector": str,
    "intended_audience": str,
    "expected_languages": list,        # BCP 47 tags documented for the deployment
    "critical_journeys": list,
    "deployment_repository_relationship": dict,
    "auth": dict,
    "offline_relevant": bool,
    "long_process": bool,
    "untranslated_allowlist": list,
    "notes": str,
}
RELATIONSHIP_STATUSES = {"supplied", "verified", "unknown"}
LANG_RX = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
JOURNEY_ACTIONS = {"goto", "click", "expect_text", "expect_url", "fill_test_value", "press"}
CSV_LIST_SEP = ";"


class MetadataError(ValueError):
    pass


def empty_record() -> dict:
    rec = {k: None for k in FIELDS}
    rec["deployment_repository_relationship"] = {"status": "unknown", "detail": None,
                                                 "evidence_url": None, "deployed_version": None,
                                                 "status_source": "default (not supplied)"}
    rec["metadata_supplied"] = False
    return rec


def load_metadata(path: str | Path | None) -> tuple[dict[str, dict], list[str]]:
    """Return ({project_id: record}, warnings)."""
    if not path:
        return {}, []
    p = Path(path)
    if p.suffix.lower() in (".yaml", ".yml"):
        raw = yaml.safe_load(p.read_text("utf-8")) or {}
        if not isinstance(raw, dict):
            raise MetadataError(f"{p}: top level must be a mapping")
        projects = raw.get("projects", {k: v for k, v in raw.items() if k != "schema_version"})
        if not isinstance(projects, dict):
            raise MetadataError(f"{p}: 'projects' must map project_id -> fields")
    elif p.suffix.lower() == ".csv":
        projects = _read_csv(p)
    else:
        raise MetadataError(f"{p}: metadata must be .yaml/.yml or .csv")
    out: dict[str, dict] = {}
    warnings: list[str] = []
    for pid, fields in projects.items():
        pid = str(pid)
        rec, w = normalize_record(pid, fields or {})
        out[pid] = rec
        warnings += w
    return out, warnings


def _read_csv(p: Path) -> dict[str, dict]:
    projects: dict[str, dict] = {}
    with open(p, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("project_id") or "").strip()
            if not pid:
                continue
            if pid in projects:
                raise MetadataError(f"{p}: duplicate project_id {pid!r}")
            rec: dict[str, Any] = {}
            for k, v in row.items():
                if k is None or k == "project_id":
                    continue
                v = (v or "").strip()
                if v == "":
                    continue
                if k == "expected_languages" or k == "untranslated_allowlist":
                    rec[k] = [x.strip() for x in v.split(CSV_LIST_SEP) if x.strip()]
                elif k == "classification_labels":
                    labels = []
                    for item in v.split(CSV_LIST_SEP):
                        if not item.strip():
                            continue
                        label, _, src = item.partition("=")
                        labels.append({"label": label.strip(), "source": src.strip() or None})
                    rec[k] = labels
                elif k in ("critical_journeys_json", "critical_journeys"):
                    rec["critical_journeys"] = json.loads(v)
                elif k == "auth_json":
                    rec["auth"] = json.loads(v)
                elif k in ("offline_relevant", "long_process"):
                    rec[k] = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(v.lower(), v)
                elif k.startswith("relationship_"):
                    sub = k.removeprefix("relationship_")
                    rec.setdefault("deployment_repository_relationship", {})[sub] = v
                elif k == "deployed_version":
                    rec.setdefault("deployment_repository_relationship", {})["deployed_version"] = v
                else:
                    rec[k] = v
            projects[pid] = rec
    return projects


def normalize_record(pid: str, fields: dict) -> tuple[dict, list[str]]:
    rec = empty_record()
    rec["metadata_supplied"] = True
    warnings: list[str] = []
    for k, v in fields.items():
        if k not in FIELDS:
            warnings.append(f"metadata[{pid}]: unknown field {k!r} ignored")
            continue
        if v is None or v == "" or v == []:
            continue
        typ = FIELDS[k]
        if typ is bool and not isinstance(v, bool):
            warnings.append(f"metadata[{pid}].{k}: expected true/false, got {v!r}; left unknown")
            continue
        if typ is list and not isinstance(v, list):
            v = [v]
        if typ is str:
            v = str(v)
        rec[k] = v
    langs = rec.get("expected_languages")
    if langs:
        bad = [x for x in langs if not LANG_RX.match(str(x))]
        if bad:
            warnings.append(f"metadata[{pid}].expected_languages: invalid BCP 47 tag(s) {bad}; ignored")
        rec["expected_languages"] = [str(x) for x in langs if LANG_RX.match(str(x))] or None
    labels = rec.get("classification_labels")
    if labels:
        norm = []
        for item in labels:
            if isinstance(item, str):
                norm.append({"label": item, "source": None})
            elif isinstance(item, dict) and item.get("label"):
                norm.append({"label": str(item["label"]), "source": item.get("source") or item.get("url")})
            else:
                warnings.append(f"metadata[{pid}].classification_labels: malformed item {item!r} ignored")
        rec["classification_labels"] = norm or None
    rel = dict(rec["deployment_repository_relationship"])
    supplied_rel = fields.get("deployment_repository_relationship") or {}
    if isinstance(supplied_rel, dict) and supplied_rel:
        rel.update({k: supplied_rel.get(k) for k in ("status", "detail", "evidence_url", "deployed_version") if supplied_rel.get(k)})
        rel["status_source"] = "metadata"
    if rel.get("status") not in RELATIONSHIP_STATUSES:
        warnings.append(f"metadata[{pid}]: relationship status {rel.get('status')!r} not in {sorted(RELATIONSHIP_STATUSES)}; set to unknown")
        rel["status"] = "unknown"
    rec["deployment_repository_relationship"] = rel
    for j in rec.get("critical_journeys") or []:
        if not isinstance(j, dict) or not j.get("id") or not isinstance(j.get("steps"), list):
            warnings.append(f"metadata[{pid}]: critical journey needs id and steps list: {j!r}")
            continue
        for st in j["steps"]:
            if not isinstance(st, dict) or st.get("action") not in JOURNEY_ACTIONS:
                warnings.append(f"metadata[{pid}] journey {j['id']}: unsupported step {st!r} "
                                f"(allowed actions: {sorted(JOURNEY_ACTIONS)})")
    auth = rec.get("auth")
    if auth and any(k in auth for k in ("username", "password")):
        raise MetadataError(f"metadata[{pid}].auth: put credentials in environment variables "
                            "(username_env/password_env), never in metadata files")
    return rec, warnings


def get_record(meta: dict[str, dict], project_id: str) -> dict:
    return meta.get(project_id) or empty_record()


def check_join(meta: dict[str, dict], project_ids: list[str]) -> list[str]:
    ids = set(project_ids)
    return [f"metadata entry {pid!r} has no matching project in the input list" for pid in meta if pid not in ids]
