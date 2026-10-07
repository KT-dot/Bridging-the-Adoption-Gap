"""Static detectors: build a SourceIndex for one checkout and emit observations.

Grades: parsed-element / parsed-CSS evidence -> static_indication; regex hits in code
-> heuristic_inference. Static detectors never set verification (pass/fail) except
LANG-05 (resource coverage), whose explicit test is itself static.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import lang as L
from ..observations import Observation, src_evidence
from . import css as C
from . import i18n as I
from .parse import Element, parse_jsx, parse_markup
from .scanner import ScanResult, read_text, scan

MAX_EVIDENCE = 8
FIELD_TAGS = {"input", "select", "textarea"}
NON_LABEL_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset"}
PERSONAL_HINTS = re.compile(r"(^|[_\-\s])(name|first.?name|last.?name|full.?name|email|e-?mail|phone|mobile|tel|"
                            r"address|street|city|pin.?code|postal|zip|username|user.?id|dob|birth|country)($|[_\-\s])", re.I)
OVERLAY_HOSTS = ["acsbapp.com", "accessibe.com", "userway.org", "equalweb", "audioeye", "reciteme",
                 "recite.me", "maxaccess", "enable.co.il", "accessibilityservice", "userway", "eye-able",
                 "monsido", "purple-lens", "onlineada"]
FOCUS_LIBS = ["focus-trap", "focus-trap-react", "react-focus-lock", "focus-lock", "@headlessui/react",
              "@headlessui/vue", "@radix-ui/react-dialog", "@reach/dialog", "react-aria", "@react-aria/dialog",
              "a11y-dialog", "micromodal", "@angular/cdk", "@mui/material", "@chakra-ui/react", "react-modal",
              "@reach/dialog", "vue-final-modal", "bootstrap"]
DRAG_LIBS = ["react-dnd", "react-beautiful-dnd", "@hello-pangea/dnd", "@dnd-kit/core", "sortablejs", "vuedraggable",
             "dragula", "interactjs", "@angular/cdk", "react-sortable-hoc", "jquery-ui"]
CAPTCHA_RX = re.compile(r"g-recaptcha|grecaptcha|recaptcha|h-captcha|hcaptcha|cf-turnstile|turnstile|captcha", re.I)


@dataclass
class SourceIndex:
    root: str
    scan: ScanResult
    texts: dict = field(default_factory=dict)
    elements: list = field(default_factory=list)
    css: C.CssSummary = field(default_factory=C.CssSummary)
    deps: dict = field(default_factory=dict)
    i18n: I.I18nResult | None = None
    parse_errors: list = field(default_factory=list)
    files_parsed: dict = field(default_factory=dict)

    def code_files(self, cats=("template", "jsx", "script", "server")) -> list[str]:
        return [f.rel for f in self.scan.included(*cats)]

    def grep(self, rx: str | re.Pattern, cats=("template", "jsx", "script", "server"), limit: int = MAX_EVIDENCE,
             flags=re.I) -> list[dict]:
        c = re.compile(rx, flags) if isinstance(rx, str) else rx
        hits = []
        for rel in self.code_files(cats):
            t = self.texts.get(rel)
            if not t:
                continue
            for m in c.finditer(t):
                line = t.count("\n", 0, m.start()) + 1
                ls = t.rfind("\n", 0, m.start()) + 1
                le = t.find("\n", m.end())
                hits.append(src_evidence(rel, line, excerpt=t[ls: le if le > 0 else None].strip()))
                if len(hits) >= limit:
                    return hits
                break  # one hit per file keeps evidence diverse
        return hits

    def els(self, *tags: str) -> list[Element]:
        return [e for e in self.elements if e.kind in tags or e.tag in tags]

    def children(self, el: Element) -> list[Element]:
        return [e for e in self.elements if e.file == el.file and e.parent_idx == el.idx]

    def has_ui_source(self) -> bool:
        return any(f.category in ("template", "jsx", "style") for f in self.scan.included())


def build_index(root: str, cfg: dict) -> SourceIndex:
    sr = scan(root, cfg)
    idx = SourceIndex(root=root, scan=sr)
    for f in sr.included():
        if f.category in ("binary", "directory"):
            continue
        try:
            idx.texts[f.rel] = read_text(root, f.rel)
        except OSError as exc:
            idx.parse_errors.append({"path": f.rel, "error": str(exc)})
    for f in sr.included("template", "jsx", "script", "svg"):
        text = idx.texts.get(f.rel, "")
        try:
            if f.category in ("template", "svg"):
                els = parse_markup(text, f.rel)
                kind = "markup"
            else:
                if f.category == "script" and not re.search(r"<[A-Za-z][\w.]*[\s/>]", text):
                    continue
                els = parse_jsx(text, f.rel)
                kind = "jsx"
            idx.elements.extend(els)
            idx.files_parsed[kind] = idx.files_parsed.get(kind, 0) + 1
            if f.rel.lower().endswith((".vue", ".svelte", ".html", ".htm")):
                for m in re.finditer(r"(?is)<style\b([^>]*)>(.*?)</style>", text):
                    lang_attr = re.search(r"lang=['\"]?(\w+)", m.group(1) or "")
                    pseudo = f.rel + ("#style." + lang_attr.group(1) if lang_attr else "#style.css")
                    C.parse_css(m.group(2), pseudo if lang_attr else pseudo.replace("#style.css", ".css"), idx.css)
        except Exception as exc:  # malformed source must not stop the project
            idx.parse_errors.append({"path": f.rel, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    for f in sr.included("style"):
        try:
            C.parse_css(idx.texts.get(f.rel, ""), f.rel, idx.css)
        except Exception as exc:
            idx.parse_errors.append({"path": f.rel, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    idx.deps = I.parse_dependencies(sr, idx.texts)
    idx.i18n = I.analyse(sr, idx.texts, idx.deps)
    return idx


def _el_ev(e: Element, note: str = "") -> dict:
    attrs = " ".join(f'{k}="{v}"' if v is not None else k for k, v in list(e.attrs.items())[:6])
    dyn = " ".join(f":{d}" for d in sorted(e.dynamic)[:4])
    return src_evidence(e.file, e.line, e.end_line, f"<{e.tag} {attrs} {dyn}>{e.text[:80]}", note)


class StaticDetectors:
    """Each method returns one Observation for the source interface."""

    def __init__(self, idx: SourceIndex, project_id: str, interface_id: str, cfg: dict, meta: dict, commit: str | None):
        self.idx, self.pid, self.iid, self.cfg, self.meta = idx, project_id, interface_id, cfg, meta
        self.unit = {"kind": "repository", "source": idx.root if not commit else f"commit:{commit}", "commit": commit}
        self.kw = cfg["keywords"]

    def obs(self, fid: str, check: str, availability: str, rationale: str, evidence=None, measurements=None,
            grade: str = "static_indication", verification: str = "not_tested", applicability: str = "",
            manual: bool = False) -> Observation:
        return Observation(project_id=self.pid, interface_id=self.iid, interface_type="source_repository",
                           feature_id=fid, check_id=check, method="static", evidence_grade=grade,
                           availability=availability, verification=verification, rationale=rationale,
                           unit=dict(self.unit), applicability_reason=applicability,
                           evidence=(evidence or [])[:MAX_EVIDENCE], measurements=measurements or {},
                           manual_review=manual)

    def none_scope(self, what: str) -> str:
        cov = self.idx.scan.coverage()
        return f"no {what} found in {cov['files_inspected']} inspected first-party files"

    # ---------------- LANGUAGE
    def lang_switcher(self) -> Observation:
        ev, n_opts = [], 0
        for sel in self.idx.els("select"):
            opts = [c for c in self.idx.children(sel) if c.tag == "option"]
            langs = {L.recognise_language_label(o.text or (o.get("value") or "")) for o in opts} - {None}
            if len(langs) >= 2:
                ev.append(_el_ev(sel, f"select with language options {sorted(langs)}"))
                n_opts = max(n_opts, len(langs))
        hreflang = [e for e in self.idx.els("a", "link") if e.has("hreflang") and e.tag == "a"]
        if hreflang:
            ev += [_el_ev(e, "link with hreflang") for e in hreflang[:3]]
        groups: dict = {}
        for a in self.idx.els("a", "button"):
            code = L.recognise_language_label(a.text) or L.recognise_language_label(a.get("lang") or "")
            if code:
                groups.setdefault((a.file, a.parent_idx if a.parent_idx >= 0 else a.line), set()).add(code)
        for (file, _), codes in groups.items():
            if len(codes) >= 2:
                ev.append(src_evidence(file, next(a.line for a in self.idx.els("a", "button") if a.file == file),
                                       note=f"sibling links/buttons naming languages {sorted(codes)}"))
        labelled = [e for e in self.idx.elements if re.search(r"language|भाषा|langue|idioma|sprache",
                                                                  (e.get("aria-label") or "") + " " + (e.get("title") or ""), re.I)]
        ev += [_el_ev(e, "control labelled as language selector") for e in labelled[:2]]
        calls = self.idx.grep(r"\bchangeLanguage\(|\bsetLocale\(|\bsetLanguage\(|\$i18n\.locale\s*=|\bi18n\.locale\s*=|"
                              r"\blocale\.value\s*=|set_language|get_available_languages|I18n\.locale\s*=|\buseLocale\(|"
                              r"switchLocalePath\(|router\.push\([^)]*locale|App\.setLocale|setAppLocale")
        if ev:
            return self.obs("LANG-01", "st.lang_switcher", "present",
                            f"parsed UI element(s) offering language choice ({len(ev)} evidence items)" +
                            (f"; {len(calls)} locale-change call site(s)" if calls else ""),
                            ev + calls[:3], {"switcher_elements": len(ev), "locale_change_calls": len(calls)})
        if calls:
            return self.obs("LANG-01", "st.lang_switcher", "present",
                            "locale-change call(s) found but no parsed UI control identified (heuristic)",
                            calls, {"locale_change_calls": len(calls)}, grade="heuristic_inference", manual=True)
        return self.obs("LANG-01", "st.lang_switcher", "not_found_in_scope", self.none_scope("language switcher"))

    def i18n_features(self) -> list[Observation]:
        r = self.idx.i18n
        m = r.to_measurements()
        out = []
        res_ev = [src_evidence(x.rel, 1, note=f"locale={x.locale} keys={len(x.keys)}") for x in r.resources[:5]]
        call_ev = [src_evidence(c["path"], c["line"], note=c["pattern"]) for c in r.call_sites[:3]]
        if not r.resources:
            out.append(self.obs("LANG-05", "st.i18n_resources", "not_found_in_scope",
                                self.none_scope("translation resources"), measurements=m))
        elif r.used:
            locs = [l for l in r.coverage]
            covs = [r.coverage[l]["coverage"] for l in locs]
            thr = float(self.cfg["static"]["translation_coverage_pass"])
            if not locs:
                ver, why = "not_tested", "only one locale"
            elif all(c >= thr for c in covs):
                ver, why = "pass", f"all {len(locs)} non-base locale(s) >= {thr:.0%} key coverage"
            elif any(c >= thr for c in covs):
                ver, why = "partial", f"{sum(c >= thr for c in covs)}/{len(locs)} non-base locales >= {thr:.0%} coverage"
            else:
                ver, why = "fail", f"no non-base locale reaches {thr:.0%} coverage"
            out.append(self.obs("LANG-05", "st.i18n_resources", "present",
                                f"translation resources used ({r.usage_reason}); {why}", res_ev + call_ev, m,
                                verification=ver))
        else:
            out.append(self.obs("LANG-05", "st.i18n_resources", "not_found_in_scope", r.usage_reason, res_ev, m,
                                manual=True))
        locs_used = [l for l in r.locales if l not in ("default", "template")] if r.used else []
        if len(locs_used) >= 2:
            out.append(self.obs("LANG-04", "st.i18n_resources", "present",
                                f"{len(locs_used)} locales in used translation resources (claimed, not observed): {', '.join(locs_used[:12])}",
                                res_ev, {"claimed_locales": locs_used}))
        elif r.resources:
            out.append(self.obs("LANG-04", "st.i18n_resources", "not_found_in_scope",
                                "fewer than two locales in used translation resources" if r.used else
                                "translation resources unused; no languages counted", res_ev, {"claimed_locales": locs_used}))
        else:
            out.append(self.obs("LANG-04", "st.i18n_resources", "not_found_in_scope", self.none_scope("translation resources")))
        return out

    def html_lang(self) -> Observation:
        htmls = self.idx.els("html")
        if not htmls:
            return self.obs("LANG-07", "st.html_lang", "unknown", "no <html> root template in inspected scope "
                            "(may be generated at build time)")
        with_lang = [e for e in htmls if e.has("lang") or e.has("xml:lang")]
        if with_lang:
            return self.obs("LANG-07", "st.html_lang", "present",
                            f"{len(with_lang)}/{len(htmls)} html root template(s) set lang",
                            [_el_ev(e) for e in with_lang], {"html_templates": len(htmls), "with_lang": len(with_lang)})
        return self.obs("LANG-07", "st.html_lang", "not_found_in_scope", f"{len(htmls)} html root template(s) without lang",
                        [_el_ev(e) for e in htmls], {"html_templates": len(htmls), "with_lang": 0})

    def rtl(self) -> Observation:
        rtl_locales = [l for l in (self.idx.i18n.locales or {}) if L.is_rtl(l)]
        exp = [l for l in (self.meta.get("expected_languages") or []) if L.is_rtl(l)]
        if not rtl_locales and not exp:
            return self.obs("LANG-09", "st.rtl", "not_applicable", "no RTL locale in resources or expected_languages",
                            applicability="no RTL language expected or shipped")
        ev = [src_evidence(r.file, r.line, note=f"RTL selector {r.selector[:60]}") for r in C.rtl_selectors(self.idx.css)[:4]]
        ev += self.idx.grep(r"document\.dir\b|\bdir\s*=\s*\{|:dir=|\bi18n\.dir\(|dir=\"rtl\"|getDirection\(|isRtl|rtlcss|stylis-plugin-rtl")
        lp = C.logical_property_count(self.idx.css)
        why = f"RTL relevance: locales {rtl_locales or '-'} / expected {exp or '-'}"
        if ev:
            return self.obs("LANG-09", "st.rtl", "present", f"{why}; RTL handling found", ev,
                            {"logical_properties": lp}, grade="static_indication" if C.rtl_selectors(self.idx.css) else "heuristic_inference")
        return self.obs("LANG-09", "st.rtl", "not_found_in_scope", f"{why}; no RTL handling found", [], {"logical_properties": lp})

    def locale_format(self) -> Observation:
        hits = self.idx.grep(r"Intl\.(DateTimeFormat|NumberFormat|RelativeTimeFormat)|toLocale(Date|Time)?String\(|"
                             r"date-fns/locale|moment\.locale\(|dayjs\.locale\(|format_date\(|format_decimal\(|"
                             r"babel\.dates|number_format\(|\bl10n\b|localize\(\s*date|DateFormat\(|NumberFormat\.getInstance")
        if hits:
            return self.obs("LANG-10", "st.locale_format", "present", f"locale-aware formatting call(s) in {len(hits)} file(s) (heuristic)",
                            hits, grade="heuristic_inference", manual=True)
        return self.obs("LANG-10", "st.locale_format", "not_found_in_scope", self.none_scope("locale-aware formatting calls"),
                        grade="heuristic_inference")

    # ---------------- NAVIGATION
    def landmarks(self) -> Observation:
        mains = [e for e in self.idx.elements if e.tag == "main" or (e.get("role") or "") == "main"]
        navs = [e for e in self.idx.elements if e.tag == "nav" or (e.get("role") or "") == "navigation"]
        if mains:
            return self.obs("NAV-01", "st.landmarks", "present", f"{len(mains)} main and {len(navs)} navigation landmark element(s) in templates",
                            [_el_ev(e) for e in mains[:3] + navs[:3]], {"main": len(mains), "nav": len(navs)})
        return self.obs("NAV-01", "st.landmarks", "not_found_in_scope", self.none_scope("main landmark element"),
                        [_el_ev(e) for e in navs[:3]], {"main": 0, "nav": len(navs)})

    def skip_link(self) -> Observation:
        cues = [re.escape(k) for k in self.kw["skip"]]
        rx = re.compile("|".join(cues), re.I)
        hits = [e for e in self.idx.els("a") if (e.get("href") or "").startswith("#") and
                (rx.search(e.text) or rx.search(e.get("aria-label") or "") or re.search(r"skip", e.get("class") or "", re.I))]
        if hits:
            return self.obs("NAV-06", "st.skip_link", "present", f"{len(hits)} in-page skip link(s) in templates",
                            [_el_ev(e) for e in hits])
        return self.obs("NAV-06", "st.skip_link", "not_found_in_scope", self.none_scope("skip link"))

    def breadcrumb(self) -> Observation:
        hits = [e for e in self.idx.elements if re.search(r"breadcrumb", (e.get("aria-label") or "") + " " + (e.get("class") or "") + " " + e.tag, re.I)
                or (e.get("itemtype") or "").endswith("BreadcrumbList")]
        if hits:
            return self.obs("NAV-07", "st.breadcrumb", "present", f"{len(hits)} breadcrumb element(s)/component(s)", [_el_ev(e) for e in hits])
        return self.obs("NAV-07", "st.breadcrumb", "not_found_in_scope", self.none_scope("breadcrumb"))

    def search(self) -> Observation:
        hits = [e for e in self.idx.elements if e.tag == "search" or (e.get("role") or "") == "search" or
                (e.tag == "input" and ((e.get("type") or "") == "search" or (e.get("name") or "") in ("q", "query", "search", "s", "keyword", "keywords")))]
        if hits:
            return self.obs("NAV-08", "st.search", "present", f"{len(hits)} search control(s) in templates", [_el_ev(e) for e in hits])
        return self.obs("NAV-08", "st.search", "not_found_in_scope", self.none_scope("search control"))

    # ---------------- KEYBOARD / ASSISTIVE
    def focus_css(self) -> Observation:
        fr = self.idx.css.focus_rules()
        visible = [r for r in fr if C.has_visible_focus_style(r.decls)]
        removed = [r for r in fr if C.removes_outline(r.decls) and not C.has_visible_focus_style(r.decls)]
        global_removal = [r for r in self.idx.css.rules if C.removes_outline(r.decls) and re.search(r"(^|,)\s*(\*|a|button|input)?\s*:focus\b", r.selector)
                          and not C.has_visible_focus_style(r.decls)]
        m = {"focus_rules": len(fr), "visible_focus_rules": len(visible), "outline_removed_without_replacement": len(removed),
             "css_files_parsed": self.idx.css.files_parsed, "css_files_heuristic": self.idx.css.files_heuristic}
        ev = [src_evidence(r.file, r.line, note=f"{r.selector[:60]} {{{'; '.join(f'{k}:{v}' for k, v in list(r.decls.items())[:3])}}}") for r in visible[:4]]
        ev += [src_evidence(r.file, r.line, note=f"candidate issue: outline removed without replacement in {r.selector[:60]}") for r in (global_removal or removed)[:4]]
        grade = "static_indication" if self.idx.css.files_parsed else "heuristic_inference"
        if not self.idx.css.rules:
            return self.obs("KBD-03", "st.focus_css", "unknown", "no first-party CSS inspected", measurements=m)
        if visible:
            return self.obs("KBD-03", "st.focus_css", "present", f"{len(visible)} focus rule(s) with visible styling; "
                            f"{len(removed)} rule(s) remove the outline without replacement (candidate issues)", ev, m, grade=grade)
        return self.obs("KBD-03", "st.focus_css", "not_found_in_scope", f"no visible focus styling in {len(fr)} focus rule(s); "
                        f"{len(removed)} outline removal(s) without replacement", ev, m, grade=grade, manual=bool(removed))

    def dialog_focus(self) -> Observation:
        dialogs = [e for e in self.idx.elements if e.tag == "dialog" or (e.get("role") or "") in ("dialog", "alertdialog")
                   or re.search(r"\bmodal\b", (e.get("class") or "") + " " + (e.get("data-toggle") or "") + " " + (e.get("data-bs-toggle") or ""), re.I)
                   or e.tag in ("modal", "dialog", "drawer")]
        code = self.idx.grep(r"\.showModal\(|aria-modal|createFocusTrap|<FocusTrap|FocusLock|useFocusTrap|cdkTrapFocus|focus-trap")
        libs = [l for l in FOCUS_LIBS if l in self.idx.deps.get("npm", [])]
        if not dialogs and not code:
            return self.obs("KBD-05", "st.dialog_focus", "not_applicable", self.none_scope("dialog/modal"),
                            applicability="no dialog indicators in inspected source")
        if code:
            return self.obs("KBD-05", "st.dialog_focus", "present", f"focus-management code in {len(code)} file(s); dialog elements: {len(dialogs)}",
                            code + [_el_ev(e) for e in dialogs[:2]], {"dialogs": len(dialogs), "focus_libs": libs}, grade="heuristic_inference")
        return self.obs("KBD-05", "st.dialog_focus", "not_found_in_scope",
                        f"{len(dialogs)} dialog element(s) but no focus-management code found" + (f"; libraries declared: {libs} (dependency is not implementation)" if libs else ""),
                        [_el_ev(e) for e in dialogs[:4]], {"dialogs": len(dialogs), "focus_libs": libs}, grade="heuristic_inference", manual=True)

    def img_alt(self) -> Observation:
        imgs = [e for e in self.idx.elements if e.kind == "img"]
        if not imgs:
            return self.obs("KBD-09", "st.img_alt", "not_applicable", self.none_scope("img elements"), applicability="no images in templates")
        with_alt = [e for e in imgs if e.has("alt")]
        decorative = [e for e in imgs if e.get("alt") == "" or (e.get("role") or "") in ("presentation", "none") or e.get("aria-hidden") == "true"]
        missing = [e for e in imgs if not e.has("alt") and not e.has_spread and not e.has("aria-label") and not e.has("aria-labelledby")
                   and (e.get("role") or "") not in ("presentation", "none")]
        m = {"images": len(imgs), "with_alt": len(with_alt), "decorative_marked": len(decorative), "missing_alt_candidates": len(missing),
             "spread_props_unknown": sum(1 for e in imgs if e.has_spread and not e.has("alt"))}
        ev = [_el_ev(e, "candidate issue: no alt") for e in missing[:5]] + [_el_ev(e) for e in with_alt[:3]]
        if with_alt or decorative:
            return self.obs("KBD-09", "st.img_alt", "present", f"{len(with_alt)}/{len(imgs)} image elements have alt; {len(missing)} candidate(s) without",
                            ev, m)
        return self.obs("KBD-09", "st.img_alt", "not_found_in_scope", f"none of {len(imgs)} image elements has alt", ev, m)

    def form_labels(self) -> Observation:
        fields = [e for e in self.idx.elements if e.tag in FIELD_TAGS and (e.get("type") or "text").lower() not in NON_LABEL_INPUT_TYPES]
        comp_fields = [e for e in self.idx.elements if e.syntax == "jsx" and re.match(r"(text|input|select|date|form)?field$|^(textinput|textarea|select|input|checkbox|radio)$", e.tag) and e.tag not in FIELD_TAGS]
        if not fields and not comp_fields:
            return self.obs("KBD-10", "st.form_labels", "not_applicable", self.none_scope("form fields"), applicability="no form fields in templates")
        label_for = {(e.file, e.get("for")) for e in self.idx.els("label") if e.get("for")}
        labelled, unlabelled = [], []
        for e in fields:
            ok = (e.has("aria-label") or e.has("aria-labelledby") or e.has("title") or "label" in e.parent_tags
                  or (e.get("id") and (e.file, e.get("id")) in label_for) or (e.has("id") and "id" in e.dynamic))
            (labelled if ok else unlabelled).append(e)
        comp_ok = [e for e in comp_fields if e.has("label") or e.has("aria-label")]
        m = {"fields": len(fields), "labelled": len(labelled), "unlabelled_candidates": len(unlabelled),
             "placeholder_only_candidates": sum(1 for e in unlabelled if e.has("placeholder")),
             "component_fields": len(comp_fields), "component_fields_with_label_prop": len(comp_ok)}
        ev = [_el_ev(e, "candidate issue: no programmatic label") for e in unlabelled[:5]] + [_el_ev(e) for e in labelled[:2]]
        if labelled or comp_ok:
            return self.obs("KBD-10", "st.form_labels", "present", f"{len(labelled)}/{len(fields)} fields labelled; {len(unlabelled)} candidate(s) without label",
                            ev, m)
        return self.obs("KBD-10", "st.form_labels", "not_found_in_scope", f"none of {len(fields)} fields has a programmatic label", ev, m)

    def live_regions(self) -> Observation:
        hits = [e for e in self.idx.elements if e.has("aria-live") or (e.get("role") or "") in ("alert", "status", "log") or e.tag == "output"]
        code = self.idx.grep(r"aria-live|role=['\"](alert|status)|setAttribute\(\s*['\"]aria-live|announce\(|LiveAnnouncer|useAnnouncer")
        if hits or code:
            return self.obs("KBD-11", "st.live_regions", "present", f"{len(hits)} live-region element(s); {len(code)} file(s) with announcement code",
                            [_el_ev(e) for e in hits[:4]] + code[:3], {"live_region_elements": len(hits)},
                            grade="static_indication" if hits else "heuristic_inference")
        return self.obs("KBD-11", "st.live_regions", "not_found_in_scope", self.none_scope("live regions"))

    def drag(self) -> Observation:
        els = [e for e in self.idx.elements if e.get("draggable") == "true" or e.has("ondragstart") or e.has("ondrop")]
        libs = [l for l in DRAG_LIBS if l in self.idx.deps.get("npm", [])]
        code = self.idx.grep(r"useDrag\(|useDrop\(|Sortable\.create|new Sortable|<Draggable|<Droppable|DndContext|dragula\(|cdkDrag")
        if els or code:
            return self.obs("KBD-12", "st.drag", "present", f"drag interaction in source ({len(els)} elements, {len(code)} files)",
                            [_el_ev(e) for e in els[:3]] + code[:3], {"libs": libs}, grade="heuristic_inference", manual=True)
        return self.obs("KBD-12", "st.drag", "not_applicable", self.none_scope("drag-and-drop usage") + (f" (libraries declared but unused: {libs})" if libs else ""),
                        applicability="no drag interaction found", grade="heuristic_inference")

    def captcha(self) -> Observation:
        hits = [e for e in self.idx.elements if CAPTCHA_RX.search(" ".join(str(v) for v in e.attrs.values() if v) + " " + e.tag)]
        code = self.idx.grep(CAPTCHA_RX)
        if hits or code:
            return self.obs("KBD-13", "st.captcha", "present", "CAPTCHA usage in source (subject present)",
                            [_el_ev(e) for e in hits[:3]] + code[:3], grade="heuristic_inference", manual=True)
        return self.obs("KBD-13", "st.captcha", "not_applicable", self.none_scope("CAPTCHA"), applicability="no CAPTCHA found",
                        grade="heuristic_inference")

    # ---------------- VISUAL
    def reduced_motion(self) -> Observation:
        mq = self.idx.css.media_matching(r"prefers-reduced-motion")
        code = self.idx.grep(r"prefers-reduced-motion|useReducedMotion|matchMedia\(\s*['\"]\(prefers-reduced-motion")
        anim = [r for r in self.idx.css.rules if any(k.startswith("animation") or k.startswith("transition") for k in r.decls)]
        ev = [src_evidence(f, l, note=f"@media {p[:60]}") for p, f, l in mq[:4]] + code[:2]
        if mq or code:
            return self.obs("VIS-09", "st.reduced_motion_css", "present", f"{len(mq)} prefers-reduced-motion media quer(ies)", ev,
                            {"animated_rules": len(anim)}, grade="static_indication" if mq else "heuristic_inference")
        if anim:
            return self.obs("VIS-09", "st.reduced_motion_css", "not_found_in_scope", f"{len(anim)} animated/transitioned CSS rule(s) without prefers-reduced-motion handling",
                            [src_evidence(r.file, r.line, note=r.selector[:60]) for r in anim[:4]], {"animated_rules": len(anim)})
        return self.obs("VIS-09", "st.reduced_motion_css", "not_applicable", "no CSS animations/transitions in inspected styles",
                        applicability="no animations in source", measurements={"animated_rules": 0})

    def text_size_control(self) -> Observation:
        rx = re.compile(r"^\s*(a\s*[+\-]{1,2}|a\+|a-|a)\s*$", re.I)
        cues = re.compile("|".join(re.escape(k) for k in self.kw["text_size"] if len(k) > 3), re.I)
        els = [e for e in self.idx.els("button", "a") if (rx.match(e.text or "") and len(e.text or "") <= 4 and e.text.strip().lower() != "a")
               or cues.search((e.get("aria-label") or "") + " " + (e.get("title") or "") + " " + e.text)]
        code = self.idx.grep(r"documentElement\.style\.fontSize|body\.style\.fontSize|increaseFont|decreaseFont|fontResize|changeFontSize")
        if els or code:
            return self.obs("VIS-14", "st.text_size_control", "present", "text-size control in source", [_el_ev(e) for e in els[:3]] + code[:3],
                            grade="static_indication" if els else "heuristic_inference")
        return self.obs("VIS-14", "st.text_size_control", "not_found_in_scope", self.none_scope("text-size control"))

    def color_scheme(self) -> Observation:
        mq = self.idx.css.media_matching(r"prefers-color-scheme|forced-colors|prefers-contrast")
        code = self.idx.grep(r"data-theme|dark-?mode|high-?contrast|theme-toggle|toggleTheme|colorScheme")
        if mq or code:
            return self.obs("VIS-15", "st.color_scheme", "present", f"{len(mq)} colour-scheme media quer(ies); {len(code)} theme toggle file(s)",
                            [src_evidence(f, l, note=p[:60]) for p, f, l in mq[:3]] + code[:3], grade="static_indication" if mq else "heuristic_inference")
        return self.obs("VIS-15", "st.color_scheme", "not_found_in_scope", self.none_scope("colour-scheme support"))

    def overlay(self) -> Observation:
        rx = "|".join(re.escape(h) for h in OVERLAY_HOSTS)
        hits = self.idx.grep(rx)
        if hits:
            return self.obs("VIS-16", "st.overlay", "present", "third-party overlay script reference (descriptive; no credit)", hits, grade="heuristic_inference")
        return self.obs("VIS-16", "st.overlay", "not_found_in_scope", self.none_scope("overlay script references"), grade="heuristic_inference")

    def viewport_meta(self) -> list[Observation]:
        metas = [e for e in self.idx.els("meta") if (e.get("name") or "").lower() == "viewport"]
        if not metas:
            return [self.obs("VIS-03", "st.viewport_meta", "unknown", "no viewport meta in inspected templates (may be injected at build)")]
        content = [(e, (e.get("content") or "").lower().replace(" ", "")) for e in metas]
        responsive = [e for e, c in content if "width=device-width" in c]
        blocks = [e for e, c in content if "user-scalable=no" in c or "user-scalable=0" in c or re.search(r"maximum-scale=(0?\.\d+|1(\.0+)?)(,|$)", c)]
        out = [self.obs("VIS-03", "st.viewport_meta", "present" if responsive else "not_found_in_scope",
                        f"{len(responsive)}/{len(metas)} viewport meta tag(s) responsive", [_el_ev(e) for e in metas[:3]])]
        if blocks:
            out.append(self.obs("VIS-05", "st.viewport_meta", "present", "candidate issue: viewport meta restricts zoom (user-scalable=no / maximum-scale<2)",
                                [_el_ev(e, "zoom restricted") for e in blocks[:3]], {"zoom_restricting_meta": len(blocks)}, manual=True))
        return out

    def media_tracks(self) -> Observation:
        vids = [e for e in self.idx.elements if e.tag in ("video", "audio")]
        if not vids:
            return self.obs("VIS-11", "st.media_tracks", "not_applicable", self.none_scope("video/audio elements"), applicability="no media elements")
        tracks = [e for e in self.idx.els("track") if (e.get("kind") or "subtitles") in ("captions", "subtitles")]
        if tracks:
            return self.obs("VIS-11", "st.media_tracks", "present", f"{len(tracks)} caption/subtitle track(s) for {len(vids)} media element(s)", [_el_ev(e) for e in tracks[:3]])
        return self.obs("VIS-11", "st.media_tracks", "not_found_in_scope", f"{len(vids)} media element(s) without caption tracks", [_el_ev(e) for e in vids[:3]])

    # ---------------- FORMS
    def autocomplete(self) -> Observation:
        fields = [e for e in self.idx.els("input") if (e.get("type") or "text").lower() not in NON_LABEL_INPUT_TYPES]
        personal = [e for e in fields if (e.get("type") or "").lower() in ("email", "tel", "password") or
                    PERSONAL_HINTS.search(" ".join(filter(None, [e.get("name"), e.get("id"), e.get("placeholder")])))]
        if not personal:
            return self.obs("FORM-06", "st.autocomplete", "not_applicable", self.none_scope("personal-information fields"),
                            applicability="no personal-information fields detected", grade="heuristic_inference")
        with_ac = [e for e in personal if e.has("autocomplete") and (e.get("autocomplete") or "x") not in ("off", "false")]
        m = {"personal_fields": len(personal), "with_autocomplete": len(with_ac)}
        ev = [_el_ev(e) for e in with_ac[:3]] + [_el_ev(e, "candidate: no autocomplete token") for e in personal if e not in with_ac][:3]
        return self.obs("FORM-06", "st.autocomplete", "present" if with_ac else "not_found_in_scope",
                        f"{len(with_ac)}/{len(personal)} personal fields carry autocomplete tokens", ev, m)

    def paste_block(self) -> Observation:
        pw = [e for e in self.idx.els("input") if (e.get("type") or "").lower() == "password"]
        blocks = [e for e in self.idx.elements if re.search(r"return\s+false|preventDefault", (e.get("onpaste") or "") + (e.get("oncopy") or ""))
                  or "onpaste" in e.dynamic]
        code = self.idx.grep(r"addEventListener\(\s*['\"]paste['\"][^)]*preventDefault|onpaste\s*=|on\(\s*['\"]paste|\.bind\(\s*['\"]paste|onPaste=\{[^}]*preventDefault")
        if not pw:
            return self.obs("FORM-08", "st.paste_block", "not_applicable", self.none_scope("password fields"), applicability="no password fields in templates")
        if blocks or code:
            return self.obs("FORM-08", "st.paste_block", "not_found_in_scope", "candidate issue: paste-blocking code near authentication fields",
                            [_el_ev(e) for e in blocks[:3]] + code[:3], {"password_fields": len(pw)}, grade="heuristic_inference", manual=True)
        return self.obs("FORM-08", "st.paste_block", "unknown", f"{len(pw)} password field(s); no paste-blocking code found (does not prove paste works)",
                        [_el_ev(e) for e in pw[:2]], {"password_fields": len(pw)})

    def timeout(self) -> Observation:
        metas = [e for e in self.idx.els("meta") if (e.get("http-equiv") or "").lower() == "refresh"]
        cues = re.compile("|".join(re.escape(k) for k in self.kw["timeout"]), re.I)
        warn = self.idx.grep(cues, cats=("template", "jsx", "script", "translation"))
        timers = self.idx.grep(r"react-idle-timer|useIdleTimer|@ng-idle|idleTimeout|sessionTimeout|SESSION_TIMEOUT|session_timeout")
        if warn:
            return self.obs("FORM-09", "st.timeout", "present", "timeout warning text in source (heuristic)", warn + timers[:2], grade="heuristic_inference", manual=True)
        if metas or timers:
            return self.obs("FORM-09", "st.timeout", "not_found_in_scope", "time limit indicators without warning text",
                            [_el_ev(e) for e in metas[:2]] + timers[:3], {"meta_refresh": len(metas)}, grade="heuristic_inference", manual=True)
        return self.obs("FORM-09", "st.timeout", "not_applicable", self.none_scope("time-limit indicators"), applicability="no time limits in source",
                        grade="heuristic_inference")

    def save_resume(self) -> Observation:
        lp = self.meta.get("long_process")
        if lp is False:
            return self.obs("FORM-10", "st.save_resume", "not_applicable", "metadata long_process=false", applicability="metadata long_process=false")
        cues = re.compile("|".join(re.escape(k) for k in self.kw["save_resume"]), re.I)
        hits = self.idx.grep(cues, cats=("template", "jsx", "script", "translation"))
        note = "" if lp else " (relevance unknown: long_process not supplied)"
        if hits:
            return self.obs("FORM-10", "st.save_resume", "present", "save/resume wording in source (heuristic)" + note, hits, grade="heuristic_inference", manual=True)
        return self.obs("FORM-10", "st.save_resume", "not_found_in_scope", self.none_scope("save/resume wording") + note, grade="heuristic_inference")

    # ---------------- RESOURCES / TRUST
    def service_worker(self) -> Observation:
        if self.meta.get("offline_relevant") is False:
            return self.obs("RES-02", "st.service_worker", "not_applicable", "metadata offline_relevant=false", applicability="metadata offline_relevant=false")
        reg = self.idx.grep(r"serviceWorker\.register\(|workbox|registerSW\(|vite-plugin-pwa|next-pwa|@angular/service-worker|sw-precache")
        manifests = [f.rel for f in self.idx.scan.included("manifest") if f.rel.endswith(("manifest.json", ".webmanifest"))]
        if reg:
            return self.obs("RES-02", "st.service_worker", "present", f"service worker registration in source; manifests: {manifests[:2]}", reg,
                            grade="heuristic_inference")
        return self.obs("RES-02", "st.service_worker", "not_found_in_scope", self.none_scope("service worker registration"), grade="heuristic_inference")

    def trust_links(self) -> list[Observation]:
        out = []
        anchors = self.idx.els("a")
        for fid, key in (("TRUST-05", "privacy"), ("TRUST-07", "accessibility_statement"), ("TRUST-08", "grievance")):
            rx = re.compile("|".join(re.escape(k) for k in self.kw[key]), re.I)
            hits = [e for e in anchors if rx.search(e.text or "") or rx.search(e.get("href") or "") or rx.search(e.get("aria-label") or "")]
            if hits:
                out.append(self.obs(fid, "st.trust_links", "present", f"{len(hits)} link(s) matching {key} cues in templates (heuristic)",
                                    [_el_ev(e) for e in hits[:4]], grade="heuristic_inference"))
            else:
                out.append(self.obs(fid, "st.trust_links", "not_found_in_scope", self.none_scope(f"{key} links"), grade="heuristic_inference"))
        return out

    def run_all(self) -> list[Observation]:
        out: list[Observation] = []
        steps = [self.lang_switcher, self.i18n_features, self.html_lang, self.rtl, self.locale_format, self.landmarks,
                 self.skip_link, self.breadcrumb, self.search, self.focus_css, self.dialog_focus, self.img_alt,
                 self.form_labels, self.live_regions, self.drag, self.captcha, self.reduced_motion, self.text_size_control,
                 self.color_scheme, self.overlay, self.viewport_meta, self.media_tracks, self.autocomplete,
                 self.paste_block, self.timeout, self.save_resume, self.service_worker, self.trust_links]
        for step in steps:
            try:
                r = step()
                out.extend(r if isinstance(r, list) else [r])
            except Exception as exc:  # isolate detector errors; never silently drop
                out.append(Observation(project_id=self.pid, interface_id=self.iid, interface_type="source_repository",
                                       feature_id="LANG-01" if step is self.lang_switcher else _step_feature(step.__name__),
                                       check_id=f"st.{step.__name__}", method="static", evidence_grade="static_indication",
                                       availability="unknown", verification="not_tested",
                                       rationale=f"detector error: {type(exc).__name__}: {str(exc)[:200]}",
                                       unit=dict(self.unit), manual_review=True))
        if not self.idx.has_ui_source():
            # No templates/components/styles at all: UI features are not "absent", their
            # applicability is unknown (the repository may be backend-only or the UI may live elsewhere).
            for o in out:
                if o.feature_id not in NON_UI_STATIC and o.availability in ("not_found_in_scope", "not_applicable"):
                    o.availability = "unknown"
                    o.applicability_reason = ""
                    o.rationale = ("no web UI templates/components/styles in the inspected first-party scope; "
                                   "UI applicability unknown (not inferred). Detector note: " + o.rationale)
        return out


NON_UI_STATIC = {"LANG-04", "LANG-05"}

_STEP_FEATURES = {"i18n_features": "LANG-05", "html_lang": "LANG-07", "rtl": "LANG-09", "locale_format": "LANG-10",
                  "landmarks": "NAV-01", "skip_link": "NAV-06", "breadcrumb": "NAV-07", "search": "NAV-08",
                  "focus_css": "KBD-03", "dialog_focus": "KBD-05", "img_alt": "KBD-09", "form_labels": "KBD-10",
                  "live_regions": "KBD-11", "drag": "KBD-12", "captcha": "KBD-13", "reduced_motion": "VIS-09",
                  "text_size_control": "VIS-14", "color_scheme": "VIS-15", "overlay": "VIS-16", "viewport_meta": "VIS-03",
                  "media_tracks": "VIS-11", "autocomplete": "FORM-06", "paste_block": "FORM-08", "timeout": "FORM-09",
                  "save_resume": "FORM-10", "service_worker": "RES-02", "trust_links": "TRUST-05"}


def _step_feature(name: str) -> str:
    return _STEP_FEATURES.get(name, "LANG-01")


def static_coverage(idx: SourceIndex) -> dict:
    cov = idx.scan.coverage()
    cov.update({"elements_parsed": len(idx.elements), "files_parsed": idx.files_parsed,
                "css_rules": len(idx.css.rules), "css_files_parsed": idx.css.files_parsed,
                "css_files_heuristic": idx.css.files_heuristic, "parse_errors": len(idx.parse_errors),
                "parse_error_examples": idx.parse_errors[:5], "ui_source_detected": idx.has_ui_source(),
                "dependencies": {k: len(v) for k, v in idx.deps.items()}})
    return cov
