"""Human review: deterministic stratified queue, inter-rater agreement, validation metrics.

* Queue strata = category x method group x machine label group (present, not found,
  unknown, partial, fail, pass). Selection within a stratum is ordered by
  sha256(seed | project | feature) so the queue is reproducible.
* Agreement between two reviewers: Cohen's kappa (nominal), with percent agreement and n.
* Machine-vs-reference precision/recall for availability "present" are computed only when
  the adjudicated reference sample has at least review.min_reference_per_class positives
  and negatives; 95% Wilson intervals are reported. The sample is stratified, so metrics are
  unweighted sample statistics, not population estimates. Nothing is computed before review.
"""
from __future__ import annotations

import csv
import hashlib
import math
from collections import Counter, defaultdict
from pathlib import Path

REVIEW_FIELDS = ["review_id", "stratum", "project_id", "feature_id", "feature_name", "category", "label", "method",
                 "machine_availability", "machine_verification", "machine_evidence_grade", "machine_rationale",
                 "evidence_ref", "review_instructions",
                 "reviewer_1_id", "reviewer_1_availability", "reviewer_1_verification", "reviewer_1_notes",
                 "reviewer_2_id", "reviewer_2_availability", "reviewer_2_verification", "reviewer_2_notes",
                 "disagreement", "adjudicated_availability", "adjudicated_verification", "adjudicator_id", "adjudication_notes"]
HUMAN_CHECKS = [("HUMAN-SR", "Screen-reader usability of the main service journey"),
                ("HUMAN-TQ", "Translation quality of the switched interface"),
                ("HUMAN-PL", "Plain-language quality of instructions"),
                ("HUMAN-NAV", "Intuitiveness of navigation for first-time users")]
AV = {"present", "not_found_in_scope", "unknown", "not_applicable"}
VE = {"pass", "partial", "fail", "not_tested"}


def _h(seed: str, *parts) -> str:
    return hashlib.sha256("|".join([seed, *map(str, parts)]).encode()).hexdigest()


def label_group(r: dict) -> str | None:
    if r["verification"] in ("partial", "fail"):
        return r["verification"]
    if r["availability"] == "not_applicable":
        return None
    if r["verification"] == "pass":
        return "pass"
    return r["availability"]


def method_group(r: dict) -> str:
    m = r.get("method", "")
    return "runtime" if "runtime" in m else "static" if m == "static" else "none"


def build_queue(proj_rows: list[dict], cb, cfg: dict) -> list[dict]:
    rc = cfg.get("review", {})
    seed = str(rc.get("seed", "dpi-audit-review-v1"))
    k = int(rc.get("per_stratum", 2))
    strata = defaultdict(list)
    for r in proj_rows:
        g = label_group(r)
        if g is None:
            continue
        strata[(r["category"], method_group(r), g)].append(r)
    out = []
    for key in sorted(strata):
        items = sorted(strata[key], key=lambda r: _h(seed, r["project_id"], r["feature_id"]))[:k]
        for r in items:
            f = cb.feature(r["feature_id"])
            out.append({"review_id": "R-" + _h(seed, r["project_id"], r["feature_id"])[:10], "stratum": "/".join(key),
                        "project_id": r["project_id"], "feature_id": r["feature_id"], "feature_name": r["feature_name"],
                        "category": r["category"], "label": r["label"], "method": r["method"],
                        "machine_availability": r["availability"], "machine_verification": r["verification"],
                        "machine_evidence_grade": r.get("evidence_grade", ""), "machine_rationale": (r.get("rationale") or "")[:500],
                        "evidence_ref": f"projects/{r['project_id']}/report.md; observations.jsonl obs_ids={(r.get('obs_ids') or '').split(';')[:5]}",
                        "review_instructions": (f.get("manual_review", {}) or {}).get("instructions") or f.get("definition", "")})
    n_h = int(rc.get("human_only_per_project", 1))
    projects = sorted({r["project_id"] for r in proj_rows if r["availability"] != "not_applicable"})
    for pid in projects:
        order = sorted(HUMAN_CHECKS, key=lambda h: _h(seed, pid, h[0]))[:n_h]
        for hid, name in order:
            out.append({"review_id": "R-" + _h(seed, pid, hid)[:10], "stratum": f"HUMAN/{hid}", "project_id": pid,
                        "feature_id": hid, "feature_name": name, "category": "HUMAN", "label": "human_judgement", "method": "manual",
                        "machine_availability": "", "machine_verification": "not_tested", "machine_evidence_grade": "",
                        "machine_rationale": "requires human judgement; no machine label exists",
                        "evidence_ref": f"projects/{pid}/report.md", "review_instructions": name + ". Record pass/partial/fail with notes."})
    return out


