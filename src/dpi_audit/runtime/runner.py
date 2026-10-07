"""Run all runtime checks for one web interface within its budget."""
from __future__ import annotations

import re
import time
import traceback
import urllib.parse
from pathlib import Path

from .. import lang as L
from ..observations import Observation, dom_evidence
from ..util import short_hash, slugify, truncate
from . import checks as C
from . import lang_switch as LS
from . import rules as R
from .browser import save_axe
from .discovery import Sampler, path_depth
from .net import fetch_sitemap, host_of


class InterfaceRunner:
    def __init__(self, sess, project_id: str, iface, meta: dict, cfg: dict, codebook, evidence_root: Path, log):
        self.sess, self.pid, self.iface, self.meta, self.cfg, self.cb = sess, project_id, iface, meta, cfg, codebook
        self.rt = cfg["runtime"]
        self.log = log
        self.url = iface.location.url
        self.itype = iface.interface_type
        self.slug = slugify(iface.interface_id.split("::")[-1], 40)
        self.ev_root = evidence_root / "runtime" / self.slug
        self.obs: list[Observation] = []
        self.pages: list[dict] = []
        self.states_run: dict[str, int] = {}
        self.states_skipped: dict[str, str] = {}
        self.errors: list[str] = []
        self.t0 = time.time()
        self.budget = float(self.rt.get("interface_budget_s", 1200))
        cfg.setdefault("_rules", {})
        for f in codebook.features:
            for d in (f.get("detectors", {}) or {}).get("runtime", []) or []:
                if d.get("id") == "rt.axe":
                    cfg["_rules"][f["id"]] = list(d.get("rules", []))

    # -------------------------------------------------------------- helpers
    def pc(self, url: str, viewport: str, state: str, is_home=False, **extra) -> R.PCtx:
        return R.PCtx(self.pid, self.iface.interface_id, self.itype, url, viewport, state, self.cfg, self.meta, is_home, extra)

    def over_budget(self) -> bool:
        return time.time() - self.t0 > self.budget

    def state_enabled(self, name: str, page_index: int | None = None) -> bool:
        s = self.rt["states"].get(name, {})
        if not s.get("enabled", True):
            self.states_skipped[name] = "disabled in config"
            return False
        if page_index is not None and page_index >= int(s.get("max_pages", 1 << 30)):
            return False
        if self.over_budget():
            self.states_skipped[name] = f"interface budget {self.budget:.0f}s exhausted"
            return False
        return True

    def ran(self, name: str) -> None:
        self.states_run[name] = self.states_run.get(name, 0) + 1

    def page_dir(self, url: str) -> Path:
        return self.ev_root / short_hash(url, 10)

    def guard(self, label: str, fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as exc:
            msg = f"{label}: {type(exc).__name__}: {truncate(str(exc).splitlines()[0] if str(exc) else '', 200)}"
            self.errors.append(msg)
            self.log(f"[{self.pid}] {msg}")
            return None

    # -------------------------------------------------------------- main
    def run(self) -> dict:
        vps = self.rt["viewports"]
        ctx_d = self.sess.new_context(vps["desktop"])
        ctx_m = self.sess.new_context(vps["mobile"])
        result = {"interface_id": self.iface.interface_id, "url": self.url, "status": "ok"}
        try:
            page = ctx_d.new_page()
            page.on("crash", lambda *_: setattr(page, "_dpi_crashed", True))
            home = self.sess.goto(page, self.url)
            result["home"] = {k: home.get(k) for k in ("status", "final_url", "error", "robots", "title", "load_s")}
            if not home["ok"]:
                result["status"] = "blocked" if home["error"] == "blocked_robots" or (home.get("status") in (401, 403, 429)) else "failed"
                result["reason"] = home["error"]
                self.pages.append({"url": self.url, "status": result["status"], "reason": home["error"], "http_status": home.get("status"),
                                   "page_type": "home", "source": "input"})
                return self.finish(result)
            snap = self.sess.snapshot(page)
            text_len = sum(len(s["t"]) for s in snap.get("text", []))
            n_inter = len(snap.get("links", [])) + len(snap.get("buttons", [])) + sum(len(f.get("fields", [])) for f in snap.get("forms", []))
            if text_len < 20 and n_inter == 0:
                result["status"] = "render_blocked"
                result["reason"] = "page loaded but rendered no visible text or controls (script failure, bot challenge, or JS-only shell)"
                shot = self.sess.screenshot(page, self.page_dir(page.url) / "desktop_initial.jpg")
                self.pages.append({"url": self.url, "final_url": page.url, "status": "failed", "reason": "render_blocked", "http_status": home.get("status"),
                                   "page_type": "home", "source": "input", "screenshot": shot})
                return self.finish(result)
            sampler = Sampler(self.url, self.cfg, self.sess.policy, self.sess.robots)
            sampler.plan_from_home(page.url, snap, self.meta.get("critical_journeys"))
            if len(sampler.sampled) < sampler.max_pages:
                info = self.sess.robots.info(page.url)
                maps = info.get("sitemaps") or []
                if maps:
                    urls = fetch_sitemap(maps[0], self.sess.ua, int(self.rt["sitemap_max_urls"]))
                    sampler.add_more([(u, "") for u in urls], "sitemap")
            self.sampler = sampler
            first_snaps = {}
            i = 0
            while i < len(sampler.sampled):
                if page.is_closed() or getattr(page, "_dpi_crashed", False):
                    self.errors.append("browser page crashed or closed; continuing with a fresh page")
                    page = ctx_d.new_page()
                    page.on("crash", lambda *_: setattr(page, "_dpi_crashed", True))
                if self.over_budget():
                    for s in sampler.sampled[i:]:
                        self.pages.append({"url": s["url"], "status": "skipped", "reason": "interface budget exhausted", "page_type": s["page_type"], "source": s["source"]})
                    break
                item = sampler.sampled[i]
                snap_i = self.guard(f"page {item['url']}", self.audit_page, ctx_d, ctx_m, page, item, i, snap if i == 0 else None)
                if snap_i:
                    first_snaps[item["url"]] = snap_i
                    if i < 3 and len(sampler.sampled) < sampler.max_pages:
                        sampler.add_more([(l["abs"], l.get("text") or "") for l in snap_i.get("links", []) if l.get("internal") and l.get("abs")], "second_level")
                i += 1
            sampler.finalize()
            for rec in sampler.records.values():
                if rec["status"] in ("skipped", "blocked") and not any(p["url"] == rec["url"] for p in self.pages):
                    self.pages.append({"url": rec["url"], "status": rec["status"], "reason": rec["reason"], "page_type": ",".join(rec["types"]),
                                       "source": rec["source"]})
            self.cross_page(first_snaps)
            home_url = sampler.sampled[0]["url"] if sampler.sampled else self.url
            self.homepage_tests(ctx_d, home_url, first_snaps.get(home_url) or snap)
            self.forms_tests(ctx_d, first_snaps)
            self.link_checks(ctx_d, first_snaps)
            self.journeys(ctx_d, home_url)
            self.interface_level(first_snaps)
            self.fill_absent(first_snaps)
        except Exception as exc:
            result["status"] = "failed"
            result["reason"] = f"{type(exc).__name__}: {truncate(str(exc), 300)}"
            self.errors.append(traceback.format_exc(limit=3))
        finally:
            for c in (ctx_d, ctx_m):
                try:
                    c.close()
                except Exception:
                    pass
        return self.finish(result)

    def finish(self, result: dict) -> dict:
        rendered = [p for p in self.pages if p.get("status") == "sampled" and p.get("rendered")]
        result.update({
            "pages": self.pages, "states_run": self.states_run, "states_skipped": self.states_skipped,
            "errors": self.errors[:30], "elapsed_s": round(time.time() - self.t0, 1),
            "coverage": {"pages_rendered": len(rendered), "pages_sampled": sum(1 for p in self.pages if p.get("status") == "sampled"),
                         "pages_failed": sum(1 for p in self.pages if p.get("status") == "failed"),
                         "pages_blocked": sum(1 for p in self.pages if p.get("status") == "blocked"),
                         "pages_skipped": sum(1 for p in self.pages if p.get("status") == "skipped"),
                         "discovered": len(self.pages), "max_pages": int(self.rt["max_pages"]),
                         "blocked_requests": len(self.sess.blocked), "popups_closed": self.sess.popups, "dialogs_dismissed": self.sess.dialogs},
        })
        return result

    # -------------------------------------------------------------- per page
    def audit_page(self, ctx_d, ctx_m, page, item, idx: int, home_snap: dict | None) -> dict | None:
        url = item["url"]
        pdir = self.page_dir(url)
        rec = {"url": url, "status": "sampled", "page_type": item["page_type"], "source": item["source"], "index": idx}
        self.pages.append(rec)
        if home_snap is None:
            r = self.sess.goto(page, url)
            rec.update(http_status=r.get("status"), final_url=r.get("final_url"), title=r.get("title"))
            if not r["ok"]:
                rec.update(status="blocked" if r["error"] == "blocked_robots" else "failed", reason=r["error"], rendered=False)
                return None
            snap = self.sess.snapshot(page)
        else:
            snap = home_snap
            rec.update(http_status=None, final_url=page.url, title=snap.get("title"))
        rec["rendered"] = True
        is_home = idx == 0
        # desktop initial
        axe_d = self.sess.run_axe(page)
        if self.cfg["evidence"].get("save_raw_axe", True):
            rec["axe_desktop"] = save_axe(pdir / "axe_desktop_initial.json.gz", axe_d)
        rec["screenshot_desktop"] = self.sess.screenshot(page, pdir / "desktop_initial.jpg")
        rec["axe_counts_desktop"] = {k: len(axe_d.get(k, [])) for k in ("violations", "passes", "incomplete", "inapplicable")}
        pc = self.pc(url, "desktop", "initial", is_home)
        self.obs += R.initial_state(pc, snap, axe_d, desktop=True)
        rec["snapshot_summary"] = {"links": len(snap.get("links", [])), "forms": len(snap.get("forms", [])), "html_lang": snap.get("html_lang"),
                                   "text_segments": len(snap.get("text", [])), "dialogs": len(snap.get("dialogs", []))}
        self.ran("initial_desktop")
        snap["_axe"] = axe_d
        # keyboard (desktop)
        if self.state_enabled("keyboard", idx):
            self.guard("keyboard", self._keyboard, page, url, pdir, axe_d)
        # modals
        if snap.get("dialog_triggers") and self.state_enabled("modal", idx):
            self.guard("modal", self._modals, page, url, snap)
        # text spacing (desktop, same page)
        if self.state_enabled("text_spacing", idx):
            def ts():
                self.sess.goto(page, url)
                r = C.text_spacing_test(self.sess, page, snap.get("clipped", []))
                self.obs.append(R.spacing_state(self.pc(url, "desktop", "text_spacing"), r, axe_d))
                self.ran("text_spacing")
            self.guard("text_spacing", ts)
        # password fields: paste test
        pw = [f for f in R._fields(snap) if f.get("type") == "password"]
        if pw:
            def paste():
                if page.url != url:
                    self.sess.goto(page, url)
                users = [f for f in R._fields(snap) if f.get("type") in ("text", "email") and f.get("_form") == pw[0].get("_form")][:1]
                res = C.auth_paste_test(self.sess, page, pw + users)
                self.obs.append(R.paste_state(self.pc(url, "desktop", "auth_paste"), res, bool(snap.get("captcha"))))
                self.ran("auth_paste")
            self.guard("auth_paste", paste)
        # mobile initial
        def mobile():
            pm = ctx_m.new_page()
            try:
                r = self.sess.goto(pm, url)
                if not r["ok"]:
                    return
                sm = self.sess.snapshot(pm)
                axe_m = self.sess.run_axe(pm)
                if self.cfg["evidence"].get("save_raw_axe", True):
                    rec["axe_mobile"] = save_axe(pdir / "axe_mobile_initial.json.gz", axe_m)
                rec["screenshot_mobile"] = self.sess.screenshot(pm, pdir / "mobile_initial.jpg")
                self.obs += R.initial_state(self.pc(url, "mobile", "initial", is_home), sm, axe_m, desktop=False)
                self.ran("initial_mobile")
            finally:
                pm.close()
        self.guard("mobile", mobile)
        # reflow / zoom / reduced motion in dedicated contexts
        if self.state_enabled("reflow", idx):
            self.guard("reflow", self._layout_state, "reflow", url, pdir, snap, axe_d)
        if self.state_enabled("zoom", idx):
            self.guard("zoom", self._layout_state, "zoom", url, pdir, snap, axe_d)
        if self.state_enabled("reduced_motion", idx):
            n0 = len(snap.get("animations", []))
            if n0 == 0:
                self.obs.append(R.motion_state(self.pc(url, "desktop", "reduced_motion"), 0, []))
            else:
                self.guard("reduced_motion", self._reduced_motion, url, n0)
        if self.state_enabled("load_measure", idx):
            def lm():
                r = C.load_measure(self.sess, url, self.rt["viewports"]["desktop"], self.rt["load_profile"])
                self.obs.append(self._res01(url, r))
                rec["load"] = r
                self.ran("load_measure")
            self.guard("load_measure", lm)
        return snap

    def _keyboard(self, page, url, pdir, axe_d):
        if page.url != url:
            self.sess.goto(page, url)
        kcfg = self.rt["states"]["keyboard"]
        walk = C.keyboard_walk(self.sess, page, kcfg, self.cfg["thresholds"], pdir / "focus")
        skip = C.skip_link_test(self.sess, page, walk.get("first_stops", []), self.cfg["keywords"]["skip"]) if walk.get("first_stops") else None
        act = C.keyboard_activation(self.sess, page)
        for s in walk.get("first_stops", []):
            s.pop("excerpt", None)
        self.obs += R.keyboard_state(self.pc(url, "desktop", "keyboard"), walk, skip, act, axe_d)
        self.keyboard_seq = getattr(self, "keyboard_seq", {})
        self.keyboard_seq[url] = [s.get("selector") for s in walk.get("sequence", [])]
        self.ran("keyboard")

    def _modals(self, page, url, snap):
        n = int(self.rt["states"]["modal"].get("max_dialogs_per_page", 2))
        for k, trig in enumerate(snap["dialog_triggers"][:n]):
            self.sess.goto(page, url)
            fresh = self.sess.snapshot(page)
            t = next((x for x in fresh.get("dialog_triggers", []) if x["selector"] == trig["selector"]), None)
            if not t:
                continue
            rec = C.modal_test(self.sess, page, t)
            self.obs += R.modal_state(self.pc(url, "desktop", f"modal:{k + 1}", trigger=t["selector"]), rec)
            self.ran("modal")

    def _layout_state(self, kind, url, pdir, snap, axe_d):
        if kind == "reflow":
            vp = dict(self.rt["reflow_viewport"], device_scale_factor=1)
        else:
            z = self.rt["zoom_emulation"]
            f = float(z["factor"])
            vp = {"width": int(z["base_width"] / f), "height": int(z["base_height"] / f), "device_scale_factor": f}
        ctx = self.sess.new_context(vp)
        try:
            p = ctx.new_page()
            r = self.sess.goto(p, url)
            if not r["ok"]:
                return
            m = C.measure_layout(self.sess, p)
            self.sess.screenshot(p, pdir / f"{kind}.jpg")
            if kind == "reflow":
                self.obs.append(R.reflow_state(self.pc(url, f"w{vp['width']}", "reflow"), m, int(self.cfg["thresholds"]["reflow_tolerance_px"])))
            else:
                self.obs.append(R.zoom_state(self.pc(url, f"zoom{int(vp['device_scale_factor'] * 100)}", "zoom"), m, snap.get("clipped", []), axe_d))
            self.ran(kind)
        finally:
            ctx.close()

    def _reduced_motion(self, url, n0):
        ctx = self.sess.new_context(self.rt["viewports"]["desktop"], reduced_motion="reduce")
        try:
            p = ctx.new_page()
            r = self.sess.goto(p, url)
            if r["ok"]:
                after = C.animations_now(self.sess, p)
                self.obs.append(R.motion_state(self.pc(url, "desktop", "reduced_motion"), n0, after))
                self.ran("reduced_motion")
        finally:
            ctx.close()

    def _res01(self, url, r) -> Observation:
        pc = self.pc(url, "desktop", "load_measure")
        if not r.get("ok"):
            return R.mk(pc, "RES-01", "rt.load_measure", "unknown", "not_tested", f"load measurement failed: {r.get('error')}", meas=r)
        th = self.cfg["thresholds"]
        limits = {"transfer_kb": th.get("page_weight_kb_max"), "requests": th.get("page_requests_max"), "load_ms": th.get("load_time_ms_max")}
        active = {k: v for k, v in limits.items() if v is not None}
        if not active:
            return R.mk(pc, "RES-01", "rt.load_measure", "present", "not_tested", f"{r['transfer_kb']} KB, {r['requests']} requests, load {r.get('load_ms')} ms "
                        f"under profile {r['profile']} (no study thresholds configured)", meas=r)
        exceeded = [k for k, v in active.items() if (r.get(k) or 0) > v]
        return R.mk(pc, "RES-01", "rt.load_measure", "present", "fail" if exceeded else "pass",
                    f"thresholds {active}: exceeded {exceeded}" if exceeded else f"within study thresholds {active}", meas=r)

    # -------------------------------------------------------------- cross page
    def cross_page(self, snaps: dict[str, dict]):
        urls = list(snaps)
        titles = {u: (snaps[u].get("title") or "").strip() for u in urls}
        for u in urls:
            pc = self.pc(u, "desktop", "initial")
            t = titles[u]
            dup = [v for v in urls if v != u and titles[v] == t and t]
            if not t:
                self.obs.append(R.mk(pc, "NAV-02", "rt.titles", "not_found_in_scope", "fail", "missing or empty title"))
            elif dup:
                self.obs.append(R.mk(pc, "NAV-02", "rt.titles", "present", "partial", f"title {t!r} identical on {len(dup)} other sampled page(s)",
                                     meas={"title": t, "duplicates": dup[:5]}))
            else:
                self.obs.append(R.mk(pc, "NAV-02", "rt.titles", "present", "pass", f"title {truncate(t, 80)!r} is distinct among sampled pages", meas={"title": t}))
        # NAV-05: navigation order
        def primary_nav(s):
            navs = [n for n in s.get("landmarks", {}).get("nav", []) if n.get("links")]
            if not navs:
                return []
            n = max(navs, key=lambda n: (n.get("region") in ("header", "other"), len(n["links"])))
            return [(l["text"].strip().lower(), urllib.parse.urlsplit(l["href"]).path) for l in n["links"] if l.get("text")]
        seqs = {u: primary_nav(snaps[u]) for u in urls}
        with_nav = [u for u in urls if len(seqs[u]) >= 2]
        if len(with_nav) >= 2:
            ref = with_nav[0]
            for u in with_nav[1:]:
                common = [x for x in seqs[ref] if x in seqs[u]]
                order_u = [x for x in seqs[u] if x in common]
                pc = self.pc(u, "desktop", "consistency", compared_with=ref)
                if len(common) < 2:
                    self.obs.append(R.mk(pc, "NAV-05", "rt.consistency", "not_applicable", "not_tested", "fewer than two navigation links in common with the reference page",
                                         appl="navigation not repeated"))
                elif order_u == common:
                    self.obs.append(R.mk(pc, "NAV-05", "rt.consistency", "present", "pass", f"{len(common)} common navigation links in the same relative order as {ref}",
                                         meas={"common": len(common)}))
                else:
                    self.obs.append(R.mk(pc, "NAV-05", "rt.consistency", "present", "fail", f"relative order of {len(common)} common navigation links differs from {ref}",
                                         meas={"reference": common[:15], "this_page": order_u[:15]}))
        else:
            self.obs.append(R.mk(self.pc(self.url, "desktop", "consistency"), "NAV-05", "rt.consistency", "not_applicable", "not_tested",
                                 f"{len(with_nav)} sampled page(s) with a navigation landmark (need >= 2)", appl="fewer than 2 pages with repeated navigation", kind="interface"))
        # NAV-11: consistent help placement
        rx = R.kw_rx(self.cfg["keywords"]["help"] + self.cfg["keywords"]["contact"])
        def help_pos(s):
            for region in ("header", "nav", "footer", "aside", "main", "other"):
                links = [l for l in s.get("links", []) if l.get("region") == region and l.get("visible")]
                for i, l in enumerate(links):
                    if rx.search(l.get("name") or l.get("text") or ""):
                        return (region, i, (l.get("name") or "").strip().lower())
            return None
        pos = {u: help_pos(snaps[u]) for u in urls}
        have = [u for u in urls if pos[u]]
        if len(have) >= 2:
            ref = have[0]
            for u in have[1:]:
                pc = self.pc(u, "desktop", "consistency", compared_with=ref)
                same = pos[u][0] == pos[ref][0] and pos[u][1] == pos[ref][1]
                self.obs.append(R.mk(pc, "NAV-11", "rt.consistency", "present", "pass" if same else "fail",
                                     f"help mechanism at {pos[u][0]}#{pos[u][1]} vs reference {pos[ref][0]}#{pos[ref][1]}",
                                     meas={"this": pos[u], "reference": pos[ref]}))
        else:
            self.obs.append(R.mk(self.pc(self.url, "desktop", "consistency"), "NAV-11", "rt.consistency", "not_applicable", "not_tested",
                                 f"help/contact mechanism found on {len(have)} sampled page(s) (need >= 2)", appl="help not repeated", kind="interface"))

    # -------------------------------------------------------------- homepage tests
    def homepage_tests(self, ctx_d, home_url, home_snap):
        if self.state_enabled("language_switch"):
            def lsw():
                r = LS.run(self.sess, ctx_d, home_url, self.meta, self.cfg, self.page_dir(home_url),
                           int(self.rt["states"]["language_switch"].get("max_options", 2)))
                self.lang_result = r
                self.obs += self._lang_obs(home_url, r)
                self.ran("language_switch")
            self.guard("language_switch", lsw)
        if self.state_enabled("text_size_control"):
            ctrls = home_snap.get("text_size_controls", [])
            pc = self.pc(home_url, "desktop", "text_size_control")
            if not ctrls:
                self.obs.append(R.mk(pc, "VIS-14", "rt.text_size_control", "not_found_in_scope", "not_tested", "no on-page text-size control"))
            else:
                def tsc():
                    p = ctx_d.new_page()
                    try:
                        self.sess.goto(p, home_url)
                        self.sess.ensure_lib(p)
                        fresh = self.sess.snapshot(p)
                        inc = next((c for c in fresh.get("text_size_controls", []) if "+" in c["text"] or re.search(r"increase|larger|बढ़", c["name"], re.I)), None) \
                            or (fresh.get("text_size_controls") or [None])[0]
                        if not inc:
                            self.obs.append(R.mk(pc, "VIS-14", "rt.text_size_control", "present", "not_tested", "control found at first load but not on reload"))
                            return
                        r = C.text_size_control_test(self.sess, p, inc)
                        grew = r.get("after") and r.get("before") and r["after"] > r["before"]
                        self.obs.append(R.mk(pc, "VIS-14", "rt.text_size_control", "present", "pass" if grew else "fail",
                                             f"font-size {r.get('before')} -> {r.get('after')} px after activating {inc.get('text') or inc.get('name')!r}",
                                             evidence=[dom_evidence(home_url, inc["selector"], inc.get("name") or "", "desktop", "text_size_control")], meas=r))
                        self.ran("text_size_control")
                    finally:
                        p.close()
                self.guard("text_size_control", tsc)
        if self.state_enabled("color_scheme"):
            self.guard("color_scheme", self._color_scheme, home_url, home_snap)
        if self.state_enabled("offline"):
            def off():
                pc = self.pc(home_url, "desktop", "offline")
                if self.meta.get("offline_relevant") is False:
                    self.obs.append(R.mk(pc, "RES-02", "rt.offline", "not_applicable", "not_tested", "metadata offline_relevant=false", appl="metadata offline_relevant=false"))
                    return
                r = C.offline_check(self.sess, home_url, self.rt["viewports"]["desktop"], self.page_dir(home_url) / "offline.jpg")
                regs = r.get("sw_registrations") or 0
                if r.get("error"):
                    self.obs.append(R.mk(pc, "RES-02", "rt.offline", "unknown", "not_tested", f"offline check failed: {r['error']}", meas=r))
                elif regs <= 0:
                    note = "" if self.meta.get("offline_relevant") else " (relevance not established by metadata; descriptive)"
                    self.obs.append(R.mk(pc, "RES-02", "rt.offline", "not_found_in_scope", "not_tested", f"no service worker registered{note}", meas=r))
                else:
                    self.obs.append(R.mk(pc, "RES-02", "rt.offline", "present", "pass" if r.get("offline_rendered") else "fail",
                                         f"{regs} service worker(s); offline reload {'rendered content' if r.get('offline_rendered') else 'failed'}", meas=r))
                self.ran("offline")
            self.guard("offline", off)

    def _color_scheme(self, home_url, home_snap):
        pc = self.pc(home_url, "desktop", "color_scheme")
        ctx = self.sess.new_context(self.rt["viewports"]["desktop"], color_scheme="dark")
        try:
            p = ctx.new_page()
            r = self.sess.goto(p, home_url)
            if not r["ok"]:
                return
            self.sess.ensure_lib(p)
            lum_dark = p.evaluate("__dpi.bgLuminance()")
            axe_dark = self.sess.run_axe(p, ["color-contrast"])
        finally:
            ctx.close()
        ctx2 = self.sess.new_context(self.rt["viewports"]["desktop"], color_scheme="light")
        try:
            p2 = ctx2.new_page()
            self.sess.goto(p2, home_url)
            self.sess.ensure_lib(p2)
            lum_light = p2.evaluate("__dpi.bgLuminance()")
        finally:
            ctx2.close()
        def nviol(a):
            return sum(v["node_count"] for v in a.get("violations", []) if v["id"] == "color-contrast")
        base = nviol(home_snap.get("_axe", {}))
        dark = nviol(axe_dark)
        changed = lum_dark is not None and lum_light is not None and abs(lum_dark - lum_light) > 30
        toggles = home_snap.get("theme_toggles", [])
        meas = {"bg_luminance_light": lum_light, "bg_luminance_dark": lum_dark, "contrast_nodes_light": base, "contrast_nodes_dark": dark, "toggles": toggles[:3]}
        if changed:
            self.obs.append(R.mk(pc, "VIS-15", "rt.color_scheme", "present", "pass" if dark <= base else "fail",
                                 f"page responds to prefers-color-scheme; color-contrast violation nodes {base} (light) vs {dark} (dark)", meas=meas))
        elif toggles:
            self.obs.append(R.mk(pc, "VIS-15", "rt.color_scheme", "present", "not_tested", f"theme/contrast toggle {toggles[0]['name']!r} found (not operated)", meas=meas, grade="heuristic_inference"))
        else:
            self.obs.append(R.mk(pc, "VIS-15", "rt.color_scheme", "not_found_in_scope", "not_tested", "no response to dark scheme and no toggle", meas=meas))
        self.ran("color_scheme")

    def _lang_obs(self, url, r) -> list[Observation]:
        pc = self.pc(url, "desktop", "language_switch", True)
        thr = self.cfg["thresholds"]
        out = []
        ev = [dom_evidence(url, c["selector"], str(c["options"])[:300], "desktop", "language_switch", c["kind"]) for c in r.get("candidates", [])[:3]]
        if r.get("error") and not r.get("found"):
            for fid in ("LANG-01", "LANG-02", "LANG-03", "LANG-06"):
                out.append(R.mk(pc, fid, "rt.lang_switch", "unknown", "not_tested", f"language test failed: {r['error']}"))
            return out
        if not r.get("found"):
            why = "language-labelled control found but its options could not be identified" if r.get("toggle_only") else "no control offering language choices on the homepage"
            out.append(R.mk(pc, "LANG-01", "rt.lang_switch", "not_found_in_scope", "not_tested", why, evidence=ev, manual=bool(r.get("toggle_only"))))
            for fid in ("LANG-02", "LANG-03", "LANG-06"):
                out.append(R.mk(pc, fid, "rt.lang_switch", "not_found_in_scope" if fid != "LANG-06" else "unknown", "not_tested", "no switcher to test"))
            self.obs_rtl(pc, r, out)
            return out
        out.append(R.mk(pc, "LANG-01", "rt.lang_switch", "present", "not_tested", f"switcher offering {r.get('offered_languages')}", evidence=ev,
                        meas={"offered_languages": r.get("offered_languages"), "base_language": r.get("base_language")}))
        if not r.get("attempted") or r.get("activation_error"):
            why = r.get("activation_error") or r.get("note") or "switch not attempted"
            for fid in ("LANG-02", "LANG-03", "LANG-06"):
                out.append(R.mk(pc, fid, "rt.lang_switch", "unknown", "not_tested", why))
            self.obs_rtl(pc, r, out)
            return out
        cr = r.get("changed_ratio") or 0.0
        cond_text = cr >= float(thr["lang_switch_min_changed_ratio"])
        cond_lang = bool(r.get("lang_attr_matches"))
        ev2 = [{"type": "language_switch", "option": r.get("option"), "html_lang_before": r.get("html_lang_before"), "html_lang_after": r.get("html_lang_after"),
                "url_after": r.get("url_after"), "text_before": r.get("text_sample_before"), "text_after": r.get("text_sample_after")},
               {"type": "screenshot", "path": r.get("screenshot")}]
        meas = {k: r.get(k) for k in ("changed_ratio", "segments_before", "segments_after", "lang_attr_matches", "in_target_language", "url_changed", "language_evidence")}
        avail = "present" if r.get("observable_change") else "not_found_in_scope"
        ver = "pass" if cond_text and cond_lang else "partial" if (cond_text or cond_lang) else "fail"
        out.append(R.mk(pc, "LANG-02", "rt.lang_switch", avail, ver,
                        f"switch to {r['option']['code']}: {cr:.0%} of visible segments changed (threshold {float(thr['lang_switch_min_changed_ratio']):.0%}); "
                        f"html lang {r.get('html_lang_before')!r} -> {r.get('html_lang_after')!r}", evidence=ev2, meas=meas, manual=True))
        per = r.get("persistence")
        if not per:
            out.append(R.mk(pc, "LANG-03", "rt.lang_switch", "unknown", "not_tested", "persistence not tested (switch produced no change)"))
        else:
            nav, rel = per.get("navigation", {}), per.get("reload", {})
            tested = [x for x in (nav, rel) if x.get("tested")]
            kept = [x for x in tested if x.get("retained")]
            if not tested:
                out.append(R.mk(pc, "LANG-03", "rt.lang_switch", "unknown", "not_tested", f"persistence steps failed: {nav.get('reason')}; {rel.get('reason')}"))
            else:
                ver3 = "pass" if len(kept) == 2 else "partial" if kept else "fail"
                if len(tested) == 1 and kept:
                    ver3 = "partial"
                out.append(R.mk(pc, "LANG-03", "rt.lang_switch", "present" if kept else "not_found_in_scope", ver3,
                                f"after navigation: {nav.get('retained') if nav.get('tested') else 'not tested'}; after reload: {rel.get('retained') if rel.get('tested') else 'not tested'}",
                                evidence=[{"type": "language_persistence", **per}], meas=per))
        ur = r.get("untranslated_ratio")
        if ur is None:
            out.append(R.mk(pc, "LANG-06", "rt.lang_switch", "unknown", "not_tested", "no comparable text segments"))
        else:
            v6 = "pass" if ur <= float(thr["untranslated_pass_max_ratio"]) else "fail" if ur >= float(thr["untranslated_fail_min_ratio"]) else "partial"
            out.append(R.mk(pc, "LANG-06", "rt.lang_switch", "present", v6, f"{ur:.0%} of eligible visible segments unchanged after switching",
                            evidence=[{"type": "untranslated_segments", "examples": r.get("untranslated", [])[:20]}],
                            meas={"untranslated_ratio": ur, "eligible_segments": r.get("eligible_segments")}, manual=True))
        self.obs_rtl(pc, r, out)
        return out

    def obs_rtl(self, pc, r, out):
        exp = [l for l in (self.meta.get("expected_languages") or []) if L.is_rtl(l)]
        offered = r.get("rtl_offered") or []
        if not exp and not offered:
            out.append(R.mk(pc, "LANG-09", "rt.lang_switch", "not_applicable", "not_tested", "no RTL language offered or expected", appl="no RTL language"))
            return
        rt = r.get("rtl")
        if not rt:
            out.append(R.mk(pc, "LANG-09", "rt.lang_switch", "unknown", "not_tested", f"RTL relevance (expected {exp}, offered {offered}) but no RTL switch could be performed"))
            return
        is_rtl = "rtl" in (rt.get("main_dir"), rt.get("body_dir")) or (rt.get("html_dir") or "").lower() == "rtl"
        out.append(R.mk(pc, "LANG-09", "rt.lang_switch", "present" if is_rtl else "not_found_in_scope", "pass" if is_rtl else "fail",
                        f"after switching to {rt['option']}: html dir={rt.get('html_dir')!r}, body direction={rt.get('body_dir')}, main direction={rt.get('main_dir')}",
                        meas=rt, manual=True))

    # -------------------------------------------------------------- forms / links / journeys
    def forms_tests(self, ctx_d, snaps):
        if not self.state_enabled("form_error"):
            return
        maxf = int(self.rt["states"]["form_error"].get("max_forms", 3))
        chosen = []
        for u, s in snaps.items():
            for f in s.get("forms", []):
                fields = [x for x in f.get("fields", []) if x.get("visible")]
                if not fields or not f.get("visible"):
                    continue
                if f.get("role") == "search" or (len(fields) == 1 and (fields[0].get("type") == "search" or (fields[0].get("name") or "") in ("q", "s", "search", "query"))):
                    continue
                if not any(x.get("required") or x.get("aria_required") == "true" or x.get("pattern") or x.get("type") == "email" for x in fields):
                    continue
                chosen.append((u, f["selector"]))
        for u, sel in chosen[:maxf]:
            def one(u=u, sel=sel):
                p = ctx_d.new_page()
                try:
                    r = self.sess.goto(p, u)
                    if not r["ok"]:
                        return
                    rec = C.form_error_test(self.sess, p, sel)
                    self.sess.screenshot(p, self.page_dir(u) / f"form_error_{short_hash(sel, 6)}.jpg")
                    self.obs += R.form_state(self.pc(u, "desktop", f"form_error:{short_hash(sel, 6)}", form=sel), rec)
                    self.ran("form_error")
                finally:
                    p.close()
            self.guard("form_error", one)

    def link_checks(self, ctx_d, snaps):
        if not self.state_enabled("link_check"):
            return
        maxl = int(self.rt["states"]["link_check"].get("max_links", 80))
        targets = {}
        for o in self.obs:
            if o.feature_id in ("NAV-09", "NAV-10", "TRUST-05", "TRUST-07", "TRUST-08") and o.availability == "present":
                for t in (o.measurements.get("targets") or [])[:1]:
                    if t and t.startswith("http"):
                        targets.setdefault(o.feature_id, t)
        per_page: dict[str, list[str]] = {}
        order: list[str] = list(dict.fromkeys(targets.values()))
        for u, s in snaps.items():
            links = []
            for l in s.get("links", []):
                a = (l.get("abs") or "").split("#")[0]
                if l.get("internal") and a.startswith("http") and not self.sess.policy.excluded(a) and not l.get("download"):
                    links.append(a)
            per_page[u] = list(dict.fromkeys(links))
            order += per_page[u]
        order = list(dict.fromkeys(order))[:maxl]
        res = C.link_check(self.sess, ctx_d, order)
        self.link_results = res
        self.ran("link_check")
        checked = set(res)
        for u, links in per_page.items():
            mine = [l for l in links if l in checked]
            pc = self.pc(u, "desktop", "link_check")
            if not mine:
                self.obs.append(R.mk(pc, "NAV-12", "rt.link_check", "not_applicable" if not links else "unknown", "not_tested",
                                     "no internal links" if not links else "link budget exhausted before this page's links", appl="no internal links" if not links else ""))
                continue
            broken = [l for l in mine if res[l]["class"] == "broken"]
            ev = [{"type": "http", "url": l, "status": res[l]["status"]} for l in broken[:6]]
            self.obs.append(R.mk(pc, "NAV-12", "rt.link_check", "present", "fail" if broken else "pass",
                                 f"{len(broken)}/{len(mine)} checked internal links broken (404/410/5xx)" if broken else f"{len(mine)} checked internal links resolve",
                                 evidence=ev, meas={"checked": len(mine), "broken": len(broken), "restricted": sum(res[l]['class'] == 'restricted' for l in mine),
                                                    "unchecked": len(links) - len(mine)}))
        for fid, t in targets.items():
            r = res.get(t)
            pc = self.pc(self.url, "desktop", "link_check", target=t)
            if not r:
                continue
            if r["class"] in ("ok", "restricted"):
                self.obs.append(R.mk(pc, fid, "rt.link_check", "present", "pass", f"target {t} responds {r['status']}", kind="interface"))
            elif r["class"] == "broken":
                self.obs.append(R.mk(pc, fid, "rt.link_check", "present", "fail", f"target {t} responds {r['status']}", kind="interface"))
            else:
                self.obs.append(R.mk(pc, fid, "rt.link_check", "present", "not_tested", f"target {t}: {r['class']}", kind="interface"))

    def journeys(self, ctx_d, home_url):
        js = self.meta.get("critical_journeys") or []
        if not js:
            self.obs.append(R.mk(self.pc(self.url, "desktop", "journeys"), "NAV-14", "rt.journeys", "not_applicable", "not_tested",
                                 "no critical journeys defined in metadata", appl="no journeys supplied", kind="interface"))
            return
        for j in js:
            def one(j=j):
                r = C.run_journey(self.sess, ctx_d, j, home_url, self.page_dir(home_url))
                pc = self.pc(j.get("start_url") or home_url, "desktop", f"journey:{j.get('id')}")
                tot, done = r.get("steps_total", 0), r.get("steps_completed", 0)
                if not r.get("start_ok"):
                    self.obs.append(R.mk(pc, "NAV-14", "rt.journeys", "not_found_in_scope", "not_tested", f"journey start unreachable: {r.get('error')}", meas=r))
                    return
                ver = "pass" if done == tot else "fail" if done == 0 else "partial"
                self.obs.append(R.mk(pc, "NAV-14", "rt.journeys", "present", ver, f"{done}/{tot} steps completed", evidence=[{"type": "journey_log", "log": r.get("log")}],
                                     meas=r, manual=True))
                self.ran("journeys")
            self.guard("journey", one)

    def interface_level(self, snaps):
        # LANG-04: union of observed languages
        langs: dict[str, list[str]] = {}
        for u, s in snaps.items():
            if s.get("html_lang"):
                langs.setdefault(L.primary(s["html_lang"]), []).append(f"html lang on {u}")
            for seg in s.get("text", []):
                if seg.get("lang"):
                    langs.setdefault(L.primary(seg["lang"]), []).append(f"lang attribute on {u}")
            shares = L.script_shares(" ".join(x["t"] for x in s.get("text", [])))
            ref = L.likely_script(s.get("html_lang"))
            for sc, share in shares.items():
                if share >= 0.15 and sc != ref and sc not in ("Other",):
                    langs.setdefault(f"script:{sc}", []).append(f"{share:.0%} of letters on {u}")
        lr = getattr(self, "lang_result", None) or {}
        for code in lr.get("offered_languages") or []:
            langs.setdefault(code, []).append("offered by language switcher")
        if lr.get("persistence") or lr.get("in_target_language"):
            code = (lr.get("option") or {}).get("code")
            if code:
                langs.setdefault(code, []).append("observed after switching")
        pc = self.pc(self.url, "desktop", "interface")
        distinct = {k for k in langs if k}
        self.obs.append(R.mk(pc, "LANG-04", "rt.languages", "present" if len(distinct) >= 2 else "not_found_in_scope" if distinct else "unknown", "not_tested",
                             f"observed languages/scripts: {sorted(distinct)}", meas={"observed": {k: v[:3] for k, v in langs.items()}}, kind="interface"))
        # NAV-08 search (interface-level applicability by size/type)
        n_internal = len([r for r in self.sampler.records.values()]) if hasattr(self, "sampler") else 0
        applicable = n_internal >= 20 or self.itype in ("data_portal", "documentation_site")
        controls = [(u, c) for u, s in snaps.items() for c in s.get("search_controls", []) if c.get("visible")]
        if not controls:
            self.obs.append(R.mk(pc, "NAV-08", "rt.search", "not_found_in_scope" if applicable else "not_applicable", "not_tested",
                                 f"no search control; {n_internal} URLs discovered" + ("" if applicable else " (small site: not applicable)"),
                                 appl="" if applicable else "fewer than 20 discovered URLs", kind="interface"))
        else:
            named = [c for u, c in controls if c.get("tag") != "input" or c.get("name")]
            seqs = getattr(self, "keyboard_seq", {})
            reached = any(c["selector"] in seqs.get(u, []) for u, c in controls if c.get("tag") == "input")
            ver = "pass" if named and (reached or not seqs) and len(named) == len(controls) else "fail" if not named else "partial"
            self.obs.append(R.mk(pc, "NAV-08", "rt.search", "present", ver, f"{len(controls)} search control(s); named: {len(named)}; keyboard reached: {reached}",
                                 evidence=[dom_evidence(u, c["selector"], c.get("name") or "", "desktop", "initial") for u, c in controls[:3]], kind="interface"))

    def fill_absent(self, snaps):
        """Emit explicit interface-level observations when a conditional test had no subject."""
        have = {o.feature_id for o in self.obs}
        pc = self.pc(self.url, "desktop", "interface")
        def na(fid, why, appl):
            self.obs.append(R.mk(pc, fid, "rt.absent", "not_applicable", "not_tested", why, appl=appl, kind="interface"))
        if "KBD-05" not in have and self.rt["states"]["modal"].get("enabled", True) and not self.states_skipped.get("modal"):
            if not any(s.get("dialog_triggers") for s in snaps.values()):
                na("KBD-05", f"no dialog triggers on {len(snaps)} sampled page(s)", "no dialogs observed")
        if self.rt["states"]["form_error"].get("enabled", True) and not self.states_skipped.get("form_error"):
            for fid in ("FORM-03", "FORM-04", "FORM-05", "KBD-11"):
                if fid not in have:
                    na(fid, f"no forms with client-side constraints on {len(snaps)} sampled page(s)", "no testable forms")
        if "FORM-08" not in have:
            na("FORM-08", "no password fields on sampled pages", "no authentication form observed")
