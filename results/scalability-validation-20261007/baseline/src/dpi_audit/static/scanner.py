"""Walk a checked-out repository read-only and classify first-party files.

Vendored dependencies, generated bundles and build outputs are excluded from normal
counts (config ``static.exclude_*``); every exclusion is recorded with its reason so the
coverage map states exactly what was and was not inspected.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE_EXT = {".html", ".htm", ".xhtml", ".vue", ".svelte", ".hbs", ".handlebars", ".mustache",
                ".ejs", ".njk", ".jinja", ".jinja2", ".j2", ".erb", ".php", ".twig", ".cshtml",
                ".razor", ".jsp", ".liquid", ".astro", ".haml", ".pug", ".jade", ".tpl", ".gohtml",
                ".tmpl", ".blade.php"}
JSX_EXT = {".jsx", ".tsx"}
SCRIPT_EXT = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}
STYLE_EXT = {".css", ".scss", ".sass", ".less", ".styl", ".pcss"}
SERVER_EXT = {".py", ".rb", ".java", ".kt", ".kts", ".go", ".cs", ".scala", ".clj", ".ex", ".exs",
              ".rs", ".dart", ".swift", ".m", ".groovy", ".pl"}
TRANSLATION_EXT = {".po", ".pot", ".properties", ".arb", ".xlf", ".xliff", ".strings", ".ftl",
                   ".resx", ".json", ".yml", ".yaml", ".xml", ".toml", ".ts", ".js"}
MANIFEST_NAMES = {"package.json", "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
                  "Pipfile", "composer.json", "Gemfile", "pom.xml", "build.gradle", "build.gradle.kts",
                  "go.mod", "Cargo.toml", "pubspec.yaml", "manifest.json", "site.webmanifest",
                  "manifest.webmanifest", "angular.json", "nuxt.config.js", "nuxt.config.ts",
                  "next.config.js", "next.config.mjs", "vite.config.js", "vite.config.ts",
                  "settings.py", "AndroidManifest.xml"}
I18N_DIR_NAMES = {"locales", "locale", "i18n", "l10n", "lang", "langs", "languages", "translations",
                  "translation", "messages", "intl", "nls", "trans", "localization", "localisation"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svgz", ".woff", ".woff2", ".ttf",
              ".otf", ".eot", ".mp4", ".mp3", ".webm", ".ogg", ".wav", ".pdf", ".zip", ".gz", ".tgz",
              ".jar", ".war", ".class", ".so", ".dll", ".exe", ".bin", ".mo", ".pyc", ".db", ".sqlite",
              ".psd", ".ai", ".sketch", ".fig", ".xlsx", ".docx", ".pptx"}


@dataclass
class FileInfo:
    rel: str
    size: int
    category: str            # template | jsx | script | style | translation | manifest | server | svg | other
    included: bool
    reason: str = ""         # exclusion reason when not included


@dataclass
class ScanResult:
    root: str
    files: list[FileInfo] = field(default_factory=list)
    truncated: bool = False
    total_bytes: int = 0

    def included(self, *cats: str) -> list[FileInfo]:
        return [f for f in self.files if f.included and (not cats or f.category in cats)]

    def coverage(self) -> dict:
        inc: dict[str, int] = {}
        exc: dict[str, int] = {}
        for f in self.files:
            if f.included:
                inc[f.category] = inc.get(f.category, 0) + 1
            else:
                exc[f.reason] = exc.get(f.reason, 0) + 1
        return {"files_seen": len(self.files), "files_inspected": sum(inc.values()),
                "inspected_by_category": dict(sorted(inc.items())),
                "excluded_by_reason": dict(sorted(exc.items())),
                "file_budget_truncated": self.truncated, "bytes_inspected": self.total_bytes}


def _category(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    low = name.lower()
    segs = [s.lower() for s in rel.split("/")[:-1]]
    ext = os.path.splitext(low)[1]
    if low.endswith(".blade.php"):
        return "template"
    if name in MANIFEST_NAMES:
        return "manifest"
    if is_translation_path(rel):
        return "translation"
    if ext in JSX_EXT:
        return "jsx"
    if ext in TEMPLATE_EXT:
        return "template"
    if ext in SCRIPT_EXT:
        return "script"
    if ext in STYLE_EXT:
        return "style"
    if ext == ".svg":
        return "svg"
    if ext in SERVER_EXT:
        return "server"
    if ext == ".xml" and any(s.startswith("layout") for s in segs):
        return "template"  # Android layouts (recorded; web detectors do not apply)
    return "other"


_LOCALE_TOKEN = re.compile(r"^[a-z]{2,3}([-_][A-Za-z]{2,4})?([-_][A-Za-z0-9]{2,8})?$", re.I)


def is_translation_path(rel: str) -> bool:
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    segs = low.split("/")[:-1]
    stem, ext = os.path.splitext(name)
    if ext in (".po", ".pot", ".arb", ".xlf", ".xliff", ".ftl"):
        return True
    if name == "localizable.strings" or (ext == ".strings" and any(s.endswith(".lproj") for s in segs)):
        return True
    if ext == ".xml" and name == "strings.xml" and any(s.startswith("values") for s in segs):
        return True
    if ext == ".properties" and re.search(r"(messages|resources?|bundle|strings|i18n|labels)", stem):
        return True
    if ext == ".resx" and re.search(r"\.[a-z]{2}(-[a-z]{2,4})?$", stem):
        return True
    if ext in (".json", ".yml", ".yaml", ".toml", ".js", ".ts"):
        in_i18n_dir = any(s in I18N_DIR_NAMES for s in segs)
        if in_i18n_dir and (_LOCALE_TOKEN.match(stem) or any(_LOCALE_TOKEN.match(s) for s in segs[-2:])):
            return ext != ".js" and ext != ".ts" or _LOCALE_TOKEN.match(stem) is not None
        if re.search(r"(^|[._-])(messages|translations?|strings|lang)[._-][a-z]{2}([-_][a-z]{2,4})?$", stem):
            return True
    return False


def scan(root: str | Path, cfg: dict) -> ScanResult:
    sc = cfg["static"]
    root = Path(root)
    exclude_dirs = [d.strip("/") for d in sc["exclude_dirs"]]
    single = {d for d in exclude_dirs if "/" not in d}
    multi = [d for d in exclude_dirs if "/" in d]
    include_override = [d.strip("/") for d in sc.get("include_dirs_override") or []]
    globs = sc["exclude_file_globs"]
    markers = [m.lower() for m in sc["generated_markers"]]
    max_bytes = int(sc["max_file_bytes"])
    res = ScanResult(root=str(root))
    candidates: list[FileInfo] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            forced = any(rel == o or rel.startswith(o + "/") or o.startswith(rel + "/") for o in include_override)
            if not forced and (d in single or d.startswith(".") and d not in (".github", ".well-known")
                               or any(rel == m or rel.endswith("/" + m) for m in multi)):
                res.files.append(FileInfo(rel + "/", 0, "directory", False, f"excluded_dir:{d}"))
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            full = Path(dirpath) / fn
            try:
                if full.is_symlink():
                    res.files.append(FileInfo(rel, 0, "other", False, "symlink_not_followed"))
                    continue
                size = full.stat().st_size
            except OSError:
                res.files.append(FileInfo(rel, 0, "other", False, "unreadable"))
                continue
            ext = os.path.splitext(fn.lower())[1]
            if ext in BINARY_EXT:
                res.files.append(FileInfo(rel, size, "binary", False, "binary_asset"))
                continue
            if any(fnmatch.fnmatch(fn, g) for g in globs):
                res.files.append(FileInfo(rel, size, _category(rel), False, "generated_or_lock_glob"))
                continue
            cat = _category(rel)
            if size > max_bytes:
                res.files.append(FileInfo(rel, size, cat, False, "over_size_limit_likely_generated"))
                continue
            candidates.append(FileInfo(rel, size, cat, True))
    # Generated-marker and binary sniffing for candidate text files.
    priority = {"template": 0, "jsx": 0, "translation": 1, "style": 2, "manifest": 2, "script": 3,
                "server": 4, "svg": 6, "other": 7}
    candidates.sort(key=lambda f: (priority.get(f.category, 9), f.rel))
    budget = int(sc["max_files"])
    total_cap = int(sc["max_total_bytes"])
    inspected = 0
    for f in candidates:
        if f.category == "other":
            f.included, f.reason = False, "not_relevant_file_type"
            res.files.append(f)
            continue
        if inspected >= budget or res.total_bytes + f.size > total_cap:
            f.included, f.reason = False, "file_budget_exhausted"
            res.truncated = True
            res.files.append(f)
            continue
        try:
            with open(root / f.rel, "rb") as fh:
                head = fh.read(4096)
        except OSError:
            f.included, f.reason = False, "unreadable"
            res.files.append(f)
            continue
        if b"\x00" in head:
            f.included, f.reason = False, "binary_content"
        elif any(m in head[:2048].decode("utf-8", "ignore").lower() for m in markers):
            f.included, f.reason = False, "generated_marker"
        elif f.category in ("script", "style") and _looks_minified(head):
            f.included, f.reason = False, "minified_content"
        else:
            inspected += 1
            res.total_bytes += f.size
        res.files.append(f)
    res.files.sort(key=lambda f: f.rel)
    return res


def _looks_minified(head: bytes) -> bool:
    text = head.decode("utf-8", "ignore")
    lines = text.splitlines() or [""]
    return len(text) >= 2000 and max(len(x) for x in lines) > 1500 and len(lines) < 5


def read_text(root: str | Path, rel: str) -> str:
    with open(Path(root) / rel, "rb") as fh:
        return fh.read().decode("utf-8", "replace")