def write_queue(path: Path, rows: list[dict]) -> None:
    """Write the queue; if a filled queue already exists, keep reviewer columns for matching review_ids."""
    existing = {}
    if path.exists():
        with open(path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                existing[r["review_id"]] = r
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=REVIEW_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            old = existing.get(r["review_id"], {})
            merged = {**{k: "" for k in REVIEW_FIELDS}, **r}
            for k in REVIEW_FIELDS:
                if k.startswith(("reviewer_", "adjudicat", "disagreement")) and old.get(k):
                    merged[k] = old[k]
            w.writerow(merged)


def read_review(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def cohen_kappa(a: list[str], b: list[str]) -> dict:
    n = len(a)
    if n == 0:
        return {"n": 0, "kappa": None, "percent_agreement": None, "note": "no doubly-reviewed items"}
    agree = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb_ = Counter(a), Counter(b)
    cats = set(ca) | set(cb_)
    pe = sum((ca[c] / n) * (cb_[c] / n) for c in cats)
    if len(cats) < 2 or pe >= 1:
        return {"n": n, "kappa": None, "percent_agreement": round(agree, 4), "note": "kappa undefined (single category)"}
    return {"n": n, "kappa": round((agree - pe) / (1 - pe), 4), "percent_agreement": round(agree, 4), "categories": sorted(cats)}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((c - m) / d, 4), round((c + m) / d, 4)


def agreement(rows: list[dict]) -> dict:
    out = {}
    for field in ("availability", "verification"):
        pairs = [(r[f"reviewer_1_{field}"].strip(), r[f"reviewer_2_{field}"].strip()) for r in rows
                 if r.get(f"reviewer_1_{field}", "").strip() and r.get(f"reviewer_2_{field}", "").strip()]
        out[field] = cohen_kappa([a for a, _ in pairs], [b for _, b in pairs])
    out["statistic"] = "Cohen's kappa (nominal categories), two reviewers; percent agreement shown alongside"
    return out


def reference_label(r: dict, field: str) -> str:
    adj = (r.get(f"adjudicated_{field}") or "").strip()
    if adj:
        return adj
    a, b = (r.get(f"reviewer_1_{field}") or "").strip(), (r.get(f"reviewer_2_{field}") or "").strip()
    return a if a and a == b else ""


def metrics(rows: list[dict], min_per_class: int) -> dict:
    ref = [(r["machine_availability"], reference_label(r, "availability")) for r in rows if r.get("category") != "HUMAN"]
    ref = [(m, h) for m, h in ref if m in ("present", "not_found_in_scope") and h in ("present", "not_found_in_scope")]
    pos = sum(1 for _, h in ref if h == "present")
    neg = sum(1 for _, h in ref if h == "not_found_in_scope")
    out = {"availability_reference_items": len(ref), "reference_positives": pos, "reference_negatives": neg,
           "note": "stratified review sample; unweighted sample statistics, not population estimates"}
    if pos < min_per_class or neg < min_per_class:
        out["status"] = f"insufficient adjudicated reference sample (need >= {min_per_class} positives and negatives); metrics not computed"
        return out
    tp = sum(1 for m, h in ref if m == "present" and h == "present")
    fp = sum(1 for m, h in ref if m == "present" and h == "not_found_in_scope")
    fn = sum(1 for m, h in ref if m == "not_found_in_scope" and h == "present")
    out.update(status="computed", tp=tp, fp=fp, fn=fn,
               precision=round(tp / (tp + fp), 4) if tp + fp else None, precision_ci95=wilson(tp, tp + fp),
               recall=round(tp / (tp + fn), 4) if tp + fn else None, recall_ci95=wilson(tp, tp + fn))
    vref = [(r["machine_verification"], reference_label(r, "verification")) for r in rows]
    vref = [(m, h) for m, h in vref if m in ("pass", "partial", "fail") and h in ("pass", "partial", "fail")]
    if vref:
        acc = sum(1 for m, h in vref if m == h)
        out["verification_items"] = len(vref)
        out["verification_accuracy"] = round(acc / len(vref), 4)
        out["verification_accuracy_ci95"] = wilson(acc, len(vref))
        out["verification_confusion"] = {f"{m}->{h}": c for (m, h), c in Counter(vref).items()}
    return out


def mark_disagreements(rows: list[dict]) -> list[dict]:
    for r in rows:
        d = []
        for field in ("availability", "verification"):
            a, b = (r.get(f"reviewer_1_{field}") or "").strip(), (r.get(f"reviewer_2_{field}") or "").strip()
            if a and b and a != b:
                d.append(field)
        r["disagreement"] = ";".join(d)
    return rows


def apply_reviews(results: Path, review_path: Path) -> dict:
    rows = mark_disagreements(read_review(review_path))
    by = {(r["project_id"], r["feature_id"]): r for r in rows}
    src = results / "project_features.csv"
    with open(src, encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        feats = list(rd)
        fields = list(rd.fieldnames or [])
    extra = ["review_id", "human_availability", "human_verification", "review_status", "human_evidence_grade"]
    n = 0
    for f in feats:
        r = by.get((f["project_id"], f["feature_id"])) if f["scope"] == "project" else None
        f.update({k: "" for k in extra})
        if not r:
            continue
        ha, hv = reference_label(r, "availability"), reference_label(r, "verification")
        status = "adjudicated" if r.get("adjudicated_availability") or r.get("adjudicated_verification") else \
                 "agreed" if (ha or hv) else "disagreement" if r.get("disagreement") else \
                 "single_reviewer" if r.get("reviewer_1_availability") or r.get("reviewer_1_verification") else "pending"
        f.update(review_id=r["review_id"], human_availability=ha, human_verification=hv, review_status=status,
                 human_evidence_grade="human_validation" if (ha or hv) else "")
        n += 1
    out = results / "project_features_reviewed.csv"
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields + extra)
        w.writeheader()
        w.writerows(feats)
    with open(review_path.with_name(review_path.stem + "_checked.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=REVIEW_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    bad = [r["review_id"] for r in rows for fld in ("reviewer_1_availability", "reviewer_2_availability", "adjudicated_availability")
           if r.get(fld) and r[fld] not in AV] + \
          [r["review_id"] for r in rows for fld in ("reviewer_1_verification", "reviewer_2_verification", "adjudicated_verification")
           if r.get(fld) and r[fld] not in VE]
    return {"rows_linked": n, "output": str(out), "invalid_labels": sorted(set(bad)),
            "machine_labels_preserved": True}
