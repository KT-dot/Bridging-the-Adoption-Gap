"""Load, validate, hash and freeze the feature codebook."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .util import atomic_write_json, package_root, repo_root, sha256_obj, utc_now

DEFAULT_CODEBOOK = repo_root() / "codebook" / "codebook.yaml"
AVAILABILITY = ("present", "not_found_in_scope", "unknown", "not_applicable")
VERIFICATION = ("pass", "partial", "fail", "not_tested")
GRADES = ("direct_observation", "static_indication", "heuristic_inference", "human_validation")
LABELS = ("wcag_related", "ux_research_indicator")
REQ_LEVELS = ("universal_check", "conditional_check", "conditional_indicator", "descriptive")
SEMANTICS = ("implementation", "subject_presence")
VERIF_AGG = ("mixed_is_partial", "any_fail_is_fail")
ROLLUPS = ("rollup_service_first", "rollup_any_web")
DETECTOR_STATUS = ("implemented", "partial", "manual_only", "planned")
REQUIRED_KEYS = ("id", "name", "category", "label", "requirement_level", "availability_semantics",
                 "definition", "barrier", "applicability", "detectors", "decision_rules",
                 "evidence_required", "automation_limitations", "manual_review", "wcag",
                 "min_coverage", "aggregation")


@dataclass
class Codebook:
    meta: dict
    features: list[dict]
    categories: dict
    path: Path
    content_hash: str
    raw: dict

    @property
    def version(self) -> str:
        return str(self.meta["version"])

    @property
    def status(self) -> str:
        return str(self.meta.get("status", "draft"))

    def feature(self, fid: str) -> dict:
        return self._by_id[fid]

    def ids(self) -> list[str]:
        return [f["id"] for f in self.features]

    def __post_init__(self):
        self._by_id = {f["id"]: f for f in self.features}


def load_codebook(path: str | Path | None = None) -> Codebook:
    p = Path(path) if path else DEFAULT_CODEBOOK
    raw = yaml.safe_load(p.read_text("utf-8"))
    features = raw["features"]
    for f in features:  # drop YAML-merge helper noise and normalise
        f.setdefault("universal_requirement", False)
    return Codebook(meta=raw["codebook"], features=features, categories=raw["categories"],
                    path=p, content_hash=sha256_obj({"codebook": raw["codebook"] | {"status": None, "frozen_at": None},
                                                     "features": features, "categories": raw["categories"]}),
                    raw=raw)


def _wcag_snapshot() -> dict[str, dict]:
    data = json.loads((package_root() / "data" / "wcag22_sc.json").read_text("utf-8"))
    return {sc["sc"]: sc for sc in data["success_criteria"]}


def _axe_snapshot() -> dict[str, dict]:
    data = json.loads((package_root() / "data" / "axe_rules.json").read_text("utf-8"))
    return {r["id"]: r for r in data["rules"]}


def validate_codebook(cb: Codebook) -> list[str]:
    """Return a list of problems (empty when valid)."""
    errs: list[str] = []
    wcag = _wcag_snapshot()
    axe = _axe_snapshot()
    seen = set()
    for f in cb.features:
        fid = f.get("id", "?")
        if fid in seen:
            errs.append(f"{fid}: duplicate id")
        seen.add(fid)
        for k in REQUIRED_KEYS:
            if k not in f:
                errs.append(f"{fid}: missing {k}")
        if f.get("category") not in cb.categories:
            errs.append(f"{fid}: unknown category {f.get('category')}")
        if not fid.startswith(str(f.get("category", "")) + "-"):
            errs.append(f"{fid}: id prefix does not match category")
        for key, allowed in (("label", LABELS), ("requirement_level", REQ_LEVELS),
                             ("availability_semantics", SEMANTICS)):
            if f.get(key) not in allowed:
                errs.append(f"{fid}: {key}={f.get(key)!r} not in {allowed}")
        agg = f.get("aggregation", {})
        if agg.get("verification") not in VERIF_AGG:
            errs.append(f"{fid}: aggregation.verification invalid")
        if agg.get("rollup") not in ROLLUPS:
            errs.append(f"{fid}: aggregation.rollup invalid")
        rules = f.get("decision_rules", {})
        for k in rules.get("availability", {}):
            if k not in AVAILABILITY:
                errs.append(f"{fid}: decision_rules.availability has unknown state {k}")
        for k in rules.get("verification", {}):
            if k not in VERIFICATION:
                errs.append(f"{fid}: decision_rules.verification has unknown state {k}")
        if "verification" not in rules or "not_tested" not in rules["verification"] and \
                not any(s in rules["verification"] for s in ("pass", "fail")):
            errs.append(f"{fid}: verification rules must define pass/fail or not_tested")
        for side in ("static", "runtime"):
            for d in (f.get("detectors", {}) or {}).get(side, []) or []:
                if d.get("status") not in DETECTOR_STATUS:
                    errs.append(f"{fid}: detector {d.get('id')} has invalid status")
                for r in d.get("rules", []) or []:
                    if r not in axe:
                        errs.append(f"{fid}: axe rule {r!r} not in installed axe-core rule list")
        for m in f.get("wcag", []) or []:
            sc = str(m.get("sc"))
            if sc not in wcag:
                errs.append(f"{fid}: WCAG SC {sc} not in WCAG 2.2 snapshot")
            elif wcag[sc].get("obsolete"):
                errs.append(f"{fid}: WCAG SC {sc} is obsolete in WCAG 2.2")
            if m.get("relation") not in ("automated_partial", "related", "manual"):
                errs.append(f"{fid}: WCAG relation {m.get('relation')!r} invalid")
        if f.get("label") == "wcag_related" and not f.get("wcag"):
            errs.append(f"{fid}: wcag_related feature without WCAG mapping")
        if f.get("label") == "ux_research_indicator" and any(
                m.get("relation") == "automated_partial" for m in f.get("wcag", []) or []):
            errs.append(f"{fid}: UX indicator should not claim automated_partial WCAG coverage")
        mr = f.get("manual_review", {})
        if mr.get("required") and not mr.get("instructions"):
            errs.append(f"{fid}: manual review required but no instructions")
    return errs


def wcag_refs(feature: dict) -> list[dict]:
    snap = _wcag_snapshot()
    out = []
    for m in feature.get("wcag", []) or []:
        sc = snap.get(str(m["sc"]), {})
        out.append({"sc": str(m["sc"]), "name": sc.get("name"), "level": sc.get("level"),
                    "relation": m.get("relation"), "spec_url": sc.get("spec_url"),
                    "understanding_url": sc.get("understanding_url")})
    return out


def lock_path(cb: Codebook) -> Path:
    return cb.path.with_name("codebook.lock.json")


def freeze(cb: Codebook, note: str = "") -> dict:
    errs = validate_codebook(cb)
    if errs:
        raise ValueError("codebook invalid; fix before freezing:\n" + "\n".join(errs))
    lock = {"codebook_id": cb.meta["id"], "version": cb.version, "content_sha256": cb.content_hash,
            "frozen_at": utc_now(), "note": note, "n_features": len(cb.features)}
    atomic_write_json(lock_path(cb), lock)
    return lock


def frozen_state(cb: Codebook) -> dict:
    p = lock_path(cb)
    if not p.exists():
        return {"frozen": False, "reason": "no codebook.lock.json (draft codebook: pilot use only)"}
    lock = json.loads(p.read_text("utf-8"))
    if lock.get("content_sha256") != cb.content_hash:
        return {"frozen": False, "reason": "codebook content changed after freezing; bump version and re-freeze",
                "lock": lock}
    return {"frozen": True, "lock": lock}
