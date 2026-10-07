"""Runtime decision rules: raw facts (snapshot, axe results, interaction results) ->
unit-level observations, exactly as written in codebook.yaml decision_rules.

Units: (page URL x viewport x state). Interface-level units (cross-page comparisons,
link checks, homepage-only tests) use unit kind "interface" or the homepage URL.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import lang as L
from ..observations import Observation, dom_evidence, tool_evidence

GENERIC_EXTRA = set()
PERSONAL_RX = re.compile(r"(^|[_\-\s])(name|first.?name|last.?name|full.?name|email|e-?mail|phone|mobile|tel|telephone|address|street|"
                         r"city|pin.?code|postal|zip|username|user.?name|login.?id|dob|birth|country)($|[_\-\s])", re.I)
AUTOCOMPLETE_TOKENS = {
    "on", "name", "honorific-prefix", "given-name", "additional-name", "family-name", "honorific-suffix", "nickname", "email",
    "username", "new-password", "current-password", "one-time-code", "organization-title", "organization", "street-address",
    "address-line1", "address-line2", "address-line3", "address-level4", "address-level3", "address-level2", "address-level1",
    "country", "country-name", "postal-code", "cc-name", "cc-given-name", "cc-additional-name", "cc-family-name", "cc-number",
    "cc-exp", "cc-exp-month", "cc-exp-year", "cc-csc", "cc-type", "transaction-currency", "transaction-amount", "language",
    "bday", "bday-day", "bday-month", "bday-year", "sex", "url", "photo", "tel", "tel-country-code", "tel-national",
    "tel-area-code", "tel-local", "tel-extension", "impp", "webauthn",
}
REQUIRED_CUE = re.compile(r"\*|required|mandatory|compulsory|अनिवार्य|आवश्यक|ज़रूरी|जरूरी|obligatoire|obligatorio", re.I)
FORM_REQ_INSTR = re.compile(r"(\*|asterisk)[^.]{0,40}(required|mandatory|अनिवार्य|आवश्यक)|all fields (are )?(required|mandatory)|"
                            r"(required|mandatory) fields? (are )?marked|सभी (फ़ील्ड|क्षेत्र)", re.I)
HINT_RX = re.compile(r"format|e\.g\.|eg[:.]|example|\bdd\b|\bmm\b|yyyy|digit|characters?|\d+\s*(digit|अंक)|उदाहरण|प्रारूप|अंक", re.I)
SOFT404 = re.compile(r"\b(404|not found|page (does not|doesn't) exist|page not available)\b|पृष्ठ नहीं मिला|पेज नहीं मिला", re.I)
ERROR_CUE = re.compile(r"error|invalid|required|please (enter|fill|select|provide)|must|अमान्य|त्रुटि|अनिवार्य|आवश्यक|कृपया", re.I)

AXE_WCAG_PREFIX = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa")


@dataclass
class PCtx:
    project_id: str
    interface_id: str
    interface_type: str
    url: str
    viewport: str
    state: str
    cfg: dict
    meta: dict
    is_home: bool = False
    extra_unit: dict = field(default_factory=dict)

    def unit(self, kind: str = "page") -> dict:
        u = {"kind": kind, "page_url": self.url, "viewport": self.viewport, "state": self.state}
        u.update(self.extra_unit)
        return u


def mk(pc: PCtx, fid: str, check: str, availability: str, verification: str, rationale: str,
       grade: str = "direct_observation", evidence=None, meas=None, manual: bool = False, appl: str = "",
       kind: str = "page") -> Observation:
    return Observation(project_id=pc.project_id, interface_id=pc.interface_id, interface_type=pc.interface_type,
                       feature_id=fid, check_id=check, method="runtime", evidence_grade=grade, availability=availability,
                       verification=verification, rationale=rationale, unit=pc.unit(kind),
                       applicability_reason=appl, evidence=(evidence or [])[:10], measurements=meas or {},
                       manual_review=manual)


# ------------------------------------------------------------------ axe helpers
def _is_wcag(tags: list[str]) -> bool:
    return any(t in AXE_WCAG_PREFIX for t in tags)


def axe_outcomes(axe: dict, rules: list[str]) -> dict:
    out = {}
    idx = {}
    for kind in ("violations", "incomplete", "passes"):
        for r in axe.get(kind, []):
            idx.setdefault(r["id"], (kind, r))
    inap = {r["id"]: r for r in axe.get("inapplicable", [])}
    for rid in rules:
        if rid in idx:
            kind, r = idx[rid]
            out[rid] = {"outcome": {"violations": "violation", "incomplete": "incomplete", "passes": "pass"}[kind],
                        "nodes": r.get("node_count", 0), "tags": r.get("tags", []), "rec": r}
        elif rid in inap:
            out[rid] = {"outcome": "inapplicable", "nodes": 0, "tags": inap[rid].get("tags", []), "rec": None}
        else:
            out[rid] = {"outcome": "not_run", "nodes": 0, "tags": [], "rec": None}
    return out


def axe_ev(outs: dict) -> list[dict]:
    ev = []
    for rid, o in outs.items():
        if o["outcome"] in ("violation", "incomplete") and o["rec"]:
            nodes = [{"target": n.get("target"), "html": n.get("html"), "summary": n.get("summary"), "data": n.get("data")}
                     for n in o["rec"].get("nodes", [])[:5]]
            ev.append(tool_evidence("axe-core", rid, o["outcome"], nodes, {"node_count": o["nodes"], "help_url": o["rec"].get("helpUrl")}))
        elif o["outcome"] == "pass":
            ev.append(tool_evidence("axe-core", rid, "pass", [], {"node_count": o["nodes"]}))
    return ev


def axe_counts(outs: dict) -> dict:
    return {rid: {"outcome": o["outcome"], "nodes": o["nodes"]} for rid, o in outs.items()}


def axe_generic(pc: PCtx, fid: str, axe: dict, rules: list[str], manual: bool = False) -> Observation:
    outs = axe_outcomes(axe, rules)
    viol = [r for r, o in outs.items() if o["outcome"] == "violation" and _is_wcag(o["tags"])]
    viol_bp = [r for r, o in outs.items() if o["outcome"] == "violation" and not _is_wcag(o["tags"])]
    passes = [r for r, o in outs.items() if o["outcome"] == "pass"]
    inc = [r for r, o in outs.items() if o["outcome"] == "incomplete"]
    ev, meas = axe_ev(outs), {"axe": axe_counts(outs)}
    if viol:
        n = sum(outs[r]["nodes"] for r in viol)
        return mk(pc, fid, "rt.axe", "present", "fail", f"axe violation(s): {', '.join(viol)} ({n} node(s))", evidence=ev, meas=meas,
                  manual=manual or bool(inc))
    if passes or viol_bp:
        why = f"no violations of mapped WCAG-tagged axe rules; passes: {', '.join(passes) or '-'}"
        if viol_bp:
            why += f"; best-practice findings recorded: {', '.join(viol_bp)}"
        if inc:
            why += f"; incomplete (needs review): {', '.join(inc)}"
        return mk(pc, fid, "rt.axe", "present", "pass", why, evidence=ev, meas=meas, manual=manual or bool(inc))
    if inc:
        return mk(pc, fid, "rt.axe", "present", "not_tested", f"axe returned only incomplete results ({', '.join(inc)}); manual review",
                  evidence=ev, meas=meas, manual=True)
    return mk(pc, fid, "rt.axe", "not_applicable", "not_tested", "all mapped axe rules inapplicable on this page", evidence=ev, meas=meas,
              appl="subject not present on page")


def kw_rx(words: list[str]) -> re.Pattern:
    parts = []
    for w in words:
        e = re.escape(w)
        parts.append(rf"(?<![\w]){e}(?![\w])" if re.fullmatch(r"[A-Za-z +\-]+", w) else e)
    return re.compile("|".join(parts), re.I)


# ------------------------------------------------------------------ initial state
def initial_state(pc: PCtx, snap: dict, axe: dict, desktop: bool) -> list[Observation]:
    out: list[Observation] = []
    cfg = pc.cfg
    text = " ".join(s["t"] for s in snap.get("text", []))
    words = len(re.findall(r"[^\W\d_]+", text))
    out.append(_lang07(pc, snap, axe, text))
    out.append(_lang08(pc, snap, axe))
    out.append(_nav01(pc, snap, axe))
    out.append(_nav03(pc, snap, axe))
    out.append(axe_generic(pc, "NAV-04", axe, ["button-name", "input-button-name", "aria-command-name", "summary-name", "label-content-name-mismatch"]))
    out.append(axe_generic(pc, "KBD-07", axe, cfg["_rules"]["KBD-07"], manual=True))
    out.append(_kbd08(pc, snap, axe, words))
    out.append(_kbd09(pc, snap, axe))
    out.append(_kbd10(pc, snap, axe))
    out.append(axe_generic(pc, "VIS-01", axe, ["color-contrast"]))
    out.append(axe_generic(pc, "VIS-07", axe, ["target-size"]))
    out.append(axe_generic(pc, "VIS-08", axe, ["link-in-text-block"], manual=True))
    if not desktop:
        out.append(_vis03(pc, snap, cfg))
        return out
    out.append(_nav07(pc, snap))
    out.append(_nav13(pc, snap))
    out.append(_kbd12(pc, snap))
    out.append(_kbd13(pc, snap))
    out.append(_vis02(pc, snap))
    out.append(_vis10(pc, snap, axe))
    out += _media(pc, snap, axe)
    out.append(_vis16(pc, snap))
    out.append(_form01(pc, snap))
    out.append(_form02(pc, snap))
    out.append(_form06(pc, snap, axe))
    out.append(_form09(pc, snap, axe, text))
    out.append(_form10(pc, snap, text))
    out += _text_metrics(pc, snap, text, words)
    out.append(_trust06(pc, snap))
    out += _keyword_features(pc, snap)
    return out


def _lang07(pc, snap, axe, text):
    outs = axe_outcomes(axe, ["html-has-lang", "html-lang-valid", "html-xml-lang-mismatch"])
    ev = axe_ev(outs) + [dom_evidence(pc.url, "html", f'<html lang="{snap.get("html_lang")}">', pc.viewport, pc.state)]
    viol = [r for r, o in outs.items() if o["outcome"] == "violation"]
    lang = snap.get("html_lang")
    cons, why = L.consistent_with_script(lang, text)
    meas = {"html_lang": lang, "script_shares": L.script_shares(text), "consistency": why, "axe": axe_counts(outs)}
    avail = "present" if lang else "not_found_in_scope"
    if viol:
        return mk(pc, "LANG-07", "rt.axe+script", avail, "fail", f"axe: {', '.join(viol)}", evidence=ev, meas=meas)
    if cons is False:
        return mk(pc, "LANG-07", "rt.axe+script", avail, "fail", why, evidence=ev, meas=meas)
    if not lang:
        return mk(pc, "LANG-07", "rt.axe+script", avail, "fail", "no lang attribute on html", evidence=ev, meas=meas)
    return mk(pc, "LANG-07", "rt.axe+script", avail, "pass", f"valid lang={lang}; {why}", evidence=ev, meas=meas)


def _lang08(pc, snap, axe):
    page_lang = snap.get("html_lang")
    text = " ".join(s["t"] for s in snap.get("text", [])[:600])
    ref = L.likely_script(page_lang) or L.dominant_script(text, 0.5)
    mixed, ok, bad = 0, 0, []
    for s in snap.get("text", []):
        if len(re.findall(r"[^\W\d_]", s["t"])) < 3:
            continue
        dom = L.dominant_script(s["t"], 0.6)
        if not dom or not ref or dom == ref or dom == "Other":
            continue
        mixed += 1
        if s.get("lang") and L.likely_script(s["lang"]) == dom:
            ok += 1
        elif len(bad) < 6:
            bad.append(dom_evidence(pc.url, s["p"], s["t"][:120], pc.viewport, pc.state, f"script {dom}; lang ancestry {s.get('lang')}"))
    outs = axe_outcomes(axe, ["valid-lang"])
    viol = outs["valid-lang"]["outcome"] == "violation"
    meas = {"mixed_script_passages": mixed, "with_matching_lang": ok, "reference_script": ref, "axe": axe_counts(outs)}
    if not mixed and not viol:
        return mk(pc, "LANG-08", "rt.lang_parts", "not_applicable", "not_tested", "no passages in a script different from the page language",
                  meas=meas, appl="no mixed-script passages on this page", manual=True)
    if viol or ok == 0:
        why = "axe valid-lang violation" if viol else f"0/{mixed} mixed-script passages carry a matching lang"
        return mk(pc, "LANG-08", "rt.lang_parts", "present", "fail", why, evidence=bad + axe_ev(outs), meas=meas, manual=True)
    ver = "pass" if ok == mixed else "partial"
    return mk(pc, "LANG-08", "rt.lang_parts", "present", ver, f"{ok}/{mixed} mixed-script passages carry a matching lang", evidence=bad, meas=meas, manual=True)


def _nav01(pc, snap, axe):
    lm = snap.get("landmarks", {})
    navs = [n for n in lm.get("nav", [])]
    links = snap.get("links", [])
    header_links = sum(1 for l in links if l.get("region") in ("header", "nav") and l.get("visible"))
    internal = sum(1 for l in links if l.get("internal"))
    nav_expected = header_links >= 3 or internal >= 10
    outs = axe_outcomes(axe, ["landmark-one-main", "region", "bypass", "landmark-no-duplicate-main", "landmark-main-is-top-level"])
    meas = {"main": lm.get("main", 0), "nav": len(navs), "header_links": header_links, "axe": axe_counts(outs)}
    ev = axe_ev(outs) + [dom_evidence(pc.url, n["selector"], f"nav label={n.get('label')!r} links={len(n['links'])}", pc.viewport, pc.state) for n in navs[:3]]
    if lm.get("main", 0) == 0:
        return mk(pc, "NAV-01", "rt.landmarks", "not_found_in_scope", "fail", "no main landmark", evidence=ev, meas=meas)
    issues = []
    if lm.get("main", 0) > 1:
        issues.append("more than one main landmark")
    if nav_expected and not navs:
        issues.append("navigation links present but no navigation landmark")
    bad_axe = [r for r in ("bypass", "region", "landmark-no-duplicate-main", "landmark-main-is-top-level") if outs[r]["outcome"] == "violation"]
    if bad_axe:
        issues.append("axe: " + ", ".join(bad_axe))
    if issues:
        return mk(pc, "NAV-01", "rt.landmarks", "present", "partial", "; ".join(issues), evidence=ev, meas=meas)
    return mk(pc, "NAV-01", "rt.landmarks", "present", "pass", f"main landmark and {len(navs)} navigation landmark(s)", evidence=ev, meas=meas)


def _nav03(pc, snap, axe):
    links = [l for l in snap.get("links", []) if l.get("visible")]
    outs = axe_outcomes(axe, ["link-name", "area-alt"])
    generic_words = {w.lower() for w in pc.cfg["keywords"]["generic_link_text"]}
    generic = [l for l in links if (l.get("name") or "").strip().lower().strip(".:»›>") in generic_words
               and not l.get("aria_describedby")]
    meas = {"links": len(links), "generic_names": len(generic), "axe": axe_counts(outs)}
    ev = axe_ev(outs) + [dom_evidence(pc.url, l["selector"], l.get("name") or "", pc.viewport, pc.state, "generic link name") for l in generic[:4]]
    if not links and all(o["outcome"] in ("inapplicable", "not_run") for o in outs.values()):
        return mk(pc, "NAV-03", "rt.links", "not_applicable", "not_tested", "no links", meas=meas, appl="no links on page")
    viol = [r for r, o in outs.items() if o["outcome"] == "violation"]
    if viol:
        return mk(pc, "NAV-03", "rt.links", "present", "fail", f"axe: {', '.join(viol)}; {len(generic)} generic link name(s)", evidence=ev, meas=meas)
    if generic:
        return mk(pc, "NAV-03", "rt.links", "present", "partial", f"{len(generic)} generic link name(s) without programmatic context (heuristic)",
                  grade="heuristic_inference", evidence=ev, meas=meas, manual=True)
    return mk(pc, "NAV-03", "rt.links", "present", "pass", f"{len(links)} visible links named; no generic names", evidence=ev, meas=meas)


def _kbd08(pc, snap, axe, words):
    hs = snap.get("headings", [])
    outs = axe_outcomes(axe, ["empty-heading", "heading-order", "page-has-heading-one"])
    levels = [h["level"] for h in hs]
    skipped = sum(1 for a, b in zip(levels, levels[1:]) if b > a + 1)
    meas = {"headings": len(hs), "h1": levels.count(1), "skipped_levels": skipped, "words": words, "outline": [f"h{h['level']} {h['text'][:40]}" for h in hs[:12]],
            "axe": axe_counts(outs)}
    ev = axe_ev(outs)
    if outs["empty-heading"]["outcome"] == "violation":
        return mk(pc, "KBD-08", "rt.headings", "present" if hs else "not_found_in_scope", "fail", "empty heading(s) (axe)", evidence=ev, meas=meas)
    if not hs:
        if words > 150:
            return mk(pc, "KBD-08", "rt.headings", "not_found_in_scope", "fail", f"no headings on a page with {words} words", evidence=ev, meas=meas, manual=True)
        return mk(pc, "KBD-08", "rt.headings", "not_found_in_scope", "not_tested", f"no headings; only {words} words of text", evidence=ev, meas=meas)
    issues = []
    if skipped:
        issues.append(f"{skipped} skipped heading level(s)")
    if not levels.count(1):
        issues.append("no h1")
    if outs["heading-order"]["outcome"] == "violation":
        issues.append("axe heading-order (best practice)")
    if issues:
        return mk(pc, "KBD-08", "rt.headings", "present", "partial", "; ".join(issues), evidence=ev, meas=meas)
    return mk(pc, "KBD-08", "rt.headings", "present", "pass", f"{len(hs)} headings, h1 present, no skipped levels", evidence=ev, meas=meas)


def _kbd09(pc, snap, axe):
    imgs = [i for i in snap.get("images", []) if i.get("visible")]
    rules = ["image-alt", "input-image-alt", "role-img-alt", "svg-img-alt", "area-alt", "object-alt", "image-redundant-alt"]
    outs = axe_outcomes(axe, rules)
    dec = sum(1 for i in imgs if i["decorative"])
    with_alt = sum(1 for i in imgs if i["has_alt"] and i["alt"])
    missing = [i for i in imgs if not i["has_alt"] and not i["decorative"] and i["tag"] in ("img", "input", "area") and not i.get("name")]
    meas = {"visible_images": len(imgs), "informative_with_alt": with_alt, "decorative_marked": dec, "missing_alt": len(missing), "axe": axe_counts(outs)}
    ev = axe_ev(outs) + [dom_evidence(pc.url, i["selector"], i.get("src", ""), pc.viewport, pc.state, "image without text alternative") for i in missing[:4]]
    applicable = imgs or any(o["outcome"] in ("violation", "pass", "incomplete") for o in outs.values())
    if not applicable:
        return mk(pc, "KBD-09", "rt.images", "not_applicable", "not_tested", "no images", meas=meas, appl="no images on page")
    avail = "present" if (with_alt or dec) else "not_found_in_scope"
    viol = [r for r, o in outs.items() if o["outcome"] == "violation" and _is_wcag(o["tags"])]
    if viol:
        return mk(pc, "KBD-09", "rt.images", avail, "fail", f"axe: {', '.join(viol)}", evidence=ev, meas=meas, manual=True)
    return mk(pc, "KBD-09", "rt.images", avail, "pass", f"{with_alt} informative with alt, {dec} decorative-marked; no WCAG rule violations",
              evidence=ev, meas=meas, manual=True)


def _fields(snap):
    fs = []
    for f in snap.get("forms", []):
        fs += [dict(x, _form=f["selector"]) for x in f.get("fields", [])]
    fs += [dict(x, _form=None) for x in snap.get("loose_fields", [])]
    return [f for f in fs if f.get("visible")]


def _kbd10(pc, snap, axe):
    fields = _fields(snap)
    outs = axe_outcomes(axe, ["label", "select-name", "form-field-multiple-labels", "aria-input-field-name", "label-title-only"])
    labelled = [f for f in fields if f.get("label_text") or f.get("aria_label") or f.get("labelledby") or f.get("title")]
    ph_only = [f for f in fields if f not in labelled and f.get("placeholder")]
    meas = {"visible_fields": len(fields), "labelled": len(labelled), "placeholder_only": len(ph_only), "axe": axe_counts(outs)}
    ev = axe_ev(outs) + [dom_evidence(pc.url, f["selector"], f"placeholder={f.get('placeholder')!r}", pc.viewport, pc.state, "placeholder-only label") for f in ph_only[:4]]
    if not fields and all(o["outcome"] in ("inapplicable", "not_run") for o in outs.values()):
        return mk(pc, "KBD-10", "rt.form_labels", "not_applicable", "not_tested", "no form fields", meas=meas, appl="no form fields on page")
    avail = "present" if labelled else "not_found_in_scope"
    viol = [r for r, o in outs.items() if o["outcome"] == "violation" and _is_wcag(o["tags"])]
    if viol:
        return mk(pc, "KBD-10", "rt.form_labels", avail, "fail", f"axe: {', '.join(viol)}", evidence=ev, meas=meas)
    if ph_only or outs["label-title-only"]["outcome"] == "violation":
        return mk(pc, "KBD-10", "rt.form_labels", avail, "partial", f"{len(ph_only)} placeholder-only field(s); label-title-only: {outs['label-title-only']['outcome']}",
                  evidence=ev, meas=meas)
    return mk(pc, "KBD-10", "rt.form_labels", avail, "pass", f"{len(labelled)}/{len(fields)} fields labelled; no violations", evidence=ev, meas=meas)


def _vis03(pc, snap, cfg):
    mv = (snap.get("meta_viewport") or "").replace(" ", "").lower()
    sc = snap.get("scroll", {})
    tol = int(cfg["thresholds"]["reflow_tolerance_px"])
    over = sc.get("scrollWidth", 0) > sc.get("clientWidth", 0) + tol
    culprits = (snap.get("overflow") or {}).get("culprits", [])
    meas = {"meta_viewport": snap.get("meta_viewport"), "scrollWidth": sc.get("scrollWidth"), "clientWidth": sc.get("clientWidth"), "culprits": culprits[:5]}
    avail = "present" if "width=device-width" in mv else "not_found_in_scope"
    ev = [dom_evidence(pc.url, c["selector"], f"right={c['right']} width={c['width']}", pc.viewport, pc.state, "overflowing element") for c in culprits[:4]]
    if over:
        return mk(pc, "VIS-03", "rt.mobile", avail, "fail", f"horizontal overflow at mobile width ({sc.get('scrollWidth')} > {sc.get('clientWidth')})", evidence=ev, meas=meas)
    return mk(pc, "VIS-03", "rt.mobile", avail, "pass", "no horizontal page overflow at mobile width", evidence=ev, meas=meas)


def _nav07(pc, snap):
    from .discovery import path_depth
    d = path_depth(pc.url)
    bcs = [b for b in snap.get("breadcrumbs", []) if b.get("visible")]
    meas = {"path_depth": d, "breadcrumbs": len(bcs), "jsonld": snap.get("breadcrumb_jsonld")}
    if d < 2 and not bcs:
        return mk(pc, "NAV-07", "rt.breadcrumb", "not_applicable", "not_tested", f"path depth {d} < 2", meas=meas, appl="page not deep in hierarchy")
    if not bcs:
        return mk(pc, "NAV-07", "rt.breadcrumb", "not_found_in_scope", "not_tested", f"no breadcrumb on page at depth {d}", meas=meas)
    b = bcs[0]
    ev = [dom_evidence(pc.url, b["selector"], f"in_nav={b['in_nav']} label={b.get('nav_label')!r} current={b['has_current']}", pc.viewport, pc.state)]
    if b["in_nav"] and b.get("nav_label") and b["has_current"]:
        return mk(pc, "NAV-07", "rt.breadcrumb", "present", "pass", "breadcrumb in named nav with aria-current", evidence=ev, meas=meas)
    return mk(pc, "NAV-07", "rt.breadcrumb", "present", "partial", "breadcrumb lacks named nav semantics or current-page indication", evidence=ev, meas=meas)


def _nav13(pc, snap):
    h1 = next((h["text"] for h in snap.get("headings", []) if h["level"] == 1), "")
    internal = sum(1 for l in snap.get("links", []) if l.get("internal") and l.get("visible") and not (l.get("href") or "").startswith("#"))
    soft = bool(SOFT404.search(snap.get("title", "") + " " + h1))
    meas = {"internal_links": internal, "soft_404": soft, "title": snap.get("title"), "h1": h1}
    if soft or internal == 0:
        why = "soft-404 page (HTTP OK but not-found content)" if soft else "no internal navigation links"
        return mk(pc, "NAV-13", "rt.dead_end", "present", "fail", why, meas=meas)
    return mk(pc, "NAV-13", "rt.dead_end", "present", "pass", f"{internal} internal links; not a soft-404", meas=meas)


def _kbd12(pc, snap):
    d = snap.get("draggable", [])
    if not d:
        return mk(pc, "KBD-12", "rt.drag", "not_applicable", "not_tested", "no draggable elements", appl="no drag interaction observed")
    return mk(pc, "KBD-12", "rt.drag", "present", "not_tested", f"{len(d)} draggable element(s); alternatives require human review",
              grade="heuristic_inference", evidence=[dom_evidence(pc.url, x["selector"], x["excerpt"], pc.viewport, pc.state) for x in d[:3]], manual=True)


def _kbd13(pc, snap):
    caps = snap.get("captcha", [])
    if not caps:
        return mk(pc, "KBD-13", "rt.captcha", "not_applicable", "not_tested", "no CAPTCHA", appl="no CAPTCHA observed")
    ev = [dom_evidence(pc.url, c["selector"], f"vendor={c['vendor']} audio_cue={c['audio_alternative_cue']}", pc.viewport, pc.state) for c in caps[:3]]
    vendor = [c for c in caps if c["vendor"] != "custom"]
    custom = [c for c in caps if c["vendor"] == "custom"]
    if custom and not any(c["audio_alternative_cue"] for c in custom):
        return mk(pc, "KBD-13", "rt.captcha", "present", "fail", "custom image/text CAPTCHA without an audio/alternative control (heuristic)",
                  grade="heuristic_inference", evidence=ev, manual=True)
    if vendor:
        return mk(pc, "KBD-13", "rt.captcha", "present", "partial", f"third-party CAPTCHA ({', '.join(sorted({c['vendor'] for c in vendor}))}) with vendor alternatives; needs review",
                  grade="heuristic_inference", evidence=ev, manual=True)
    return mk(pc, "KBD-13", "rt.captcha", "present", "not_tested", "custom CAPTCHA with an audio/alternative cue; needs review", grade="heuristic_inference", evidence=ev, manual=True)


def _lum(c):
    def ch(v):
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c["r"]) + 0.7152 * ch(c["g"]) + 0.0722 * ch(c["b"])


def contrast(a, b) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 2)


def _blend(fg, bg):
    a = fg.get("a", 1)
    return {k: fg[k] * a + bg[k] * (1 - a) for k in ("r", "g", "b")}


def _vis02(pc, snap):
    ctrls = snap.get("control_colors", [])
    rows, ok = [], 0
    for c in ctrls:
        if c.get("parent_bg_image") or not c.get("parent_bg"):
            continue
        pbg = c["parent_bg"]
        best = 0.0
        for s in c["sides"]:
            if s["width"] >= 1 and s["style"] not in ("none", "hidden") and s["color"] and s["color"]["a"] > 0:
                best = max(best, contrast(_blend(s["color"], pbg), pbg))
        if c.get("bg") and c["bg"]["a"] > 0:
            best = max(best, contrast(_blend(c["bg"], pbg), pbg))
        rows.append({"selector": c["selector"], "best_ratio": best})
        ok += best >= 3.0
    if not rows:
        return mk(pc, "VIS-02", "rt.control_contrast", "not_applicable", "not_tested", "no assessable form controls", appl="no visible text-entry controls")
    ev = [dom_evidence(pc.url, r["selector"], f"best boundary/fill contrast {r['best_ratio']}:1", pc.viewport, pc.state) for r in rows if r["best_ratio"] < 3][:4]
    meas = {"controls": len(rows), "meeting_3_to_1": ok, "ratios": rows[:10]}
    ver = "pass" if ok == len(rows) else "fail" if ok == 0 else "partial"
    return mk(pc, "VIS-02", "rt.control_contrast", "present", ver, f"{ok}/{len(rows)} controls have boundary or fill contrast >= 3:1 (computed colours; heuristic)",
              grade="heuristic_inference", evidence=ev, meas=meas, manual=True)


def _vis10(pc, snap, axe):
    anims = snap.get("animations", [])
    infinite = [a for a in anims if a.get("infinite")]
    long_ = [a for a in anims if (a.get("active") or 0) > 5000 and not a.get("infinite")]
    mv = snap.get("moving", {})
    carousels = mv.get("carousels", [])
    outs = axe_outcomes(axe, ["marquee", "blink"])
    moving = len(infinite) + len(long_) + mv.get("marquee", 0) + mv.get("blink", 0) + mv.get("autoplay_media", 0) + len(carousels)
    meas = {"infinite_animations": len(infinite), "long_animations": len(long_), "marquee": mv.get("marquee", 0),
            "autoplay_media": mv.get("autoplay_media", 0), "carousels": len(carousels), "pause_controls": len(snap.get("pause_controls", [])), "axe": axe_counts(outs)}
    if not moving:
        return mk(pc, "VIS-10", "rt.motion_controls", "not_applicable", "not_tested", "no auto-moving content detected", meas=meas, appl="no auto-moving content")
    ev = axe_ev(outs) + [dom_evidence(pc.url, c["selector"], f"pause_control={c['pause_control']}", pc.viewport, pc.state, "carousel") for c in carousels[:3]]
    ev += [dom_evidence(pc.url, a.get("target", ""), f"infinite animation {a.get('type')}", pc.viewport, pc.state) for a in infinite[:2]]
    if any(o["outcome"] == "violation" for o in outs.values()):
        return mk(pc, "VIS-10", "rt.motion_controls", "not_found_in_scope", "fail", "axe marquee/blink violation", evidence=ev, meas=meas, manual=True)
    global_pause = bool(snap.get("pause_controls"))
    with_ctrl = sum(1 for c in carousels if c["pause_control"])
    other_moving = len(infinite) + len(long_) + mv.get("marquee", 0) + mv.get("blink", 0) + mv.get("autoplay_media", 0)
    if global_pause or (carousels and with_ctrl == len(carousels) and not other_moving):
        return mk(pc, "VIS-10", "rt.motion_controls", "present", "pass", "pause/stop control present for moving content (heuristic; not operated)",
                  grade="heuristic_inference", evidence=ev, meas=meas, manual=True)
    if carousels and 0 < with_ctrl < len(carousels):
        return mk(pc, "VIS-10", "rt.motion_controls", "present", "partial", f"{with_ctrl}/{len(carousels)} carousels have pause controls",
                  grade="heuristic_inference", evidence=ev, meas=meas, manual=True)
    return mk(pc, "VIS-10", "rt.motion_controls", "not_found_in_scope", "fail", "auto-moving content without a pause/stop control (heuristic)",
              grade="heuristic_inference", evidence=ev, meas=meas, manual=True)


def _media(pc, snap, axe):
    vids = [m for m in snap.get("media", []) if m["tag"] == "video"]
    auds = [m for m in snap.get("media", []) if m["tag"] == "audio"]
    embeds = [e for e in snap.get("embeds", []) if re.search(r"youtube|youtu\.be|vimeo|dailymotion|player\.", e["src"], re.I)]
    out = []
    ev = [dom_evidence(pc.url, m["selector"], f"tracks={m['tracks']}", pc.viewport, pc.state) for m in vids[:3]]
    ev += [dom_evidence(pc.url, e["selector"], e["src"], pc.viewport, pc.state, "third-party video embed") for e in embeds[:2]]
    if not vids and not embeds:
        out.append(mk(pc, "VIS-11", "rt.media", "not_applicable", "not_tested", "no video", appl="no video on page"))
    elif vids:
        cap = [v for v in vids if any(t["kind"] in ("captions", "subtitles") for t in v["tracks"])]
        if len(cap) == len(vids):
            out.append(mk(pc, "VIS-11", "rt.media", "present", "pass", f"{len(cap)} native video(s) with caption/subtitle tracks (accuracy not assessed)", evidence=ev, manual=True))
        else:
            out.append(mk(pc, "VIS-11", "rt.media", "present" if cap else "not_found_in_scope", "fail", f"{len(vids) - len(cap)}/{len(vids)} native video(s) without caption tracks", evidence=ev, manual=True))
    else:
        out.append(mk(pc, "VIS-11", "rt.media", "unknown", "not_tested", f"{len(embeds)} third-party video embed(s); captions not inspectable", evidence=ev, manual=True))
    media_any = vids or auds or embeds
    if not media_any:
        out.append(mk(pc, "VIS-12", "rt.media", "not_applicable", "not_tested", "no audio/video", appl="no media on page"))
        out.append(mk(pc, "VIS-13", "rt.media", "not_applicable", "not_tested", "no video", appl="no video on page"))
        return out
    tr = any(m["transcript_cue"] for m in vids + auds)
    out.append(mk(pc, "VIS-12", "rt.media", "present" if tr else "not_found_in_scope", "not_tested",
                  "transcript cue near media" if tr else "no transcript cue near media (heuristic)", grade="heuristic_inference", evidence=ev, manual=True))
    if vids or embeds:
        desc = any(m["description_cue"] or any(t["kind"] == "descriptions" for t in m["tracks"]) for m in vids)
        out.append(mk(pc, "VIS-13", "rt.media", "present" if desc else "not_found_in_scope", "not_tested",
                      "audio-description evidence" if desc else "no audio-description evidence (need depends on content)", grade="heuristic_inference", evidence=ev, manual=True))
    else:
        out.append(mk(pc, "VIS-13", "rt.media", "not_applicable", "not_tested", "audio only", appl="no video on page"))
    return out


OVERLAY_RX = re.compile(r"acsbapp|accessibe|userway|equalweb|audioeye|recite\.?me|maxaccess|enable\.co\.il|eye-able|onlineada|purple-?lens", re.I)


def _vis16(pc, snap):
    hits = [s for s in snap.get("scripts", []) + [e["src"] for e in snap.get("embeds", [])] if OVERLAY_RX.search(s)]
    if hits:
        return mk(pc, "VIS-16", "rt.overlay", "present", "not_tested", "third-party overlay script loaded (descriptive; no accessibility credit)",
                  evidence=[{"type": "network", "url": h} for h in hits[:3]])
    return mk(pc, "VIS-16", "rt.overlay", "not_found_in_scope", "not_tested", "no known overlay script")


def _form01(pc, snap):
    forms = [f for f in snap.get("forms", []) if f.get("visible")]
    rows = []
    for f in forms:
        instr = bool(FORM_REQ_INSTR.search(f.get("text", "")))
        for x in f.get("fields", []):
            if not x.get("visible"):
                continue
            prog = x.get("required") or x.get("aria_required") == "true"
            vis = bool(REQUIRED_CUE.search(" ".join(filter(None, [x.get("label_text"), x.get("acc_name"), x.get("describedby_text")]))))
            if prog or vis:
                rows.append({"selector": x["selector"], "programmatic": bool(prog), "visible": vis or (prog and instr)})
    if not rows:
        return mk(pc, "FORM-01", "rt.required", "not_applicable", "not_tested", "no required fields", appl="no required fields on page")
    good = [r for r in rows if r["programmatic"] and r["visible"]]
    ev = [dom_evidence(pc.url, r["selector"], f"programmatic={r['programmatic']} visible={r['visible']}", pc.viewport, pc.state) for r in rows if r not in good][:4]
    ver = "pass" if len(good) == len(rows) else "fail" if not good else "partial"
    return mk(pc, "FORM-01", "rt.required", "present", ver, f"{len(good)}/{len(rows)} required fields indicated both visibly and programmatically",
              evidence=ev, meas={"required_fields": len(rows), "fully_indicated": len(good)})


def _form02(pc, snap):
    rows = []
    for x in _fields(snap):
        ml = x.get("maxlength")
        constrained = bool(x.get("pattern") or x.get("minlength") or (ml and ml.isdigit() and int(ml) <= 20) or x.get("type") == "tel")
        if not constrained or x.get("type") in ("checkbox", "radio", "password", "select", "select-one"):
            continue
        persistent = bool(x.get("describedby_text") or x.get("title") or HINT_RX.search(x.get("label_text") or ""))
        ph = bool(HINT_RX.search(x.get("placeholder") or "") or (x.get("placeholder") and not persistent))
        rows.append({"selector": x["selector"], "persistent": persistent, "placeholder_only": (not persistent) and ph})
    if not rows:
        return mk(pc, "FORM-02", "rt.instructions", "not_applicable", "not_tested", "no constrained fields", appl="no format-constrained fields")
    pers = sum(r["persistent"] for r in rows)
    ph = sum(r["placeholder_only"] for r in rows)
    ev = [dom_evidence(pc.url, r["selector"], f"persistent={r['persistent']} placeholder_only={r['placeholder_only']}", pc.viewport, pc.state) for r in rows if not r["persistent"]][:4]
    meas = {"constrained_fields": len(rows), "persistent_instructions": pers, "placeholder_only": ph}
    if pers == len(rows):
        return mk(pc, "FORM-02", "rt.instructions", "present", "pass", "all constrained fields have persistent instructions (heuristic)", grade="heuristic_inference", evidence=ev, meas=meas, manual=True)
    if pers or ph:
        return mk(pc, "FORM-02", "rt.instructions", "present", "partial", f"{pers}/{len(rows)} with persistent instructions; {ph} placeholder-only", grade="heuristic_inference", evidence=ev, meas=meas, manual=True)
    return mk(pc, "FORM-02", "rt.instructions", "not_found_in_scope", "fail", "no instructions for constrained fields", grade="heuristic_inference", evidence=ev, meas=meas, manual=True)


def _form06(pc, snap, axe):
    rows = []
    for x in _fields(snap):
        hint = " ".join(filter(None, [x.get("name"), x.get("id"), x.get("label_text"), x.get("placeholder")]))
        personal = x.get("type") in ("email", "tel") or (x.get("type") == "password") or bool(PERSONAL_RX.search(hint))
        if not personal or x.get("type") in ("checkbox", "radio", "search", "hidden"):
            continue
        tokens = (x.get("autocomplete") or "").lower().split()
        valid = bool(tokens) and tokens[-1] in AUTOCOMPLETE_TOKENS and tokens[-1] not in ("on",)
        rows.append({"selector": x["selector"], "autocomplete": x.get("autocomplete"), "valid": valid})
    outs = axe_outcomes(axe, ["autocomplete-valid"])
    if not rows:
        return mk(pc, "FORM-06", "rt.autocomplete", "not_applicable", "not_tested", "no personal-information fields detected", appl="no personal fields")
    ok = sum(r["valid"] for r in rows)
    ev = axe_ev(outs) + [dom_evidence(pc.url, r["selector"], f"autocomplete={r['autocomplete']!r}", pc.viewport, pc.state) for r in rows if not r["valid"]][:4]
    meas = {"personal_fields": len(rows), "valid_tokens": ok, "axe": axe_counts(outs)}
    if outs["autocomplete-valid"]["outcome"] == "violation" or ok == 0:
        return mk(pc, "FORM-06", "rt.autocomplete", "present" if ok else "not_found_in_scope", "fail",
                  "axe autocomplete-valid violation" if ok else "no personal field has a valid autocomplete token", grade="heuristic_inference", evidence=ev, meas=meas)
    ver = "pass" if ok == len(rows) else "partial"
    return mk(pc, "FORM-06", "rt.autocomplete", "present", ver, f"{ok}/{len(rows)} personal fields with valid autocomplete tokens", grade="heuristic_inference", evidence=ev, meas=meas)


def _form09(pc, snap, axe, text):
    outs = axe_outcomes(axe, ["meta-refresh"])
    cue = kw_rx(pc.cfg["keywords"]["timeout"]).search(text)
    meas = {"meta_refresh": snap.get("meta_refresh"), "timeout_cue": bool(cue), "axe": axe_counts(outs)}
    if outs["meta-refresh"]["outcome"] == "violation":
        return mk(pc, "FORM-09", "rt.timeout", "not_found_in_scope", "fail", "timed meta refresh/redirect (axe meta-refresh)", evidence=axe_ev(outs), meas=meas, manual=True)
    if cue:
        return mk(pc, "FORM-09", "rt.timeout", "present", "not_tested", f"timeout wording observed: {cue.group(0)!r} (long sessions not run)", grade="heuristic_inference", meas=meas, manual=True)
    return mk(pc, "FORM-09", "rt.timeout", "not_applicable", "not_tested", "no time limit observed on page load", meas=meas, appl="no observable time limit")


def _form10(pc, snap, text):
    if pc.meta.get("long_process") is False:
        return mk(pc, "FORM-10", "rt.keywords", "not_applicable", "not_tested", "metadata long_process=false", appl="metadata long_process=false")
    rx = kw_rx(pc.cfg["keywords"]["save_resume"])
    hay = " ".join([b.get("name") or "" for b in snap.get("buttons", [])] + [l.get("name") or "" for l in snap.get("links", [])])
    m = rx.search(hay) or rx.search(text)
    note = "" if pc.meta.get("long_process") else " (relevance unknown: long_process not supplied)"
    if m:
        return mk(pc, "FORM-10", "rt.keywords", "present", "not_tested", f"save/resume cue {m.group(0)!r}{note}", grade="heuristic_inference", manual=True)
    return mk(pc, "FORM-10", "rt.keywords", "not_found_in_scope", "not_tested", f"no save/resume cue{note}", grade="heuristic_inference")


def _text_metrics(pc, snap, text, words):
    if words < 100:
        r = mk(pc, "FORM-11", "rt.text_metrics", "not_applicable", "not_tested", f"only {words} words", appl="too little text", meas={"words": words})
        r2 = mk(pc, "FORM-12", "rt.text_metrics", "present" if words else "not_applicable", "not_tested", f"{words} words", meas={"words": words},
                appl="" if words else "no text")
        return [r, r2]
    sentences = [s for s in re.split(r"(?<=[.!?।])\s+", text) if re.search(r"[^\W\d_]{2,}", s)]
    toks = re.findall(r"[^\W\d_]+", text)
    dom = L.dominant_script(text)
    meas = {"words": words, "sentences": len(sentences), "mean_sentence_words": round(words / max(1, len(sentences)), 1), "dominant_script": dom}
    if dom == "Latn":
        meas["long_word_share"] = round(sum(1 for t in toks if len(t) >= 7) / max(1, len(toks)), 3)
    abbr_ok = set(snap.get("abbr_titles", []))
    abbrs = sorted({a for a in re.findall(r"\b[A-Z]{2,6}\b", text) if a not in abbr_ok and not re.search(rf"\({re.escape(a)}\)|{re.escape(a)}\s*\(", text)})
    return [mk(pc, "FORM-11", "rt.text_metrics", "present", "not_tested", "proxy measures recorded; plain language requires human judgement", meas=meas, manual=True),
            mk(pc, "FORM-12", "rt.text_metrics", "present", "not_tested", f"{len(abbrs)} unexpanded all-caps abbreviation(s) (proxy)",
               meas={"unexpanded_abbreviations": len(abbrs), "examples": abbrs[:15]}, manual=True)]


def _trust06(pc, snap):
    calls = snap.get("permission_calls", [])
    if not calls:
        return mk(pc, "TRUST-06", "rt.permissions", "not_applicable", "not_tested", "no permission requests on load", appl="no permission requests observed")
    pre = [c for c in calls if not c.get("user_activated")]
    meas = {"calls": calls[:10]}
    if pre:
        return mk(pc, "TRUST-06", "rt.permissions", "present", "fail", f"{len(pre)} permission request(s) before any user interaction ({', '.join(sorted({c['api'] for c in pre}))})", meas=meas, manual=True)
    return mk(pc, "TRUST-06", "rt.permissions", "present", "not_tested", "permission requests after user activation; explanation needs review", meas=meas, manual=True)


SERVICE_TYPES_NA = {"data_portal", "documentation_site", "informational_site"}


def _keyword_features(pc, snap) -> list[Observation]:
    kw = pc.cfg["keywords"]
    links = [l for l in snap.get("links", []) if l.get("visible")]
    heads = snap.get("headings", [])
    btns = [b for b in snap.get("buttons", []) if b.get("visible")]
    out = []

    def find(words, pool):
        rx = kw_rx(words)
        hits = []
        for item in pool:
            hay = " ".join(filter(None, [item.get("name"), item.get("text"), item.get("aria_label")]))
            path = urllib_path(item.get("abs") or "")
            if rx.search(hay) or (path and rx.search(path)):
                hits.append(item)
        return hits

    def ev_of(items):
        return [dom_evidence(pc.url, i.get("selector", ""), (i.get("name") or i.get("text") or "")[:80], pc.viewport, pc.state, i.get("abs") or "") for i in items[:4]]

    for fid, key in (("TRUST-01", "eligibility"), ("TRUST-02", "documents"), ("TRUST-03", "fees"), ("TRUST-04", "process")):
        if pc.interface_type in SERVICE_TYPES_NA:
            out.append(mk(pc, fid, "rt.keywords", "not_applicable", "not_tested", f"interface_type={pc.interface_type} is not a service-delivery interface",
                          appl="non-service interface type"))
            continue
        hits = find(kw[key], links + heads + btns)
        out.append(mk(pc, fid, "rt.keywords", "present" if hits else "not_found_in_scope", "not_tested",
                      f"{len(hits)} link/heading cue(s) for {key}" if hits else f"no {key} cue in links/headings (heuristic)",
                      grade="heuristic_inference", evidence=ev_of(hits), manual=bool(hits)))
    for fid, key in (("TRUST-05", "privacy"), ("TRUST-07", "accessibility_statement"), ("TRUST-08", "grievance")):
        hits = find(kw[key], links)
        out.append(mk(pc, fid, "rt.keywords", "present" if hits else "not_found_in_scope", "not_tested",
                      f"link cue for {key}: {hits[0].get('name')!r}" if hits else f"no {key} link (heuristic)",
                      grade="heuristic_inference", evidence=ev_of(hits), meas={"targets": [h.get("abs") for h in hits[:3]]}))
    if pc.is_home:
        for fid, key in (("NAV-09", "help"), ("NAV-10", "contact")):
            hits = find(kw[key], links)
            if fid == "NAV-10":
                hits += [l for l in links if (l.get("href") or "").startswith(("mailto:", "tel:")) and l not in hits]
            out.append(mk(pc, fid, "rt.keywords", "present" if hits else "not_found_in_scope", "not_tested",
                          f"homepage link cue for {key}: {hits[0].get('name') or hits[0].get('href')!r}" if hits else f"no {key} link on homepage (heuristic)",
                          grade="heuristic_inference", evidence=ev_of(hits), meas={"targets": [h.get("abs") for h in hits[:3]]}, manual=bool(hits)))
    return out


def urllib_path(u: str) -> str:
    import urllib.parse
    try:
        return urllib.parse.unquote(urllib.parse.urlsplit(u).path).replace("-", " ").replace("_", " ").replace("/", " ")
    except Exception:
        return ""


# ------------------------------------------------------------------ keyboard state
def keyboard_state(pc: PCtx, walk: dict, skip: dict | None, activation: list[dict], axe: dict) -> list[Observation]:
    out = []
    reach = walk.get("reachability", [])
    stops = walk.get("stops", 0)
    completed = walk.get("completed")
    scope = f"{stops} Tab stops ({'cycle completed' if completed else walk.get('end_reason')})"
    seq_ev = [{"type": "focus_sequence", "page_url": pc.url, "stops": [s.get("selector") for s in walk.get("sequence", [])][:60]}]
    # KBD-01
    direct = [r for r in reach if r["kind"] in ("native", "tabindex", "aria") and not r.get("disabled")]
    pointer = [r for r in reach if r["kind"] == "pointer"]
    tested_direct = direct if completed else [r for r in direct if r.get("before_last")]
    missed = [r for r in tested_direct if not r["reached"] and not r.get("duplicate_href_reached")]
    missed_ptr = [r for r in pointer if not r["reached"] and (completed or r.get("before_last"))]
    meas = {"controls_enumerated": len(direct), "controls_tested": len(tested_direct), "missed": len(missed),
            "pointer_candidates": len(pointer), "pointer_missed": len(missed_ptr), "tab_stops": stops, "completed": completed}
    ev = seq_ev + [dom_evidence(pc.url, r["selector"], r.get("excerpt", ""), pc.viewport, pc.state, f"not reached by Tab ({r['kind']})") for r in (missed + missed_ptr)[:6]]
    if not direct and not pointer:
        out.append(mk(pc, "KBD-01", "rt.keyboard_walk", "not_applicable", "not_tested", "no visible interactive controls", meas=meas, appl="no controls"))
    elif missed:
        out.append(mk(pc, "KBD-01", "rt.keyboard_walk", "present", "fail", f"{len(missed)}/{len(tested_direct)} tested controls never reached by Tab within {scope}", evidence=ev, meas=meas))
    elif missed_ptr or not completed:
        why = []
        if missed_ptr:
            why.append(f"{len(missed_ptr)} pointer-only candidate(s) not focusable (heuristic)")
        if not completed:
            why.append(f"walk ended: {walk.get('end_reason')}; {len(direct) - len(tested_direct)} controls beyond the tested range")
        out.append(mk(pc, "KBD-01", "rt.keyboard_walk", "present", "partial", "; ".join(why), evidence=ev, meas=meas,
                      grade="heuristic_inference" if missed_ptr else "direct_observation", manual=bool(missed_ptr)))
    else:
        out.append(mk(pc, "KBD-01", "rt.keyboard_walk", "present", "pass", f"all {len(tested_direct)} tested controls reached ({scope})", evidence=seq_ev, meas=meas))
    # KBD-03
    vis = [v for v in walk.get("focus_visibility", []) if "error" not in v and v.get("changed_px", -1) >= 0]
    if not vis:
        out.append(mk(pc, "KBD-03", "rt.keyboard_walk", "not_applicable" if stops == 0 else "unknown", "not_tested", "no tab stops measured", appl="no tab stops" if stops == 0 else ""))
    else:
        good = [v for v in vis if v["visible_change"]]
        bad = [v for v in vis if not v["visible_change"]]
        evv = [{"type": "screenshot", "path": v.get("screenshot"), "selector": v["selector"], "changed_px": v["changed_px"], "outline": v.get("outline")} for v in bad[:5]]
        meas3 = {"stops_measured": len(vis), "visible": len(good), "not_visible": len(bad)}
        ver = "pass" if not bad else "fail" if not good else "partial"
        out.append(mk(pc, "KBD-03", "rt.keyboard_walk", "present" if good else "not_found_in_scope", ver,
                      f"{len(good)}/{len(vis)} measured tab stops show a visible focus change", evidence=evv, meas=meas3))
    # KBD-04
    obs_ = walk.get("obscured", [])
    if stops == 0:
        out.append(mk(pc, "KBD-04", "rt.keyboard_walk", "not_applicable", "not_tested", "no tab stops", appl="no tab stops"))
    else:
        evo = [dom_evidence(pc.url, o["selector"], f"covered by {o.get('covered_by')}", pc.viewport, pc.state) for o in obs_[:4]]
        out.append(mk(pc, "KBD-04", "rt.keyboard_walk", "present", "fail" if obs_ else "pass",
                      f"{len(obs_)}/{stops} focused stops entirely hidden" if obs_ else f"no focused stop entirely hidden ({stops} stops)", evidence=evo,
                      meas={"stops": stops, "obscured": len(obs_)}))
    # KBD-06
    trap = walk.get("trap")
    if stops == 0:
        out.append(mk(pc, "KBD-06", "rt.keyboard_walk", "not_applicable", "not_tested", "no tab stops", appl="no tab stops"))
    elif trap and not trap["escaped"]:
        out.append(mk(pc, "KBD-06", "rt.keyboard_walk", "present", "fail", f"keyboard trap: {trap['cycle']['kind']} among {len(trap['cycle']['ids'])} element(s); Tab, Shift+Tab and Escape did not leave",
                      evidence=[dom_evidence(pc.url, s, "", pc.viewport, pc.state, "trapped element") for s in trap["cycle"]["selectors"][:5]], meas={"trap": trap}))
    else:
        note = f"; focus cycle escaped with {trap['escape_key']}" if trap else ""
        out.append(mk(pc, "KBD-06", "rt.keyboard_walk", "present", "pass", f"no inescapable cycle within {scope}{note}", evidence=seq_ev, meas={"trap": trap}))
    # NAV-06
    outs = axe_outcomes(axe, ["skip-link"])
    if not skip:
        out.append(mk(pc, "NAV-06", "rt.skip_link_activation", "not_found_in_scope", "not_tested", "no skip link among the first three tab stops"))
    else:
        works = skip.get("target_exists") and (skip.get("after_tab_inside_target") or skip.get("target_in_view"))
        bad_axe = outs["skip-link"]["outcome"] == "violation"
        out.append(mk(pc, "NAV-06", "rt.skip_link_activation", "present", "pass" if works and not bad_axe else "fail",
                      f"skip link {skip.get('href')}: target_exists={skip.get('target_exists')}, next Tab inside target={skip.get('after_tab_inside_target')}",
                      evidence=[dom_evidence(pc.url, skip["selector"], skip.get("name") or "", pc.viewport, pc.state)] + axe_ev(outs), meas=skip))
    # KBD-02
    if not activation:
        out.append(mk(pc, "KBD-02", "rt.keyboard_activation", "not_applicable", "not_tested", "no safe in-page controls (aria-expanded/aria-controls/summary/tab)", appl="no candidates"))
    else:
        ok = [a for a in activation if a["changed"]]
        ver = "pass" if len(ok) == len(activation) else "fail" if not ok else "partial"
        out.append(mk(pc, "KBD-02", "rt.keyboard_activation", "present", ver, f"{len(ok)}/{len(activation)} tested controls responded to Enter/Space",
                      evidence=[dom_evidence(pc.url, a["selector"], a.get("name") or "", pc.viewport, pc.state, f"key={a.get('key')} change={a.get('change')}") for a in activation[:4]],
                      meas={"tested": len(activation), "responded": len(ok)}))
    return out


def modal_state(pc: PCtx, rec: dict) -> list[Observation]:
    out = []
    if not rec.get("opened"):
        out.append(mk(pc, "KBD-05", "rt.modal", "unknown", "not_tested", f"trigger {rec['trigger']} did not open a dialog via Enter ({rec.get('error', 'no new visible dialog')})", manual=True))
        return out
    a, b, c, d = rec.get("focus_moved_in"), rec.get("focus_contained"), rec.get("dismissed"), rec.get("focus_returned")
    subs = {"focus_moved_in": a, "focus_contained": b if b is not None else "n/a (non-modal)", "keyboard_dismissal": c, "focus_returned": d}
    holds = [bool(a), b is not False, bool(c), bool(d)]
    ev = [dom_evidence(pc.url, rec.get("dialog", ""), "", pc.viewport, pc.state, f"trigger={rec['trigger']} sub-checks={subs}")]
    if all(holds):
        ver = "pass"
    elif not a and not c:
        ver = "fail"
    else:
        ver = "partial"
    out.append(mk(pc, "KBD-05", "rt.modal", "present" if a else "not_found_in_scope", ver, f"dialog sub-checks: {subs}", evidence=ev, meas=rec))
    if rec.get("trap"):
        out.append(mk(pc, "KBD-06", "rt.modal", "present", "fail", "modal dialog keeps focus and cannot be dismissed by keyboard", evidence=ev, meas=rec))
    return out


def form_state(pc: PCtx, rec: dict) -> list[Observation]:
    out = []
    if not rec.get("tested"):
        for fid in ("FORM-03", "FORM-04", "FORM-05", "KBD-11"):
            out.append(mk(pc, fid, "rt.form_error", "unknown", "not_tested", f"form {rec['form']} not tested: {rec.get('reason')}"))
        return out
    fields = rec.get("fields", [])
    msgs = rec.get("messages", [])
    custom = [m for m in msgs if ERROR_CUE.search(m["text"]) or m.get("live")] or msgs
    target = rec.get("target_field")
    invalid = [f for f in fields if f.get("valid") is False or f.get("aria_invalid") == "true" or f.get("selector") == target]
    # With novalidate the browser never displays its validation bubble, so validationMessage is not user-visible.
    native = [] if rec.get("novalidate") else [f for f in invalid if f.get("validation_message")]
    assoc = [f for f in invalid if f.get("aria_invalid") == "true" and (f.get("describedby_text") or f.get("errormessage_text"))
             or ERROR_CUE.search(f.get("label_text") or "") and f.get("aria_invalid") == "true"]
    identified = {f["id"] for f in native} | {f["id"] for f in assoc} | ({f["id"] for f in invalid} if custom else set())
    ev = [dom_evidence(pc.url, f.get("selector", ""), f"valid={f.get('valid')} msg={f.get('validation_message')!r} aria-invalid={f.get('aria_invalid')} desc={f.get('describedby_text')!r}", pc.viewport, pc.state)
          for f in invalid[:4]] + [dom_evidence(pc.url, m["selector"], m["text"], pc.viewport, pc.state, f"new message live={m['live']}") for m in custom[:3]]
    meas = {"mode": rec.get("mode"), "novalidate": rec.get("novalidate"), "invalid_fields": len(invalid), "native_messages": len(native), "associated": len(assoc), "custom_messages": len(custom),
            "blocked_requests": rec.get("blocked_requests"), "filled": rec.get("filled")}
    produced = bool(identified or custom)
    if not produced:
        if rec.get("blocked_requests"):
            for fid in ("FORM-03", "FORM-04", "FORM-05", "KBD-11"):
                out.append(mk(pc, fid, "rt.form_error", "unknown", "not_tested", "no client-side validation; submission reached the network and was blocked by the safety policy (server-side validation not exercised)",
                              evidence=ev, meas=meas))
            return out
        out.append(mk(pc, "FORM-03", "rt.form_error", "not_found_in_scope", "fail", "invalid submission produced no error text and no request (no feedback)",
                      grade="heuristic_inference", evidence=ev, meas=meas, manual=True))
        for fid in ("FORM-04", "FORM-05", "KBD-11"):
            out.append(mk(pc, fid, "rt.form_error", "not_applicable", "not_tested", "no error state produced", appl="no errors produced"))
        return out
    n_inv = max(1, len(invalid))
    ver3 = "pass" if len(identified) >= n_inv else "partial" if identified else "fail"
    out.append(mk(pc, "FORM-03", "rt.form_error", "present", ver3, f"{len(identified)}/{n_inv} invalid field(s) identified with text (native {len(native)}, custom messages {len(custom)})",
                  evidence=ev, meas=meas))
    if assoc and len(assoc) >= n_inv:
        v4, why4 = "pass", "all invalid fields reference error text via aria"
    elif assoc:
        v4, why4 = "partial", f"{len(assoc)}/{n_inv} invalid fields associated"
    elif native and not custom:
        v4, why4 = "partial", "native validation messages only (association handled by the browser)"
    else:
        v4, why4 = "fail", "custom error messages not programmatically associated with fields"
    out.append(mk(pc, "FORM-04", "rt.form_error", "present" if (assoc or native) else "not_found_in_scope", v4, why4, evidence=ev, meas=meas))
    pres = rec.get("preserved", {})
    if not pres:
        out.append(mk(pc, "FORM-05", "rt.form_error", "unknown", "not_tested", "no other fields could be filled with test values", meas=meas))
    else:
        kept = sum(1 for v in pres.values() if v)
        v5 = "pass" if kept == len(pres) else "fail" if kept == 0 else "partial"
        out.append(mk(pc, "FORM-05", "rt.form_error", "present" if kept else "not_found_in_scope", v5, f"{kept}/{len(pres)} filled test values retained after the error", meas=meas))
    if not custom:
        out.append(mk(pc, "KBD-11", "rt.form_error", "not_applicable", "not_tested", "only native validation bubbles; no DOM messages inserted", appl="no dynamic messages", meas=meas))
    else:
        live = [m for m in custom if m.get("live") or m.get("focused")]
        focus_moved = bool(rec.get("focus_after", {}).get("selector")) and any(m["selector"] in (rec["focus_after"]["selector"] or "") for m in custom)
        v11 = "pass" if len(live) == len(custom) or focus_moved else "fail" if not live else "partial"
        out.append(mk(pc, "KBD-11", "rt.form_error", "present" if live else "not_found_in_scope", v11, f"{len(live)}/{len(custom)} new messages in live regions or focused",
                      evidence=ev, meas=meas))
    return out


def paste_state(pc: PCtx, results: list[dict], has_captcha: bool) -> Observation:
    if not results:
        return mk(pc, "FORM-08", "rt.auth_form", "not_applicable", "not_tested", "no password field", appl="no password fields")
    blocked = [r for r in results if r.get("prevented") or re.search(r"return\s+false", r.get("onpaste_attr") or "")]
    off = [r for r in results if (r.get("autocomplete") or "").lower() in ("off", "false")]
    ev = [dom_evidence(pc.url, r["selector"], f"paste_prevented={r.get('prevented')} autocomplete={r.get('autocomplete')!r}", pc.viewport, pc.state) for r in results]
    if blocked:
        return mk(pc, "FORM-08", "rt.auth_form", "not_found_in_scope", "fail", f"paste blocked on {len(blocked)} authentication field(s)", evidence=ev)
    issues = []
    if off:
        issues.append("autocomplete=off on credential field")
    if has_captcha:
        issues.append("CAPTCHA on the authentication page")
    if issues:
        return mk(pc, "FORM-08", "rt.auth_form", "present", "partial", "; ".join(issues), evidence=ev)
    return mk(pc, "FORM-08", "rt.auth_form", "present", "pass", "synthetic paste not prevented; password-manager hints not disabled", evidence=ev)


# ------------------------------------------------------------------ layout states
def reflow_state(pc: PCtx, m: dict, tol: int) -> Observation:
    ov = m["overflow"]
    culprits = ov.get("culprits", [])
    non_exempt = [c for c in culprits if not c["exempt"] and (c.get("has_text") or c["tag"] in ("img", "picture", "input", "select", "button"))]
    page_scroll = ov["scrollWidth"] > ov["clientWidth"] + tol
    meas = {"viewport_width": m["viewport_width"], "scrollWidth": ov["scrollWidth"], "clientWidth": ov["clientWidth"],
            "culprits": culprits[:8], "non_exempt": len(non_exempt), "page_horizontal_scroll": page_scroll}
    ev = [dom_evidence(pc.url, c["selector"], f"right={c['right']}px exempt={c['exempt']}", pc.viewport, pc.state) for c in culprits[:5]]
    if non_exempt:
        return mk(pc, "VIS-04", "rt.reflow", "present", "fail", f"{len(non_exempt)} non-exempt element(s) extend past the 320px viewport" + (" (page scrolls horizontally)" if page_scroll else " (clipped)"),
                  evidence=ev, meas=meas)
    note = "; overflow only from exempt 2-D content" if culprits else ""
    return mk(pc, "VIS-04", "rt.reflow", "present", "pass", f"content reflows at {m['viewport_width']}px{note}", evidence=ev, meas=meas)


def zoom_state(pc: PCtx, m: dict, baseline_clipped: list[str], axe_desktop: dict) -> Observation:
    outs = axe_outcomes(axe_desktop, ["meta-viewport", "meta-viewport-large"])
    new = sorted(set(m["clipped"]) - set(baseline_clipped))
    meas = {"clipped_baseline": len(baseline_clipped), "clipped_zoom": len(m["clipped"]), "newly_clipped": new[:15], "axe": axe_counts(outs)}
    ev = axe_ev(outs) + [dom_evidence(pc.url, s, "", pc.viewport, pc.state, "text newly clipped at 200%") for s in new[:4]]
    if outs["meta-viewport"]["outcome"] == "violation":
        return mk(pc, "VIS-05", "rt.zoom", "present", "fail", "viewport meta disables or limits zoom (axe meta-viewport)", evidence=ev, meas=meas)
    if new:
        return mk(pc, "VIS-05", "rt.zoom", "present", "partial", f"{len(new)} text element(s) newly clipped at emulated 200% zoom (heuristic)", grade="heuristic_inference", evidence=ev, meas=meas, manual=True)
    return mk(pc, "VIS-05", "rt.zoom", "present", "pass", "zoom not restricted; no newly clipped text at emulated 200%", evidence=ev, meas=meas)


def spacing_state(pc: PCtx, r: dict, axe_desktop: dict) -> Observation:
    outs = axe_outcomes(axe_desktop, ["avoid-inline-spacing"])
    meas = dict(r, axe=axe_counts(outs))
    ev = axe_ev(outs) + [dom_evidence(pc.url, s, "", pc.viewport, pc.state, "text newly clipped with WCAG text spacing") for s in r["newly_clipped"][:4]]
    if outs["avoid-inline-spacing"]["outcome"] == "violation":
        return mk(pc, "VIS-06", "rt.text_spacing", "present", "fail", "inline !important spacing prevents user overrides (axe)", evidence=ev, meas=meas)
    if r["newly_clipped_count"]:
        return mk(pc, "VIS-06", "rt.text_spacing", "present", "partial", f"{r['newly_clipped_count']} text element(s) newly clipped (heuristic)", grade="heuristic_inference", evidence=ev, meas=meas, manual=True)
    return mk(pc, "VIS-06", "rt.text_spacing", "present", "pass", "no newly clipped text with WCAG 1.4.12 spacing", evidence=ev, meas=meas)


def motion_state(pc: PCtx, n0: int, after: list[dict]) -> Observation:
    n1 = len(after)
    meas = {"running_animations_default": n0, "running_animations_reduced": n1}
    if n0 == 0:
        return mk(pc, "VIS-09", "rt.reduced_motion", "not_applicable", "not_tested", "no running animations by default", meas=meas, appl="no animations observed")
    if n1 == 0:
        return mk(pc, "VIS-09", "rt.reduced_motion", "present", "pass", f"{n0} running animation(s) stop under prefers-reduced-motion", meas=meas)
    if n1 < n0:
        return mk(pc, "VIS-09", "rt.reduced_motion", "present", "partial", f"animations reduced {n0} -> {n1}", meas=meas)
    return mk(pc, "VIS-09", "rt.reduced_motion", "not_found_in_scope", "fail", f"{n1} animation(s) still running under prefers-reduced-motion", meas=meas)
