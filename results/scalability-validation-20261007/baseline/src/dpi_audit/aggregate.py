"""Aggregate unit-level observations into interface-level and project-level feature rows
using only the codebook's predeclared rules.

Availability precedence within one method: present > not_found_in_scope > not_applicable
> unknown. Across methods: present from either method wins; otherwise a runtime
not_applicable (applicability determined on the deployed UI) takes precedence over a
static not_found; otherwise not_found from either method; otherwise unknown.

Verification uses only tested units (pass/partial/fail); not_tested units are counted but
never converted into failures. Unknown and not-applicable are never turned into zeros.
"""
from __future__ import annotations

from .inputs import DEPLOYED_UI_TYPES, NON_WEB_TYPES, PROJECT_INFO_TYPES
from .observations import Observation

GRADE_RANK = {"direct_observation": 3, "human_validation": 4, "static_indication": 2, "heuristic_inference": 1}
STATIC_VERIFICATION_FEATURES = {"LANG-05"}


def combine_availability(obs: list[Observation]) -> tuple[str, list[Observation]]:
    if not obs:
        return "unknown", []
    for state in ("present", "not_found_in_scope"):
        sel = [o for o in obs if o.availability == state]
        if sel:
            return state, sel
    if all(o.availability == "not_applicable" for o in obs):
        return "not_applicable", obs
    return "unknown", [o for o in obs if o.availability == "unknown"]


def combine_verification(obs: list[Observation], rule: str) -> tuple[str, dict]:
    vals = [o.verification for o in obs]
    tested = [v for v in vals if v != "not_tested"]
    eligible = [o for o in obs if o.availability != "not_applicable"]
    counts = {"units_eligible": len(eligible), "units_tested": len(tested), "units_pass": tested.count("pass"),
              "units_partial": tested.count("partial"), "units_fail": tested.count("fail"),
              "units_not_tested": len([o for o in eligible if o.verification == "not_tested"])}
    if not tested:
        return "not_tested", counts
    if rule == "any_fail_is_fail" and "fail" in tested:
        return "fail", counts
    if all(v == "pass" for v in tested):
        return "pass", counts
    if all(v == "fail" for v in tested):
        return "fail", counts
    return "partial", counts


def best_grade(obs: list[Observation]) -> str:
    if not obs:
        return ""
    return max(obs, key=lambda o: GRADE_RANK.get(o.evidence_grade, 0)).evidence_grade


def _summarise(fid: str, obs: list[Observation], limit: int = 3) -> str:
    seen, parts = set(), []
    for o in obs:
        key = o.rationale[:120]
        if key in seen:
            continue
        seen.add(key)
        where = o.unit.get("page_url") or o.unit.get("source") or ""
        vp = o.unit.get("viewport")
        parts.append(f"[{o.method}{'/' + vp if vp else ''}] {o.rationale}" + (f" @ {where}" if where and o.method == "runtime" else ""))
        if len(parts) >= limit:
            break
    more = len({o.rationale[:120] for o in obs}) - len(parts)
    return " | ".join(parts) + (f" | (+{more} more)" if more > 0 else "")


