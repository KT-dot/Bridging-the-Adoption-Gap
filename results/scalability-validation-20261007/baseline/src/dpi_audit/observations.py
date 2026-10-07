"""Observation records: the raw, evidence-backed unit of measurement.

One observation = one (project, interface, unit, feature, check). A unit is a page x
viewport x interaction state for runtime checks, or the repository (with file-level
evidence) for static checks. Observation IDs are deterministic so reruns with the same
inputs produce the same IDs and project-level rows can reference them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .codebook import AVAILABILITY, GRADES, VERIFICATION
from .util import sha256_obj, utc_now

METHODS = ("static", "runtime", "manual")


@dataclass
class Observation:
    project_id: str
    interface_id: str
    interface_type: str
    feature_id: str
    check_id: str
    method: str
    evidence_grade: str
    availability: str
    verification: str
    rationale: str
    unit: dict
    applicability_reason: str = ""
    evidence: list = field(default_factory=list)
    measurements: dict = field(default_factory=dict)
    manual_review: bool = False
    rule_id: str = ""
    timestamp: str = field(default_factory=utc_now)
    obs_id: str = ""

    def __post_init__(self):
        if self.availability not in AVAILABILITY:
            raise ValueError(f"{self.feature_id}: bad availability {self.availability}")
        if self.verification not in VERIFICATION:
            raise ValueError(f"{self.feature_id}: bad verification {self.verification}")
        if self.evidence_grade not in GRADES:
            raise ValueError(f"{self.feature_id}: bad grade {self.evidence_grade}")
        if self.method not in METHODS:
            raise ValueError(f"{self.feature_id}: bad method {self.method}")
        if not self.obs_id:
            self.obs_id = "obs-" + sha256_obj({
                "p": self.project_id, "i": self.interface_id, "f": self.feature_id, "c": self.check_id,
                "u": {k: self.unit.get(k) for k in ("kind", "page_url", "viewport", "state", "source")},
            })[:20]

    def to_dict(self, run_ctx: dict | None = None) -> dict:
        d = {
            "obs_id": self.obs_id, "project_id": self.project_id, "interface_id": self.interface_id,
            "interface_type": self.interface_type, "feature_id": self.feature_id,
            "check_id": self.check_id, "rule_id": self.rule_id or f"{self.feature_id}:{self.check_id}",
            "method": self.method, "evidence_grade": self.evidence_grade,
            "availability": self.availability, "verification": self.verification,
            "applicability_reason": self.applicability_reason, "rationale": self.rationale,
            "unit": self.unit, "evidence": self.evidence, "measurements": self.measurements,
            "manual_review": self.manual_review, "timestamp": self.timestamp,
        }
        if run_ctx:
            d.update(run_ctx)
        return d


class Sink:
    """Collects observations for one project; guarantees unique obs_ids."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.items: dict[str, Observation] = {}

    def add(self, obs: Observation) -> Observation:
        if obs.obs_id in self.items:
            # same unit+check reported twice (e.g. retried state): keep the later, more complete one
            pass
        self.items[obs.obs_id] = obs
        return obs

    def extend(self, obs_list):
        for o in obs_list:
            self.add(o)

    def all(self) -> list[Observation]:
        return list(self.items.values())


def src_evidence(path: str, line: int, end_line: int | None = None, excerpt: str = "", note: str = "") -> dict:
    return {"type": "source_location", "path": path, "line_start": line, "line_end": end_line or line,
            "excerpt": excerpt[:300], "note": note}


def dom_evidence(url: str, selector: str, excerpt: str = "", viewport: str = "", state: str = "", note: str = "") -> dict:
    return {"type": "dom", "page_url": url, "selector": selector, "excerpt": (excerpt or "")[:500],
            "viewport": viewport, "state": state, "note": note}


def tool_evidence(tool: str, rule_id: str, outcome: str, nodes: list | None = None, extra: dict | None = None) -> dict:
    d = {"type": "tool_finding", "tool": tool, "rule_id": rule_id, "outcome": outcome,
         "nodes": (nodes or [])[:10]}
    if extra:
        d.update(extra)
    return d
