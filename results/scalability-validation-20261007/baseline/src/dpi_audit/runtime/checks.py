"""Bounded interaction checks. Each returns raw, JSON-serialisable facts; decisions are
made in rules.py. No check submits data to a server: mutating requests are aborted by the
session router, and form tests only trigger client-side validation."""
from __future__ import annotations

import io
import re
import time
from pathlib import Path

from ..util import truncate
from .net import host_of

CLOSE_RX = re.compile(r"close|cancel|dismiss|×|✕|✖|बंद|रद्द|cerrar|fermer", re.I)


def pixel_change(a: bytes, b: bytes, threshold: int = 24) -> int:
    from PIL import Image, ImageChops
    ia = Image.open(io.BytesIO(a)).convert("RGB")
    ib = Image.open(io.BytesIO(b)).convert("RGB")
    if ia.size != ib.size:
        return -1
    diff = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > threshold else 0)
    return diff.histogram()[255]


def _clip(rect: dict, vw: int, vh: int, pad: int = 4) -> dict | None:
    x = max(0.0, rect["x"] - pad)
    y = max(0.0, rect["y"] - pad)
    x2 = min(float(vw), rect["x"] + rect["w"] + pad)
    y2 = min(float(vh), rect["y"] + rect["h"] + pad)
    if x2 - x < 2 or y2 - y < 2:
        return None
    return {"x": x, "y": y, "width": x2 - x, "height": y2 - y}


# ------------------------------------------------------------------ keyboard walk
def keyboard_walk(sess, page, kcfg: dict, thr: dict, shot_dir: Path) -> dict:
    sess.ensure_axe(page)
    sess.ensure_lib(page)
    page.evaluate("__dpi.axeSetup()")
    page.evaluate("__dpi.resetFocus()")
    vp = page.viewport_size or {"width": 1280, "height": 800}
    max_stops = int(kcfg.get("max_tab_stops", 60))
    shot_stops = int(kcfg.get("focus_screenshot_stops", 30))
    min_px = int(thr.get("focus_pixel_change_min", 8))
    seq: list[dict] = []
    seen: dict[str, int] = {}
    vis: list[dict] = []
    obscured: list[dict] = []
    completed, end_reason, cycle, stuck = False, "tab budget exhausted", None, 0
    body_hits = 0
    for _ in range(max_stops):
        page.keyboard.press("Tab")
        page.wait_for_timeout(50)
        try:
            info = page.evaluate("__dpi.focusInfo()")
        except Exception as exc:  # page navigated or crashed
            end_reason = f"focus query failed: {type(exc).__name__}"
            break
        if info.get("body") or not info.get("id"):
            body_hits += 1
            if seq:
                completed, end_reason = True, "focus returned to document after last stop"
                break
            if body_hits >= 3:
                completed, end_reason = True, "no focusable element reached"
                break
            continue
        fid = info["id"]
        if seq and fid == seq[-1]["id"]:
            stuck += 1
            if stuck >= 3:
                cycle = {"kind": "focus_did_not_move", "ids": [fid], "selectors": [info["selector"]]}
                end_reason = "focus did not move on Tab"
                break
            continue
        stuck = 0
        if fid in seen:
            if seen[fid] == 0:
                completed, end_reason = True, "focus wrapped to first stop"
            else:
                part = seq[seen[fid]:]
                cycle = {"kind": "subset_cycle", "ids": [s["id"] for s in part], "selectors": [s["selector"] for s in part][:12],
                         "in_dialog": any(s.get("in_dialog") for s in part)}
                end_reason = "focus cycled within a subset of stops"
            break
        seen[fid] = len(seq)
        seq.append(info)
        if info.get("covered"):
            obscured.append({k: info.get(k) for k in ("selector", "covered_by", "name", "rect")})
        if len(seq) <= shot_stops:
            clip = _clip(info["rect"], vp["width"], vp["height"])
            if clip:
                try:
                    a = page.screenshot(clip=clip, type="png", timeout=5000)
                    page.evaluate("__dpi.blurActive()")
                    page.wait_for_timeout(30)
                    b = page.screenshot(clip=clip, type="png", timeout=5000)
                    changed = pixel_change(a, b)
                    rec = {"selector": info["selector"], "name": info["name"], "changed_px": changed,
                           "visible_change": changed >= min_px, "outline": info.get("outline"), "box_shadow": info.get("box_shadow")}
                    if changed >= 0 and changed < min_px and len([v for v in vis if not v["visible_change"]]) < 5:
                        p = shot_dir / f"focus_{len(seq):02d}.png"
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(a)
                        rec["screenshot"] = str(p)
                    vis.append(rec)
                    if not page.evaluate("(id) => __dpi.refocus(id)", fid):
                        end_reason = "could not restore focus after visibility check"
                        break
                except Exception as exc:
                    vis.append({"selector": info["selector"], "error": f"{type(exc).__name__}"})
    trap = None
    if cycle:
        ids = set(cycle["ids"])
        escaped, how = False, ""
        for key, n in (("Tab", len(ids) + 2), ("Shift+Tab", len(ids) + 2), ("Escape", 1)):
            for _ in range(n):
                page.keyboard.press(key)
                page.wait_for_timeout(50)
                cur = page.evaluate("__dpi.focusInfo()")
                if not cur.get("id") or cur["id"] not in ids:
                    escaped, how = True, key
                    break
            if escaped:
                break
            if key == "Escape":
                page.keyboard.press("Tab")
                page.wait_for_timeout(50)
                cur = page.evaluate("__dpi.focusInfo()")
                if not cur.get("id") or cur["id"] not in ids:
                    escaped, how = True, "Escape then Tab"
        trap = {"cycle": cycle, "escaped": escaped, "escape_key": how}
    try:
        reach = page.evaluate("(ids) => __dpi.reachability(ids)", [s["id"] for s in seq])
    except Exception:
        reach = []
    page.evaluate("__dpi.axeTeardown()")
    return {"sequence": [{k: s.get(k) for k in ("selector", "tag", "role", "name", "href", "in_dialog", "tabindex", "covered")} for s in seq],
            "stops": len(seq), "completed": completed, "end_reason": end_reason, "trap": trap,
            "focus_visibility": vis, "obscured": obscured, "reachability": reach, "budget": max_stops,
            "first_stops": seq[:3]}


