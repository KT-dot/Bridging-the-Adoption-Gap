"""Politeness and URL hygiene: robots.txt, per-host rate limiting shared across worker
processes, URL normalisation and trap/destructive-URL exclusion."""
from __future__ import annotations

import fcntl
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path


class HostRateLimiter:
    """Minimum delay between top-level requests to the same host, across processes."""

    def __init__(self, state_dir: Path, min_delay_s: float):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.delay = float(min_delay_s)
        self.extra: dict[str, float] = {}

    def set_crawl_delay(self, host: str, seconds: float | None) -> None:
        if seconds:
            self.extra[host] = min(float(seconds), 30.0)

    def wait(self, host: str) -> float:
        delay = max(self.delay, self.extra.get(host, 0.0))
        if delay <= 0 or not host:
            return 0.0
        path = self.dir / (re.sub(r"[^A-Za-z0-9.-]", "_", host) or "_")
        with open(path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            raw = fh.read().strip()
            last = float(raw) if raw else 0.0
            waited = max(0.0, last + delay - time.time())
            if waited:
                time.sleep(waited)
            fh.seek(0)
            fh.truncate()
            fh.write(str(time.time()))
            fh.flush()
            fcntl.flock(fh, fcntl.LOCK_UN)
        return waited


class Robots:
    def __init__(self, user_agent_token: str, full_ua: str, respect: bool = True, timeout: float = 15):
        self.token, self.ua, self.respect, self.timeout = user_agent_token, full_ua, respect, timeout
        self.cache: dict[str, dict] = {}

    def info(self, url: str) -> dict:
        p = urllib.parse.urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if base in self.cache:
            return self.cache[base]
        entry = {"robots_url": base + "/robots.txt", "status": None, "parser": None, "note": ""}
        req = urllib.request.Request(entry["robots_url"], headers={"User-Agent": self.ua})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read(500_000).decode("utf-8", "replace")
                entry["status"] = r.status
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(body.splitlines())
                entry["parser"] = rp
                entry["crawl_delay"] = rp.crawl_delay(self.token) or rp.crawl_delay("*")
                entry["sitemaps"] = rp.site_maps() or []
        except urllib.error.HTTPError as e:
            entry["status"] = e.code
            entry["note"] = "4xx: no robots restrictions" if 400 <= e.code < 500 else "5xx: treated as disallow-all (conservative)"
        except Exception as e:
            entry["status"] = "error"
            entry["note"] = f"robots.txt unreachable ({type(e).__name__}); host likely unreachable"
        self.cache[base] = entry
        return entry

    def allowed(self, url: str) -> tuple[bool, str]:
        if not self.respect:
            return True, "robots not consulted (runtime.respect_robots=false — local fixtures only)"
        e = self.info(url)
        if e["parser"] is not None:
            ok = e["parser"].can_fetch(self.token, url)
            return ok, "allowed by robots.txt" if ok else "disallowed by robots.txt"
        st = e["status"]
        if isinstance(st, int) and 500 <= st < 600:
            return False, "robots.txt returned 5xx: treated as disallow"
        if st == "error":
            return True, e["note"]
        return True, "no robots.txt"


def normalize_url(url: str, drop_params: list[str] | tuple = ()) -> str:
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        return url
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if k not in drop_params]
    path = p.path or "/"
    host = (p.hostname or "").lower()
    netloc = host + (f":{p.port}" if p.port and not ((p.scheme, p.port) in (("http", 80), ("https", 443))) else "")
    return urllib.parse.urlunsplit((p.scheme.lower(), netloc, path, urllib.parse.urlencode(sorted(q)), ""))


class UrlPolicy:
    def __init__(self, cfg: dict):
        s = cfg["runtime"]["safety"]
        self.exclude = [re.compile(p) for p in s["url_exclude_patterns"]]
        self.max_len = int(s["max_url_length"])
        self.drop = tuple(s["drop_query_params"])
        self.max_variants = int(s["max_query_variants_per_path"])
        self.variants: dict[str, set] = {}

    def excluded(self, url: str) -> str | None:
        if len(url) > self.max_len:
            return "url_too_long"
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https"):
            return f"scheme_{p.scheme or 'none'}"
        target = p.path + ("?" + p.query if p.query else "")
        for rx in self.exclude:
            if rx.search(target):
                return f"excluded_pattern:{rx.pattern[:40]}"
        return None

    def query_variant_ok(self, url: str) -> bool:
        p = urllib.parse.urlsplit(url)
        key = f"{p.netloc}{p.path}"
        s = self.variants.setdefault(key, set())
        if p.query in s:
            return True
        if len(s) >= self.max_variants:
            return False
        s.add(p.query)
        return True


def same_site(a: str, b: str) -> bool:
    ha = (urllib.parse.urlsplit(a).hostname or "").lower().removeprefix("www.")
    hb = (urllib.parse.urlsplit(b).hostname or "").lower().removeprefix("www.")
    pa = urllib.parse.urlsplit(a).port
    pb = urllib.parse.urlsplit(b).port
    return ha == hb and pa == pb


def host_of(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return (p.hostname or "") + (f":{p.port}" if p.port else "")


def fetch_sitemap(url: str, ua: str, max_urls: int, timeout: float = 15) -> list[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(3_000_000).decode("utf-8", "replace")
    except Exception:
        return []
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)[:max_urls]
