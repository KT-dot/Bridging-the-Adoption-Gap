"""Language-switcher discovery and functional test (LANG-01/02/03/06/09).

A globe icon is not evidence: a candidate must expose options that name languages
(endonyms/exonyms from CLDR, codes, hreflang or lang attributes). Functioning is
measured by visible text change and language metadata, then persistence across an
internal navigation and a reload.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .. import lang as L
from .net import host_of, same_site

CUE_RX = re.compile(r"\blanguage\b|\blang\b|भाषा|ভাষা|மொழி|భాష|ಭಾಷೆ|ഭാഷ|ભાષા|ਭਾਸ਼ਾ|ଭାଷା|idioma|langue|sprache|translate|globe", re.I)


def page_language(snap: dict) -> str | None:
    tag = L.primary(snap.get("html_lang"))
    if tag:
        return tag
    text = " ".join(s["t"] for s in snap.get("text", [])[:400])
    dom = L.dominant_script(text)
    if dom == "Latn":
        g, _ = L.stopword_guess(text)
        return g
    return {"Deva": "hi", "Beng": "bn", "Taml": "ta", "Telu": "te", "Knda": "kn", "Mlym": "ml", "Gujr": "gu",
            "Guru": "pa", "Orya": "or", "Arab": "ar"}.get(dom)


def find_switchers(snap: dict) -> list[dict]:
    base = page_language(snap)
    cands: list[dict] = []
    for s in snap.get("selects", []):
        if not s.get("visible"):
            continue
        opts = []
        for o in s.get("options", []):
            code = L.recognise_language_label(o["text"]) or L.recognise_language_label(o.get("value") or "") or L.primary(o.get("lang"))
            if code:
                opts.append({"label": o["text"], "value": o["value"], "code": code, "selected": o.get("selected")})
        if len({o["code"] for o in opts}) >= 2:
            cands.append({"kind": "select", "selector": s["selector"], "name": s.get("name"), "options": opts})
    groups: dict[str, list] = defaultdict(list)
    for item in [dict(x, _k="link") for x in snap.get("links", [])] + [dict(x, _k="button") for x in snap.get("buttons", [])]:
        if not item.get("visible"):
            continue
        txt = (item.get("text") or "").strip()
        code = (L.recognise_language_label(txt) or L.recognise_language_label(item.get("aria_label") or "")
                or (L.primary(item.get("hreflang")) if item.get("hreflang") else None)
                or (L.primary(item.get("lang")) if item.get("lang") and len(txt) <= 30 else None))
        if code:
            groups[item.get("parent") or ""].append({"label": txt or item.get("name"), "selector": item["selector"],
                                                     "code": code, "href": item.get("abs") or item.get("href"), "kind": item["_k"]})
    for parent, opts in groups.items():
        codes = {o["code"] for o in opts} | ({base} if base else set())
        if len(codes) >= 2 and any(o["code"] != base for o in opts):
            cands.append({"kind": "links", "selector": parent, "options": opts})
    for b in snap.get("buttons", []) + [l for l in snap.get("links", []) if (l.get("href") or "").startswith(("#", "javascript"))]:
        if b.get("visible") and CUE_RX.search((b.get("name") or "") + " " + (b.get("text") or "")):
            cands.append({"kind": "toggle", "selector": b["selector"], "name": b.get("name"), "options": []})
    order = {"select": 0, "links": 1, "toggle": 2}
    cands.sort(key=lambda c: (order[c["kind"]], -len(c["options"])))
    return cands


def choose_option(cand: dict, base: str | None, expected: list[str] | None, want_rtl: bool = False) -> dict | None:
    opts = [o for o in cand["options"] if o["code"] and o["code"] != base]
    if want_rtl:
        opts = [o for o in opts if L.is_rtl(o["code"])]
    if not opts:
        return None
    if expected:
        for e in expected:
            for o in opts:
                if L.primary(e) == o["code"]:
                    return o
    return opts[0]


def in_language(snap: dict, target: str, base: str | None) -> tuple[bool, str]:
    tag = L.primary(snap.get("html_lang"))
    text = " ".join(s["t"] for s in snap.get("text", [])[:600])
    dom = L.dominant_script(text)
    ts, bs = L.likely_script(target), L.likely_script(base) if base else None
    if tag == target:
        return True, f"html lang={snap.get('html_lang')}"
    if ts and ts != bs and dom == ts:
        return True, f"dominant script {dom} matches {target}"
    if ts == "Latn" and dom == "Latn":
        g, _ = L.stopword_guess(text)
        if g == target:
            return True, f"stopword proxy suggests {g}"
    return False, f"html lang={snap.get('html_lang')}, dominant script={dom}"


def text_change(before: dict, after: dict, allow: set[str]) -> dict:
    b = [s["t"] for s in before.get("text", [])]
    a = [s["t"] for s in after.get("text", [])]
    aset = set(a)
    if not b:
        return {"segments_before": 0, "segments_after": len(a), "changed_ratio": None, "untranslated": [], "untranslated_ratio": None}
    unchanged = [t for t in b if t in aset]
    changed_ratio = 1 - len(unchanged) / len(b)

    def eligible(t: str) -> bool:
        if t.lower() in allow:
            return False
        if re.fullmatch(r"[\W\d_]+", t) or re.search(r"https?://|www\.|@\w", t):
            return False
        words = re.findall(r"[^\W\d_]+", t)
        return len(words) >= 2 or (len(words) == 1 and len(words[0]) > 3 and not words[0][:1].isupper())

    elig_before = [t for t in b if eligible(t)]
    untranslated = [t for t in elig_before if t in aset]
    return {"segments_before": len(b), "segments_after": len(a), "changed_ratio": round(changed_ratio, 4),
            "eligible_segments": len(elig_before), "untranslated": untranslated[:40],
            "untranslated_ratio": round(len(untranslated) / len(elig_before), 4) if elig_before else None}


def _activate(sess, page, cand: dict, opt: dict) -> str:
    sess.limiter.wait(host_of(page.url))
    if cand["kind"] == "select":
        page.locator(cand["selector"]).first.select_option(value=opt["value"], timeout=5000)
        return "select_option"
    href = opt.get("href") or ""
    if href and href.startswith("http") and not same_site(href, page.url):
        ok, why = sess.robots.allowed(href)
        if not ok:
            raise RuntimeError("language link target disallowed by robots.txt")
    page.locator(opt["selector"]).first.click(timeout=6000)
    return "click"


def run(sess, ctx, url: str, meta: dict, cfg: dict, shot_dir, max_options: int = 2) -> dict:
    out: dict = {"url": url, "found": False, "attempted": False}
    page = ctx.new_page()
    try:
        r = sess.goto(page, url)
        if not r["ok"]:
            out["error"] = r["error"]
            return out
        snap0 = sess.snapshot(page)
        base = page_language(snap0)
        out["base_language"] = base
        out["html_lang_before"] = snap0.get("html_lang")
        cands = find_switchers(snap0)
        if cands and cands[0]["kind"] == "toggle":
            # open the toggle (menu) and look again
            try:
                page.locator(cands[0]["selector"]).first.click(timeout=4000)
                page.wait_for_timeout(600)
                snap_open = sess.snapshot(page)
                inner = [c for c in find_switchers(snap_open) if c["kind"] != "toggle"]
                if inner:
                    cands = inner
                    out["opened_toggle"] = cands[0]["selector"]
            except Exception:
                pass
        real = [c for c in cands if c["kind"] != "toggle"]
        out["candidates"] = [{"kind": c["kind"], "selector": c["selector"], "options": [{"label": o["label"], "code": o["code"]} for o in c["options"]][:12]} for c in cands[:5]]
        out["found"] = bool(real)
        out["toggle_only"] = bool(cands) and not real
        if not real:
            return out
        cand = real[0]
        expected = meta.get("expected_languages") or []
        opt = choose_option(cand, base, expected)
        out["offered_languages"] = sorted({o["code"] for o in cand["options"]} | ({base} if base else set()))
        if not opt:
            out["note"] = "no option for a language different from the current page"
            return out
        allow = {x.lower() for x in (meta.get("untranslated_allowlist") or [])}
        res = switch_once(sess, page, url, snap0, cand, opt, base, allow, shot_dir, "switch1")
        out.update(res)
        out["attempted"] = True
        if res.get("observable_change") and res.get("after_ok"):
            try:
                out["persistence"] = persistence(sess, page, res["_after"], opt["code"], base, cand)
            except Exception as exc:
                out["persistence"] = {"navigation": {"tested": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"},
                                      "reload": {"tested": False, "reason": "not reached"}}
        rtl_opts = [o for o in cand["options"] if L.is_rtl(o["code"])]
        out["rtl_offered"] = [o["code"] for o in rtl_opts]
        if rtl_opts and max_options >= 2:
            if L.is_rtl(opt["code"]):
                after = res.get("_after") or {}
                out["rtl"] = {"option": opt["code"], "html_dir": after.get("html_dir"), "body_dir": after.get("body_dir"), "main_dir": after.get("main_dir")}
            else:
                p2 = ctx.new_page()
                try:
                    rr = sess.goto(p2, url)
                    if rr["ok"]:
                        s0 = sess.snapshot(p2)
                        c2 = next((c for c in find_switchers(s0) if c["kind"] != "toggle"), None)
                        o2 = choose_option(c2, base, None, want_rtl=True) if c2 else None
                        if o2:
                            r2 = switch_once(sess, p2, url, s0, c2, o2, base, allow, shot_dir, "switch_rtl")
                            a2 = r2.get("_after") or {}
                            out["rtl"] = {"option": o2["code"], "html_dir": a2.get("html_dir"), "body_dir": a2.get("body_dir"),
                                          "main_dir": a2.get("main_dir"), "changed_ratio": r2.get("changed_ratio")}
                finally:
                    p2.close()
        out.pop("_after", None)
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        out.pop("_after", None)
        return out
    finally:
        page.close()


def switch_once(sess, page, url, snap0, cand, opt, base, allow, shot_dir, label) -> dict:
    res = {"switcher": {"kind": cand["kind"], "selector": cand["selector"]}, "option": {"label": opt["label"], "code": opt["code"]}}
    url0 = page.url
    try:
        res["method"] = _activate(sess, page, cand, opt)
    except Exception as exc:
        res["activation_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return res
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass
    sess.settle(page)
    try:
        after = sess.snapshot(page)
    except Exception as exc:
        res["activation_error"] = f"snapshot after switch failed: {type(exc).__name__}"
        return res
    res["after_ok"] = True
    res["_after"] = after
    res["url_after"] = page.url
    res["url_changed"] = page.url != url0
    res["html_lang_after"] = after.get("html_lang")
    res["title_before"], res["title_after"] = snap0.get("title"), after.get("title")
    tc = text_change(snap0, after, allow)
    res.update(tc)
    ok, why = in_language(after, opt["code"], base)
    res["in_target_language"] = ok
    res["language_evidence"] = why
    res["lang_attr_matches"] = L.primary(after.get("html_lang")) == opt["code"]
    res["observable_change"] = bool((tc["changed_ratio"] or 0) > 0 or res["url_changed"] or after.get("html_lang") != snap0.get("html_lang"))
    res["text_sample_before"] = [s["t"] for s in snap0.get("text", [])[:8]]
    res["text_sample_after"] = [s["t"] for s in after.get("text", [])[:8]]
    res["screenshot"] = sess.screenshot(page, shot_dir / f"lang_{label}.jpg")
    return res


def persistence(sess, page, after: dict, target: str, base: str | None, cand: dict) -> dict:
    out = {}
    lang_sel = {o.get("selector") for o in cand["options"]} | {cand.get("selector")}
    link = None
    for l in after.get("links", []):
        href = l.get("abs") or ""
        if (l.get("visible") and l.get("internal") and l.get("region") in ("nav", "header") and l["selector"] not in lang_sel
                and href and not href.split("#")[0] == page.url.split("#")[0] and not (l.get("href") or "").startswith(("#", "javascript"))
                and not sess.policy.excluded(href)):
            link = l
            break
    if link is None:
        link = next((l for l in after.get("links", []) if l.get("visible") and l.get("internal") and l["selector"] not in lang_sel
                     and (l.get("abs") or "").split("#")[0] != page.url.split("#")[0] and not (l.get("href") or "").startswith(("#", "javascript"))
                     and not sess.policy.excluded(l.get("abs") or "")), None)
    if link is None:
        out["navigation"] = {"tested": False, "reason": "no internal link to follow"}
    else:
        try:
            sess.limiter.wait(host_of(page.url))
            page.locator(link["selector"]).first.click(timeout=6000)
            page.wait_for_load_state("load", timeout=15000)
            sess.settle(page)
            s = sess.snapshot(page)
            ok, why = in_language(s, target, base)
            out["navigation"] = {"tested": True, "url": page.url, "link_text": link.get("text"), "retained": ok, "evidence": why}
        except Exception as exc:
            out["navigation"] = {"tested": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    try:
        sess.limiter.wait(host_of(page.url))
        page.reload(wait_until="load")
        sess.settle(page)
        s = sess.snapshot(page)
        ok, why = in_language(s, target, base)
        out["reload"] = {"tested": True, "url": page.url, "retained": ok, "evidence": why}
    except Exception as exc:
        out["reload"] = {"tested": False, "reason": f"{type(exc).__name__}"}
    return out