def skip_link_test(sess, page, first_stops: list[dict], skip_words: list[str]) -> dict | None:
    rx = re.compile("|".join(re.escape(w) for w in skip_words), re.I)
    cand = None
    for s in first_stops:
        href = s.get("href") or ""
        if href.startswith("#") and len(href) > 1 and (rx.search(s.get("name") or "") or re.search(r"main|content", href, re.I)):
            cand = s
            break
    if not cand:
        return None
    tid = cand["href"][1:]
    res = {"selector": cand["selector"], "name": cand.get("name"), "href": cand["href"]}
    res["target_exists"] = page.evaluate("(t) => !!(document.getElementById(t) || document.getElementsByName(t)[0])", tid)
    page.evaluate("__dpi.resetFocus()")
    ok = page.evaluate("(id) => __dpi.refocus(id)", cand["id"])
    if not ok:
        res["activation"] = "could_not_focus"
        return res
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    page.keyboard.press("Tab")
    page.wait_for_timeout(80)
    res["after_tab_inside_target"] = page.evaluate("""(t) => { const tg = document.getElementById(t) || document.getElementsByName(t)[0];
        const a = document.activeElement; return !!(tg && a && (tg === a || tg.contains(a) || (tg.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING))); }""", tid)
    res["target_in_view"] = page.evaluate("""(t) => { const tg = document.getElementById(t) || document.getElementsByName(t)[0];
        if (!tg) return false; const r = tg.getBoundingClientRect(); return r.top < innerHeight && r.bottom > 0; }""", tid)
    return res


