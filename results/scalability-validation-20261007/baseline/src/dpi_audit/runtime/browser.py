"""Playwright session with safety routing, politeness, axe-core injection and evidence capture.

Safety (always on unless explicitly relaxed for local fixtures / authorized test hosts):
* requests other than GET/HEAD/OPTIONS are aborted (no form submissions, no API writes);
* navigations to logout/delete/payment/calendar/download-like URLs are aborted;
* downloads are refused, JS dialogs dismissed, pop-up windows closed;
* service workers are blocked except in the dedicated offline-check context
  (their network requests could otherwise bypass routing);
* every top-level navigation consults robots.txt and the cross-process host rate limiter.
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.parse
from pathlib import Path

from ..util import package_root, repo_root, truncate
from .net import HostRateLimiter, Robots, UrlPolicy, host_of

JS_DIR = package_root() / "runtime" / "js"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def axe_path() -> Path:
    for base in (repo_root(), Path.cwd()):
        p = base / "node_modules" / "axe-core" / "axe.min.js"
        if p.exists():
            return p
    raise FileNotFoundError("node_modules/axe-core/axe.min.js not found: run `npm ci` in the dpi-audit checkout")


class BrowserSession:
    def __init__(self, cfg: dict, limiter: HostRateLimiter, log=None):
        self.cfg = cfg
        self.rt = cfg["runtime"]
        self.limiter = limiter
        self.log = log or (lambda *a, **k: None)
        self.policy = UrlPolicy(cfg)
        self.blocked: list[dict] = []
        self.popups = 0
        self.dialogs = 0
        self._pw = None
        self.browser = None
        self.versions: dict = {}
        self.lib_src = (JS_DIR / "lib.js").read_text("utf-8")
        self.init_src = (JS_DIR / "init.js").read_text("utf-8")
        self.axe_src = axe_path().read_text("utf-8")
        self.ua = None
        self.robots = None

    # ------------------------------------------------------------ lifecycle
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        from importlib.metadata import version
        self._pw = sync_playwright().start()
        kw = {"headless": bool(self.rt.get("headless", True))}
        if self.rt.get("executable_path"):
            kw["executable_path"] = self.rt["executable_path"]
        self.browser = self._pw.chromium.launch(**kw)
        ctx = self.browser.new_context()
        pg = ctx.new_page()
        base_ua = pg.evaluate("navigator.userAgent")
        ctx.close()
        self.ua = f"{base_ua} {self.rt['user_agent_suffix']}".strip()
        self.robots = Robots(self.rt["robots_user_agent"], self.ua, bool(self.rt.get("respect_robots", True)))
        axe_ver = json.loads((axe_path().parent / "package.json").read_text("utf-8"))["version"]
        self.versions = {"browser": "chromium", "browser_version": self.browser.version,
                         "playwright": version("playwright"), "axe_core": axe_ver, "user_agent": self.ua}
        return self

    def __exit__(self, *exc):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ------------------------------------------------------------ contexts
    def new_context(self, viewport: dict, reduced_motion: str | None = None, color_scheme: str | None = None,
                    allow_service_workers: bool = False):
        vp = {"width": int(viewport["width"]), "height": int(viewport["height"])}
        ctx = self.browser.new_context(
            viewport=vp, device_scale_factor=float(viewport.get("device_scale_factor", 1)),
            is_mobile=bool(viewport.get("is_mobile", False)), has_touch=bool(viewport.get("has_touch", False)),
            user_agent=self.ua, locale=self.rt.get("locale", "en-US"), timezone_id=self.rt.get("timezone", "UTC"),
            accept_downloads=False, service_workers="allow" if allow_service_workers else "block",
            reduced_motion=reduced_motion or "no-preference", color_scheme=color_scheme or "light",
            ignore_https_errors=False, java_script_enabled=True,
        )
        ctx.set_default_navigation_timeout(float(self.rt["navigation_timeout_s"]) * 1000)
        ctx.set_default_timeout(15000)
        ctx.add_init_script(script=self.init_src)
        ctx.route("**/*", self._route)
        ctx.on("page", self._on_page)
        return ctx

    def _on_page(self, page):
        page.on("dialog", self._on_dialog)
        opener = None
        try:
            opener = page.opener()
        except Exception:
            pass
        if opener is not None:  # pop-up windows opened by the site are closed
            self.popups += 1
            try:
                page.close()
            except Exception:
                pass

    def _on_dialog(self, dialog):
        self.dialogs += 1
        try:
            dialog.dismiss()
        except Exception:
            pass

    def _route(self, route, request):
        try:
            method = request.method.upper()
            url = request.url
            host = urllib.parse.urlsplit(url).hostname or ""
            allow_hosts = set(self.rt["safety"].get("allow_mutation_hosts") or [])
            if method not in SAFE_METHODS and self.rt["safety"].get("block_mutating_requests", True) and host not in allow_hosts:
                self.blocked.append({"url": truncate(url, 200), "method": method, "reason": "mutating_request_blocked"})
                return route.abort("blockedbyclient")
            if request.is_navigation_request():
                why = self.policy.excluded(url)
                if why and why.startswith("excluded_pattern"):
                    self.blocked.append({"url": truncate(url, 200), "method": method, "reason": why})
                    return route.abort("blockedbyclient")
            return route.continue_()
        except Exception:
            try:
                return route.continue_()
            except Exception:
                return None

    # ------------------------------------------------------------ navigation
    def goto(self, page, url: str, settle: bool = True, check_robots: bool = True) -> dict:
        res = {"url": url, "ok": False, "status": None, "final_url": None, "error": "", "robots": ""}
        if check_robots:
            ok, why = self.robots.allowed(url)
            res["robots"] = why
            if not ok:
                res["error"] = "blocked_robots"
                return res
            info = self.robots.info(url)
            self.limiter.set_crawl_delay(host_of(url), info.get("crawl_delay"))
        self.limiter.wait(host_of(url))
        t0 = time.time()
        try:
            resp = page.goto(url, wait_until="load")
            res["status"] = resp.status if resp else None
        except Exception as exc:
            res["error"] = f"navigation_error: {type(exc).__name__}: {truncate(str(exc).splitlines()[0], 200)}"
            try:
                res["final_url"] = page.url
            except Exception:
                pass
            return res
        if settle:
            self.settle(page)
        res["final_url"] = page.url
        res["load_s"] = round(time.time() - t0, 2)
        try:
            res["title"] = page.title()
        except Exception:
            res["title"] = ""
        res["ok"] = res["status"] is None or res["status"] < 400
        if not res["ok"]:
            res["error"] = f"http_{res['status']}"
        return res

    def settle(self, page):
        try:
            page.wait_for_load_state("networkidle", timeout=int(self.rt["network_idle_timeout_ms"]))
        except Exception:
            pass
        page.wait_for_timeout(int(self.rt["settle_ms"]))

    def ensure_lib(self, page) -> None:
        page.evaluate(self.lib_src)

    def ensure_axe(self, page) -> None:
        if not page.evaluate("typeof window.axe === 'object' && !!window.axe.run"):
            page.evaluate(self.axe_src)

    def snapshot(self, page, **opts) -> dict:
        self.ensure_axe(page)
        self.ensure_lib(page)
        return page.evaluate("(o) => window.__dpi.snapshot(o)", opts)

    def run_axe(self, page, rules: list[str] | None = None) -> dict:
        self.ensure_axe(page)
        max_nodes = int(self.cfg["evidence"]["axe_max_nodes_per_rule"])
        tags = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
        return page.evaluate("""async ([tags, rules, maxNodes]) => {
            const opts = rules ? {runOnly: {type: 'rule', values: rules}} : {runOnly: {type: 'tag', values: tags}};
            opts.resultTypes = undefined;
            const r = await axe.run(document, opts);
            const slim = (arr) => arr.map(x => ({id: x.id, impact: x.impact, tags: x.tags, help: x.help,
                helpUrl: x.helpUrl, node_count: x.nodes.length,
                nodes: x.nodes.slice(0, maxNodes).map(n => ({target: n.target, html: (n.html||'').slice(0, 300),
                    summary: (n.failureSummary||'').slice(0, 300), impact: n.impact,
                    data: (n.any||[]).concat(n.all||[]).map(c => c.data).filter(Boolean).slice(0,1)[0] || null}))}));
            return {violations: slim(r.violations), passes: slim(r.passes), incomplete: slim(r.incomplete),
                    inapplicable: r.inapplicable.map(x => ({id: x.id, tags: x.tags})),
                    testEngine: r.testEngine, testRunner: r.testRunner, toolOptions: r.toolOptions, url: r.url,
                    timestamp: r.timestamp};
        }""", [tags, rules, max_nodes])

    def screenshot(self, page, path: Path, full_page: bool | None = None) -> str | None:
        if not self.cfg["evidence"].get("screenshots", True):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        fmt = self.cfg["evidence"].get("screenshot_format", "jpeg")
        kw = {"path": str(path), "type": fmt, "full_page": bool(self.cfg["evidence"].get("full_page_screenshots") if full_page is None else full_page),
              "timeout": 15000}
        if fmt == "jpeg":
            kw["quality"] = int(self.cfg["evidence"].get("screenshot_quality", 70))
        try:
            page.screenshot(**kw)
            return str(path)
        except Exception:
            return None


def save_axe(path: Path, results: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False)
    return str(path)