class Aggregator:
    def __init__(self, codebook, cfg: dict, run_ctx: dict):
        self.cb = codebook
        self.cfg = cfg
        self.ctx = run_ctx

    def _feature_meta(self, f: dict) -> dict:
        return {"feature_id": f["id"], "feature_name": f["name"], "category": f["category"], "label": f["label"],
                "requirement_level": f["requirement_level"], "availability_semantics": f["availability_semantics"],
                "wcag_refs": ";".join(str(m["sc"]) for m in f.get("wcag", []) or [])}

    def interface_rows(self, project, iface_info: dict, obs: list[Observation], mode: str) -> list[dict]:
        rows = []
        by_feat: dict[str, list[Observation]] = {}
        for o in obs:
            by_feat.setdefault(o.feature_id, []).append(o)
        for f in self.cb.features:
            fid = f["id"]
            fo = by_feat.get(fid, [])
            row = self._base_row(project.project_id, "interface", iface_info["interface_id"], iface_info["interface_type"], f)
            reason = self._interface_gap_reason(f, iface_info, mode)
            if iface_info["interface_type"] in NON_WEB_TYPES:
                row.update(availability="not_applicable", verification="not_tested",
                           applicability_reason=f"declared interface_type {iface_info['interface_type']}: browser UI features do not apply",
                           method="none", rationale="not applicable to a non-web interface")
                rows.append(row)
                continue
            if not fo:
                row.update(availability="unknown", verification="not_tested", method="none", rationale=reason or "no observation recorded",
                           coverage_limits=reason or "")
                rows.append(row)
                continue
            av, decisive = combine_availability(fo)
            agg = f.get("aggregation", {})
            ver, counts = combine_verification(fo, agg.get("verification", "mixed_is_partial"))
            min_pages = int((f.get("min_coverage") or {}).get("runtime_pages", 1))
            pages = {o.unit.get("page_url") for o in fo if o.method == "runtime" and o.unit.get("kind") == "page"}
            cov_note = ""
            if iface_info["kind"] == "web" and pages and len(pages) < min_pages and ver != "not_tested" and not any(o.unit.get("kind") == "interface" for o in fo):
                cov_note = f"below minimum coverage ({len(pages)} < {min_pages} pages)"
                ver = "not_tested"
            row.update(availability=av, verification=ver, **counts,
                       availability_static=av if fo[0].method == "static" else "",
                       availability_runtime=av if fo[0].method == "runtime" else "",
                       method=fo[0].method, evidence_grade=best_grade(decisive or fo),
                       applicability_reason=next((o.applicability_reason for o in fo if o.applicability_reason), ""),
                       rationale=_summarise(fid, decisive or fo), manual_review_flag=bool(f.get("manual_review", {}).get("required")) or any(o.manual_review for o in fo),
                       obs_ids=";".join(o.obs_id for o in fo[:60]), n_observations=len(fo),
                       pages_with_observations=len(pages), coverage_limits="; ".join(x for x in (cov_note, iface_info.get("coverage_note", "")) if x))
            rows.append(row)
        return rows

    def _interface_gap_reason(self, f: dict, info: dict, mode: str) -> str:
        if info["kind"] == "source":
            if info.get("status") != "ok":
                return f"source not inspected: {info.get('status')} ({info.get('reason', '')})"
            if mode == "runtime":
                return "static analysis not run (mode=runtime)"
            if not (f.get("detectors", {}) or {}).get("static"):
                return "no static detector for this feature (runtime-only)"
            return "static detector produced no observation"
        if mode == "static":
            return "runtime checks not run (mode=static)"
        if info.get("status") != "ok":
            return f"interface not assessable at runtime: {info.get('status')} ({info.get('reason', '')})"
        if not (f.get("detectors", {}) or {}).get("runtime"):
            return "no runtime detector for this feature"
        skipped = info.get("states_skipped") or {}
        return "runtime state not run" + (f" ({'; '.join(f'{k}: {v}' for k, v in skipped.items())})" if skipped else "")

    def _base_row(self, pid, scope, iid, itype, f) -> dict:
        r = {"project_id": pid, "scope": scope, "interface_id": iid, "interface_type": itype}
        r.update(self._feature_meta(f))
        r.update({"availability": "", "availability_static": "", "availability_runtime": "", "verification": "",
                  "units_eligible": 0, "units_tested": 0, "units_pass": 0, "units_partial": 0, "units_fail": 0, "units_not_tested": 0,
                  "method": "", "evidence_grade": "", "applicability_reason": "", "rationale": "", "coverage_limits": "",
                  "rollup_basis": "", "manual_review_flag": bool(f.get("manual_review", {}).get("required")), "obs_ids": "",
                  "n_observations": 0, "pages_with_observations": 0})
        r.update(self.ctx)
        return r

    def project_rows(self, project, ui_status: tuple[str, str], iface_infos: list[dict], obs: list[Observation], mode: str) -> list[dict]:
        rows = []
        status, why = ui_status
        sources = [i for i in iface_infos if i["kind"] == "source"]
        webs = [i for i in iface_infos if i["kind"] == "web" and i["interface_type"] not in NON_WEB_TYPES]
        deployed = [i for i in webs if i["interface_type"] in DEPLOYED_UI_TYPES]
        unknown_t = [i for i in webs if i["interface_type"] == "unknown"]
        info_only = [i for i in webs if i["interface_type"] in PROJECT_INFO_TYPES]
        for f in self.cb.features:
            fid = f["id"]
            row = self._base_row(project.project_id, "project", "*", "project", f)
            if status == "not_applicable":
                row.update(availability="not_applicable", verification="not_tested", method="none", applicability_reason=why,
                           rationale="project declared non-web; UI features not applicable (not scored as zero)")
                rows.append(row)
                continue
            rule = (f.get("aggregation") or {}).get("rollup", "rollup_service_first")
            if rule == "rollup_any_web":
                basis = webs
                basis_note = "any supplied web interface"
            elif deployed:
                basis, basis_note = deployed, "deployed service/public-information/data-portal interface(s)"
            elif unknown_t:
                basis, basis_note = unknown_t, "web interface(s) of unspecified type (not declared as service UI)"
            else:
                basis = []
                basis_note = ("no deployed UI supplied; informational/documentation sites are reported at interface level only "
                              "and never substituted for the product UI") if info_only else "no web interface supplied"
            basis_ids = {i["interface_id"] for i in basis}
            src_ids = {i["interface_id"] for i in sources}
            st_obs = [o for o in obs if o.feature_id == fid and o.interface_id in src_ids and o.method == "static"]
            rt_obs = [o for o in obs if o.feature_id == fid and o.interface_id in basis_ids and o.method == "runtime"]
            av_s, dec_s = combine_availability(st_obs) if st_obs else ("", [])
            av_r, dec_r = combine_availability(rt_obs) if rt_obs else ("", [])
            if av_s == "present" or av_r == "present":
                av = "present"
                dec = (dec_r if av_r == "present" else []) + (dec_s if av_s == "present" else [])
            elif av_r == "not_applicable":
                av, dec = "not_applicable", dec_r
            elif "not_found_in_scope" in (av_s, av_r):
                av = "not_found_in_scope"
                dec = (dec_r if av_r == "not_found_in_scope" else []) + (dec_s if av_s == "not_found_in_scope" else [])
            elif av_s == "not_applicable" and not rt_obs:
                av, dec = "not_applicable", dec_s
            else:
                av, dec = "unknown", []
            agg = (f.get("aggregation") or {}).get("verification", "mixed_is_partial")
            ver, counts = combine_verification(rt_obs, agg)
            vmethod = "runtime" if counts["units_tested"] else ""
            if not counts["units_tested"] and fid in STATIC_VERIFICATION_FEATURES and st_obs:
                ver, counts = combine_verification(st_obs, agg)
                vmethod = "static" if counts["units_tested"] else ""
            methods = sorted({o.method for o in (st_obs + rt_obs) if o.availability != "unknown"})
            method = "+".join(methods) if methods else "none"
            gaps = []
            if not rt_obs:
                if mode == "static":
                    gaps.append("runtime not run (mode=static)")
                elif not basis:
                    gaps.append(basis_note)
                else:
                    bad = [i for i in basis if i.get("status") != "ok"]
                    gaps.append(("runtime basis not assessable: " + ", ".join(f"{i['interface_id']}={i.get('status')}" for i in bad)) if bad
                                else "no runtime observation for this feature")
            if not st_obs and sources and mode != "runtime" and (f.get("detectors", {}) or {}).get("static"):
                bad = [i for i in sources if i.get("status") != "ok"]
                gaps.append(("source not inspected: " + ", ".join(f"{i['interface_id']}={i.get('status')}" for i in bad)) if bad else "no static observation")
            for i in basis + sources:
                if i.get("coverage_note"):
                    gaps.append(f"{i['interface_id'].split('::')[-1]}: {i['coverage_note']}")
            all_obs = rt_obs + st_obs
            row.update(availability=av, availability_static=av_s or ("unknown" if sources else ""), availability_runtime=av_r or ("unknown" if basis else ""),
                       verification=ver, verification_method=vmethod, **counts, method=method, evidence_grade=best_grade(dec),
                       applicability_reason=next((o.applicability_reason for o in (dec or all_obs) if o.applicability_reason), "") if av == "not_applicable" else "",
                       rationale=_summarise(fid, dec or all_obs) if (dec or all_obs) else "; ".join(gaps) or "no observations",
                       coverage_limits="; ".join(dict.fromkeys(gaps)), rollup_basis=f"{basis_note}: {', '.join(sorted(basis_ids)) or '-'}" + (f"; static: {', '.join(sorted(src_ids))}" if src_ids else ""),
                       manual_review_flag=bool(f.get("manual_review", {}).get("required")) or any(o.manual_review for o in all_obs)
                       or (av == "present" and dec and all(o.evidence_grade == "heuristic_inference" for o in dec)),
                       obs_ids=";".join(o.obs_id for o in all_obs[:60]), n_observations=len(all_obs),
                       pages_with_observations=len({o.unit.get("page_url") for o in rt_obs if o.unit.get("kind") == "page"}),
                       heterogeneity=_heterogeneity(rt_obs))
            rows.append(row)
        return rows


def _heterogeneity(obs: list[Observation]) -> str:
    by_vp: dict[str, dict] = {}
    for o in obs:
        vp = o.unit.get("viewport") or "-"
        d = by_vp.setdefault(vp, {})
        d[o.verification] = d.get(o.verification, 0) + 1
    return "; ".join(f"{vp}: " + ",".join(f"{k}={v}" for k, v in sorted(d.items())) for vp, d in sorted(by_vp.items()))