def keyboard_activation(sess, page, max_controls: int = 4) -> list[dict]:
    sess.ensure_lib(page)
    cands = page.evaluate("(n) => __dpi.activationCandidates(n)", max_controls)
    out = []
    url0 = page.url
    page.evaluate("__dpi.watchMutations()")
    for c in cands:
        rec = {"selector": c["selector"], "name": c["name"], "tag": c["tag"], "role": c["role"], "changed": False, "key": None, "change": []}
        for key in ("Enter", " "):
            if not page.evaluate("(id) => __dpi.refocus(id)", c["id"]):
                rec["error"] = "not_focusable"
                break
            s0 = page.evaluate("(id) => __dpi.stateOf(id)", c["id"])
            page.keyboard.press(key if key != " " else "Space")
            page.wait_for_timeout(350)
            if page.url != url0:
                rec.update(changed=True, key=key.strip() or "Space", change=["navigated"])
                try:
                    page.go_back(wait_until="load")
                except Exception:
                    pass
                break
            s1 = page.evaluate("(id) => __dpi.stateOf(id)", c["id"])
            if s0 is None or s1 is None:
                rec.update(changed=True, key=key.strip() or "Space", change=["element replaced"])
                break
            diff = [k for k in ("expanded", "selected", "pressed", "open", "controlled_visible") if s0.get(k) != s1.get(k)]
            if s1.get("mutations", 0) > s0.get("mutations", 0):
                diff.append("dom_mutation")
            if diff:
                rec.update(changed=True, key=key.strip() or "Space", change=diff)
                # restore: toggle back with the same key when an expanded/open state changed
                if any(k in diff for k in ("expanded", "open")):
                    page.evaluate("(id) => __dpi.refocus(id)", c["id"])
                    page.keyboard.press(key if key != " " else "Space")
                    page.wait_for_timeout(200)
                break
        out.append(rec)
    return out


VISIBLE_DIALOGS_JS = """() => Array.from(document.querySelectorAll('dialog,[role=dialog],[role=alertdialog],[aria-modal=true]'))
   .filter(d => window.__dpi.visible(d)).map(d => window.__dpi.id(d))"""


def modal_test(sess, page, trigger: dict) -> dict:
    sess.ensure_lib(page)
    rec = {"trigger": trigger["selector"], "trigger_name": trigger.get("name"), "opened": False}
    before = set(page.evaluate(VISIBLE_DIALOGS_JS))
    page.evaluate("__dpi.resetFocus()")
    if not page.evaluate("(id) => __dpi.refocus(id)", trigger["id"]):
        rec["error"] = "trigger_not_focusable"
        return rec
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)
    now = [d for d in page.evaluate(VISIBLE_DIALOGS_JS) if d not in before]
    if not now:
        return rec
    did = now[0]
    rec["opened"] = True
    info = page.evaluate("""(id) => { const d = window.__dpi.el(id); const a = document.activeElement;
        let modal = d.getAttribute('aria-modal') === 'true'; try { modal = modal || d.matches(':modal'); } catch(e) {}
        const f = d.querySelectorAll('a[href],button,input:not([type=hidden]),select,textarea,[tabindex]:not([tabindex="-1"])').length;
        return {selector: window.__dpi.cssPath(d), modal, focus_inside: !!(a && (d === a || d.contains(a))), focusables: f,
                close: Array.from(d.querySelectorAll('button,a,[role=button]')).filter(b => /close|cancel|dismiss|×|✕|✖|बंद|रद्द|cerrar|fermer/i.test(window.__dpi.name(b) + ' ' + (b.className||''))).map(b => window.__dpi.id(b))[0] || null}; }""", did)
    rec.update(dialog=info["selector"], modal=info["modal"], focus_moved_in=info["focus_inside"], focusables=info["focusables"])
    contained = None
    if info["modal"]:
        contained = True
        for _ in range(min(15, info["focusables"] + 2)):
            page.keyboard.press("Tab")
            page.wait_for_timeout(40)
            inside = page.evaluate("(id) => { const d = window.__dpi.el(id); const a = document.activeElement; return !!(d && a && (d === a || d.contains(a))); }", did)
            if not inside:
                contained = False
                break
    rec["focus_contained"] = contained
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    still = page.evaluate("(id) => { const d = window.__dpi.el(id); return !!(d && window.__dpi.visible(d)); }", did)
    how = "Escape" if not still else None
    if still and info.get("close"):
        if page.evaluate("(id) => __dpi.refocus(id)", info["close"]):
            page.keyboard.press("Enter")
            page.wait_for_timeout(400)
            still = page.evaluate("(id) => { const d = window.__dpi.el(id); return !!(d && window.__dpi.visible(d)); }", did)
            how = "close control" if not still else None
    rec["dismissed"] = not still
    rec["dismissed_by"] = how
    if not still:
        rec["focus_returned"] = page.evaluate("""(id) => { const t = window.__dpi.el(id); const a = document.activeElement;
            return !!(t && a && (t === a || t.contains(a) || a.contains(t))); }""", trigger["id"])
    else:
        rec["focus_returned"] = None
    rec["trap"] = bool(still and contained)
    return rec


