"""Deterministic page discovery and sampling for one web interface.

Order: homepage; metadata critical-journey URLs; then quotas of auth, service, form,
help, contact and navigation pages from homepage links (DOM order), then other
homepage links, sitemap URLs and second-level links, up to ``runtime.max_pages``.
Every discovered URL is recorded with its fate (sampled/skipped/blocked) and reason.
"""
from __future__ import annotations

import re
import urllib.parse

from .net import normalize_url, same_site

TYPE_RULES = [
    ("auth", re.compile(r"\blog[-_ ]?in\b|\bsign[-_ ]?in\b|signin|login|\bauth\b|लॉग ?इन|साइन ?इन|प्रवेश करें", re.I)),
    ("service", re.compile(r"\bapply\b|application|e-?services?|\bservices?\b|register|registration|\btrack\b|status|certificate|scheme|"
                           r"\bbook\b|booking|आवेदन|सेवा|पंजीकरण|योजना|प्रमाण", re.I)),
    ("form", re.compile(r"\bform\b|feedback|\bapply\b|register|contact|enquiry|inquiry|फॉर्म|प्रतिक्रिया", re.I)),
    ("help", re.compile(r"\bhelp\b|\bfaqs?\b|frequently|guide|how[-_ ]to|manual|tutorial|सहायता|मदद|प्रश्न", re.I)),
    ("contact", re.compile(r"contact|support|helpdesk|helpline|grievance|complaint|संपर्क|शिकायत|हेल्पलाइन", re.I)),
]


def link_types(text: str, url: str) -> list[str]:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    hay = f"{text} {path.replace('-', ' ').replace('_', ' ').replace('/', ' ')}"
    return [name for name, rx in TYPE_RULES if rx.search(hay)]


def path_depth(url: str) -> int:
    return len([p for p in urllib.parse.urlsplit(url).path.split("/") if p])


class Sampler:
    def __init__(self, home_url: str, cfg: dict, policy, robots):
        self.home = home_url
        self.cfg = cfg
        self.rt = cfg["runtime"]
        self.policy = policy
        self.robots = robots
        self.max_pages = int(self.rt["max_pages"])
        self.records: dict[str, dict] = {}
        self.sampled: list[dict] = []
        self.drop = tuple(self.rt["safety"]["drop_query_params"])

    def norm(self, url: str) -> str:
        return normalize_url(url, self.drop)

    def _record(self, url, source, types, status, reason, text=""):
        if url not in self.records:
            self.records[url] = {"url": url, "source": source, "types": types, "status": status, "reason": reason,
                                 "link_text": text[:80]}
        return self.records[url]

    def consider(self, raw_url: str, source: str, text: str = "", forced_type: str | None = None) -> dict | None:
        if not raw_url or raw_url.startswith(("mailto:", "tel:", "javascript:", "data:")):
            return None
        url = self.norm(raw_url.split("#")[0])
        if url in self.records:
            return self.records[url]
        if not same_site(url, self.home):
            return self._record(url, source, [], "skipped", "off_site")
        why = self.policy.excluded(url)
        if why:
            return self._record(url, source, [], "skipped", why, text)
        if not self.policy.query_variant_ok(url):
            return self._record(url, source, [], "skipped", "query_variant_limit", text)
        ok, rwhy = self.robots.allowed(url)
        if not ok:
            return self._record(url, source, [], "blocked", rwhy, text)
        types = [forced_type] if forced_type else link_types(text, url)
        return self._record(url, source, types, "candidate", "", text)

    def take(self, url: str, page_type: str, source: str) -> bool:
        rec = self.records.get(url)
        if rec is None or rec["status"] != "candidate":
            return False
        if len(self.sampled) >= self.max_pages:
            rec.update(status="skipped", reason=f"page_cap_{self.max_pages}")
            return False
        rec.update(status="sampled", reason=f"quota:{page_type}", page_type=page_type)
        self.sampled.append({"url": url, "page_type": page_type, "source": source})
        return True

    def plan_from_home(self, home_final_url: str, snap: dict, journeys: list[dict] | None) -> None:
        home = self.norm(home_final_url)
        self._record(home, "input", ["home"], "candidate", "")
        self.take(home, "home", "input")
        q = self.rt["sampling"]
        for j in journeys or []:
            for u in [j.get("start_url")] + [s.get("url") for s in j.get("steps", []) if s.get("action") == "goto"]:
                if u:
                    self.consider(u, "critical_journey", j.get("id", ""), "critical_journey")
        links = [l for l in snap.get("links", []) if l.get("abs")]
        for l in links:
            self.consider(l["abs"], f"home:{l.get('region')}", l.get("text") or l.get("name") or "")
        cands = [r for r in self.records.values() if r["status"] == "candidate"]
        order = {u: i for i, u in enumerate(self.records)}

        def pick(kind: str, limit: int, pred) -> None:
            n = 0
            for r in sorted(cands, key=lambda r: order[r["url"]]):
                if n >= limit:
                    break
                if r["status"] == "candidate" and pred(r):
                    if self.take(r["url"], kind, r["source"]):
                        n += 1

        pick("critical_journey", int(q["critical_journeys"]), lambda r: "critical_journey" in r["types"])
        for kind in ("auth", "service", "form", "help", "contact"):
            pick(kind, int(q[kind]), lambda r, k=kind: k in r["types"])
        nav_regions = ("home:nav", "home:header")
        pick("navigation", int(q["navigation"]), lambda r: r["source"] in nav_regions)
        pick("other", self.max_pages, lambda r: True)

    def add_more(self, urls_with_text: list[tuple[str, str]], source: str) -> None:
        for u, t in urls_with_text:
            if len(self.sampled) >= self.max_pages:
                break
            rec = self.consider(u, source, t)
            if rec and rec["status"] == "candidate":
                self.take(rec["url"], "other", source)

    def finalize(self) -> None:
        for r in self.records.values():
            if r["status"] == "candidate":
                r.update(status="skipped", reason=f"page_cap_{self.max_pages}")

    def stats(self) -> dict:
        c: dict[str, int] = {}
        for r in self.records.values():
            c[r["status"]] = c.get(r["status"], 0) + 1
        return {"discovered": len(self.records), **c}