# ------------------------------------------------------------------ forms
FAKE = {"email": "test@example.invalid", "tel": "0000000000", "number": "1", "url": "https://example.invalid",
        "date": "2000-01-01", "search": "test", "text": "Test User", "textarea": "Test message (automated accessibility check)"}

FIELDS_JS = """(sel) => { const f = document.querySelector(sel); if (!f) return null;
  return Array.from(f.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]):not([type=reset]),select,textarea'))
   .filter(x => window.__dpi.visible(x) && !x.disabled && !x.readOnly).slice(0, 40).map(x => ({id: window.__dpi.id(x), selector: window.__dpi.cssPath(x),
     tag: x.localName, type: (x.getAttribute('type') || x.localName).toLowerCase(), required: x.required || x.getAttribute('aria-required') === 'true',
     pattern: x.getAttribute('pattern'), options: x.localName === 'select' ? x.options.length : 0, name: window.__dpi.name(x).slice(0, 80)})); }"""

AFTER_JS = """(ids) => ids.map(id => { const x = window.__dpi.el(id); if (!x) return {id, gone: true};
  const ref = (attr) => (x.getAttribute(attr) || '').split(/\\s+/).filter(Boolean).map(i => document.getElementById(i)).filter(Boolean)
      .map(e => (e.innerText || e.textContent || '').trim()).join(' ').slice(0, 200);
  const lbl = x.labels ? Array.from(x.labels).map(l => (l.innerText || '').trim()).join(' ').slice(0, 200) : '';
  return {id, selector: window.__dpi.cssPath(x), value: (x.value || '').slice(0, 100), valid: x.validity ? x.validity.valid : null,
          validation_message: (x.validationMessage || '').slice(0, 200), aria_invalid: x.getAttribute('aria-invalid'),
          describedby_text: ref('aria-describedby'), errormessage_text: ref('aria-errormessage'), label_text: lbl}; })"""


def form_error_test(sess, page, form_sel: str) -> dict:
    sess.ensure_lib(page)
    rec = {"form": form_sel, "tested": False}
    fields = page.evaluate(FIELDS_JS, form_sel)
    if not fields:
        rec["reason"] = "form not found or no editable visible fields"
        return rec
    target = next((f for f in fields if f["required"] and f["type"] not in ("checkbox", "radio", "file")), None)
    mode = "empty_required"
    if target is None:
        target = next((f for f in fields if f["type"] == "email" or f["pattern"]), None)
        mode = "invalid_format"
    if target is None:
        rec["reason"] = "no client-side constraints (no required/pattern/email fields) to trigger safely"
        return rec
    filled = {}
    for f in fields:
        if f["id"] == target["id"] or f["type"] in ("checkbox", "radio", "file", "password", "hidden", "color", "range"):
            continue
        loc = page.locator(f["selector"]).first
        try:
            if f["tag"] == "select":
                if f["options"] > 1:
                    loc.select_option(index=1, timeout=3000)
                    filled[f["id"]] = page.evaluate("(id) => window.__dpi.el(id).value", f["id"])
            else:
                val = FAKE["textarea"] if f["tag"] == "textarea" else FAKE.get(f["type"], FAKE["text"])
                if f["pattern"] and f["type"] in ("text", "tel"):
                    continue  # unknown valid value; leave untouched
                loc.fill(val, timeout=3000)
                filled[f["id"]] = val
        except Exception:
            continue
    if mode == "invalid_format":
        try:
            page.locator(target["selector"]).first.fill("not-valid", timeout=3000)
        except Exception:
            pass
    rec["novalidate"] = page.evaluate("(sel) => { const f = document.querySelector(sel); return !!(f && f.noValidate); }", form_sel)
    page.evaluate("__dpi.watchMutations()")
    blocked_before = len(sess.blocked)
    url0 = page.url
    nav = {"happened": False}
    page.once("framenavigated", lambda fr: nav.update(happened=fr == page.main_frame))
    submitted = page.evaluate("""(sel) => { const f = document.querySelector(sel); if (!f) return 'none';
        const b = f.querySelector('button[type=submit],input[type=submit],button:not([type]),input[type=image]');
        if (b) { window.__dpiSubmit = window.__dpi.id(b); return 'button'; } return 'enter'; }""", form_sel)
    try:
        if submitted == "button":
            sid = page.evaluate("window.__dpiSubmit")
            page.evaluate("(id) => __dpi.refocus(id)", sid)
            page.keyboard.press("Enter")
        else:
            page.locator(target["selector"]).first.press("Enter", timeout=3000)
    except Exception as exc:
        rec["reason"] = f"could not trigger submit: {type(exc).__name__}"
        return rec
    page.wait_for_timeout(1300)
    rec["tested"] = True
    rec["mode"] = mode
    rec["target_field"] = target["selector"]
    rec["blocked_requests"] = len(sess.blocked) - blocked_before
    rec["navigated"] = nav["happened"] or page.url != url0
    if rec["navigated"] and page.url != url0:
        rec["reason"] = "submission navigated (GET form or redirect); error state not observable"
        return rec
    try:
        after = page.evaluate(AFTER_JS, [f["id"] for f in fields])
        msgs = page.evaluate("__dpi.addedMessages()")
        active = page.evaluate("__dpi.focusInfo()")
    except Exception as exc:
        rec["reason"] = f"post-submit inspection failed: {type(exc).__name__}"
        return rec
    rec["fields"] = after
    rec["messages"] = msgs
    rec["focus_after"] = {k: active.get(k) for k in ("selector", "name")}
    by_id = {a["id"]: a for a in after}
    rec["preserved"] = {fid: (by_id.get(fid, {}).get("value") == v) for fid, v in filled.items()}
    rec["filled"] = len(filled)
    return rec


def auth_paste_test(sess, page, fields: list[dict]) -> list[dict]:
    sess.ensure_lib(page)
    out = []
    for f in fields[:3]:
        r = page.evaluate("(s) => __dpi.pasteBlocked(s)", f["selector"])
        if r is not None:
            out.append({"selector": f["selector"], **r})
    return out


# ------------------------------------------------------------------ layout states
def measure_layout(sess, page) -> dict:
    sess.ensure_lib(page)
    vw = page.evaluate("innerWidth")
    return {"overflow": page.evaluate("([w, t]) => __dpi.overflowCulprits(w, t)", [vw, 2]), "clipped": page.evaluate("__dpi.clippedText(300)"),
            "viewport_width": vw}


TEXT_SPACING_CSS = ("* { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; } "
                    "p { margin-bottom: 2em !important; }")


def text_spacing_test(sess, page, baseline_clipped: list[str]) -> dict:
    sess.ensure_lib(page)
    page.evaluate("""(css) => { const s = document.createElement('style'); s.id = '__dpi_ts'; s.textContent = css; document.head.appendChild(s); }""", TEXT_SPACING_CSS)
    page.wait_for_timeout(300)
    clipped = page.evaluate("__dpi.clippedText(300)")
    page.evaluate("() => { const s = document.getElementById('__dpi_ts'); if (s) s.remove(); }")
    new = sorted(set(clipped) - set(baseline_clipped))
    return {"clipped_after": len(clipped), "clipped_baseline": len(baseline_clipped), "newly_clipped": new[:25], "newly_clipped_count": len(new)}


def text_size_control_test(sess, page, control: dict) -> dict:
    sess.ensure_lib(page)
    before = page.evaluate("__dpi.bodyFontSize()")
    try:
        page.locator(control["selector"]).first.click(timeout=4000)
    except Exception as exc:
        return {"control": control["selector"], "error": f"click failed: {type(exc).__name__}", "before": before}
    page.wait_for_timeout(600)
    try:
        sess.ensure_lib(page)
        after = page.evaluate("__dpi.bodyFontSize()")
    except Exception:
        after = None
    return {"control": control["selector"], "name": control.get("name"), "before": before, "after": after}


def animations_now(sess, page) -> list[dict]:
    sess.ensure_lib(page)
    return page.evaluate("""() => { try { return document.getAnimations().filter(a => a.playState === 'running').slice(0, 200)
        .map(a => { const t = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : {};
          return {infinite: t.iterations === Infinity, duration: t.duration}; }); } catch (e) { return []; } }""")


# ------------------------------------------------------------------ resources
def load_measure(sess, url: str, viewport: dict, profile: dict, timeout_s: float = 90) -> dict:
    ctx = sess.new_context(viewport)
    page = ctx.new_page()
    out = {"url": url, "profile": profile.get("name")}
    try:
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": float(profile["latency_ms"]),
                 "downloadThroughput": float(profile["download_kbps"]) * 1000 / 8, "uploadThroughput": float(profile["upload_kbps"]) * 1000 / 8})
        if float(profile.get("cpu_slowdown", 1)) > 1:
            cdp.send("Emulation.setCPUThrottlingRate", {"rate": float(profile["cpu_slowdown"])})
        acc = {"bytes": 0, "requests": 0, "failed": 0}
        cdp.on("Network.requestWillBeSent", lambda e: acc.__setitem__("requests", acc["requests"] + 1))
        cdp.on("Network.loadingFinished", lambda e: acc.__setitem__("bytes", acc["bytes"] + int(e.get("encodedDataLength") or 0)))
        cdp.on("Network.loadingFailed", lambda e: acc.__setitem__("failed", acc["failed"] + 1))
        page.add_init_script("window.__lcp = null; try { new PerformanceObserver(l => { for (const e of l.getEntries()) window.__lcp = e.startTime; }).observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}")
        sess.limiter.wait(host_of(url))
        t0 = time.time()
        page.goto(url, wait_until="load", timeout=timeout_s * 1000)
        out["wall_load_s"] = round(time.time() - t0, 2)
        page.wait_for_timeout(1500)
        nav = page.evaluate("""() => { const n = performance.getEntriesByType('navigation')[0]; if (!n) return null;
            return {dcl_ms: Math.round(n.domContentLoadedEventEnd), load_ms: Math.round(n.loadEventEnd), ttfb_ms: Math.round(n.responseStart)}; }""")
        out.update(nav or {})
        out["lcp_ms"] = page.evaluate("window.__lcp === null ? null : Math.round(window.__lcp)")
        out["transfer_kb"] = round(acc["bytes"] / 1024, 1)
        out["requests"] = acc["requests"]
        out["failed_requests"] = acc["failed"]
        out["ok"] = True
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {truncate(str(exc).splitlines()[0], 160)}"
    finally:
        ctx.close()
    return out


def offline_check(sess, url: str, viewport: dict, shot: Path) -> dict:
    ctx = sess.new_context(viewport, allow_service_workers=True)
    page = ctx.new_page()
    out = {"url": url}
    try:
        r = sess.goto(page, url)
        if not r["ok"]:
            out["error"] = r["error"]
            return out
        page.wait_for_timeout(2500)
        out["sw_registrations"] = page.evaluate("navigator.serviceWorker ? navigator.serviceWorker.getRegistrations().then(r => r.length) : -1")
        out["manifest"] = page.evaluate("(() => { const m = document.querySelector('link[rel=manifest]'); return m ? m.href : null; })()")
        if (out["sw_registrations"] or 0) > 0:
            ctx.set_offline(True)
            try:
                page.reload(wait_until="load", timeout=20000)
                out["offline_url"] = page.url
                out["offline_text_chars"] = page.evaluate("document.body ? document.body.innerText.length : 0")
                out["offline_rendered"] = (not page.url.startswith("chrome-error")) and out["offline_text_chars"] > 50
            except Exception as exc:
                out["offline_rendered"] = False
                out["offline_error"] = type(exc).__name__
            out["screenshot"] = sess.screenshot(page, shot)
            ctx.set_offline(False)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {truncate(str(exc), 160)}"
    finally:
        ctx.close()
    return out


def link_check(sess, ctx, urls: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    req = ctx.request
    for u in urls:
        ok, why = sess.robots.allowed(u)
        if not ok:
            out[u] = {"status": None, "class": "skipped_robots"}
            continue
        sess.limiter.wait(host_of(u))
        try:
            r = req.head(u, timeout=15000, max_redirects=5, fail_on_status_code=False)
            st = r.status
            if st in (403, 405, 501) or st >= 500:
                sess.limiter.wait(host_of(u))
                r = req.get(u, timeout=20000, max_redirects=5, fail_on_status_code=False)
                st = r.status
            cls = "ok" if st < 400 else "restricted" if st in (401, 403) else "broken" if st in (404, 410) or st >= 500 else "client_error"
            out[u] = {"status": st, "class": cls, "final_url": r.url}
        except Exception as exc:
            out[u] = {"status": None, "class": "error", "error": type(exc).__name__}
    return out


def run_journey(sess, ctx, journey: dict, default_start: str, shot_dir: Path) -> dict:
    page = ctx.new_page()
    log = []
    res = {"journey_id": journey.get("id"), "description": journey.get("description"), "steps_total": len(journey.get("steps", []))}
    try:
        start = journey.get("start_url") or default_start
        r = sess.goto(page, start)
        res["start_ok"] = r["ok"]
        if not r["ok"]:
            res["error"] = r["error"]
            res["steps_completed"] = 0
            return res
        done = 0
        for i, st in enumerate(journey.get("steps", [])):
            act = st.get("action")
            ok, note = False, ""
            try:
                if act == "goto":
                    rr = sess.goto(page, st["url"])
                    ok, note = rr["ok"], rr.get("error", "")
                elif act == "click":
                    loc = page.get_by_text(st["target_text"], exact=False).first if st.get("target_text") else page.locator(st["selector"]).first
                    sess.limiter.wait(host_of(page.url))
                    loc.click(timeout=8000)
                    sess.settle(page)
                    ok = True
                elif act == "expect_text":
                    ok = page.get_by_text(st["text"], exact=False).count() > 0
                elif act == "expect_url":
                    ok = st["contains"] in page.url
                elif act == "press":
                    page.keyboard.press(st["key"])
                    page.wait_for_timeout(300)
                    ok = True
                elif act == "fill_test_value":
                    page.locator(st["selector"]).first.fill(str(st.get("value", "test")), timeout=5000)
                    ok = True
            except Exception as exc:
                note = f"{type(exc).__name__}: {truncate(str(exc).splitlines()[0], 120)}"
            log.append({"step": i + 1, "action": act, "ok": ok, "note": note, "url": page.url})
            if not ok:
                break
            done += 1
        res["steps_completed"] = done
        res["screenshot"] = sess.screenshot(page, shot_dir / f"journey_{journey.get('id')}.jpg")
    finally:
        res["log"] = log
        page.close()
    return res
