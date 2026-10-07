"""Translation-resource inventory, per-locale key coverage, and usage evidence.

A translation file is evidence of localisation only when first-party code uses it:
we look for (a) i18n call patterns, (b) literal references to sampled keys, and
(c) references to the resource path/directory from code. Resources without any of
these are reported as *unused translation assets*.
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import yaml

from .. import lang as L
from .parse import js_object_strings
from .scanner import ScanResult, read_text

I18N_LIBS = {
    "npm": ["i18next", "react-i18next", "next-i18next", "vue-i18n", "@nuxtjs/i18n", "react-intl",
            "@formatjs/intl", "next-intl", "svelte-i18n", "@ngx-translate/core", "@angular/localize",
            "i18n-js", "node-polyglot", "lingui", "@lingui/core", "typesafe-i18n", "rosetta",
            "@transifex/native", "i18next-browser-languagedetector", "vue-i18next", "@inlang/paraglide-js"],
    "python": ["django", "flask-babel", "babel", "gettext", "python-i18n", "fastapi-babel"],
    "ruby": ["rails-i18n", "i18n", "globalize", "mobility"],
    "php": ["laravel/framework", "symfony/translation", "gettext/gettext"],
    "dart": ["flutter_localizations", "intl", "easy_localization"],
}
CALL_PATTERNS = [
    (r"\bi18n(ext)?\.t\(", "i18n.t()"), (r"\buseTranslation\(", "useTranslation()"),
    (r"\$t\(\s*['\"`]", "$t()"), (r"(?<![\w.$])t\(\s*['\"`][\w.:-]+['\"`]", "t('key')"),
    (r"<Trans\b", "<Trans>"), (r"\bFormattedMessage\b", "FormattedMessage"),
    (r"\bformatMessage\(", "formatMessage()"), (r"\buseIntl\(", "useIntl()"),
    (r"\bgettext\(|\bngettext\(|\bugettext\(|\bgettext_lazy\(", "gettext()"),
    (r"(?<![\w.])_\(\s*['\"]", "_('text')"), (r"\{%\s*(trans|blocktrans|translate)\b", "{% trans %}"),
    (r"\bI18n\.t\(|(?<![\w.])t\(\s*['\"]\.", "I18n.t()"), (r"@lang\(|\btrans\(|__\(\s*['\"]", "trans()/__()"),
    (r"\|\s*translate\b", "| translate"), (r"\$localize\b", "$localize"), (r"\bi18n=\"", "Angular i18n attr"),
    (r"AppLocalizations\.of\(", "AppLocalizations"), (r"getString\(R\.string\.|@string/", "Android string res"),
    (r"NSLocalizedString\(", "NSLocalizedString"), (r"\bintl\.get\(|\bpolyglot\.t\(", "polyglot/intl"),
    (r"\bchangeLanguage\(|\bsetLocale\(|\bi18n\.locale\s*=|\blocale\.value\s*=", "locale change call"),
]
LOADER_PATTERNS = [r"loadPath\s*:", r"backend\s*:\s*\{", r"import\s+\w+\s+from\s+['\"][^'\"]*(locales?|i18n|lang|translations?)/",
                   r"require\(\s*['\"][^'\"]*(locales?|i18n|lang|translations?)/", r"LOCALE_PATHS", r"messages\s*:\s*\{",
                   r"addResourceBundle\(", r"resources\s*:\s*\{", r"i18n\.load_path", r"localizationsDelegates"]


@dataclass
class Resource:
    rel: str
    locale: str
    fmt: str
    keys: dict           # key -> value (str)
    namespace: str = ""
    is_template: bool = False


@dataclass
class I18nResult:
    resources: list[Resource] = field(default_factory=list)
    unparsed: list[dict] = field(default_factory=list)
    locales: dict = field(default_factory=dict)          # locale -> key count
    base_locale: str | None = None
    coverage: dict = field(default_factory=dict)         # locale -> {coverage, identical_to_base, empty}
    libs: list[str] = field(default_factory=list)
    call_sites: list[dict] = field(default_factory=list)
    loader_sites: list[dict] = field(default_factory=list)
    keys_sampled: int = 0
    keys_referenced: int = 0
    key_examples: list[dict] = field(default_factory=list)
    path_references: list[dict] = field(default_factory=list)
    used: bool | None = None
    usage_reason: str = ""

    def to_measurements(self) -> dict:
        return {"translation_files": len(self.resources), "unparsed_files": len(self.unparsed),
                "locales": self.locales, "base_locale": self.base_locale, "coverage": self.coverage,
                "i18n_libraries": self.libs, "call_sites": len(self.call_sites),
                "loader_sites": len(self.loader_sites), "keys_sampled": self.keys_sampled,
                "keys_referenced": self.keys_referenced, "used": self.used, "usage_reason": self.usage_reason}


def _flatten(obj, prefix="") -> dict:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}.{i}"))
    elif isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
        out[prefix] = str(obj)
    return out


_LOC = re.compile(r"^[a-z]{2,3}([-_][A-Za-z]{2,4})?([-_][A-Za-z0-9]{2,8})?$", re.I)


def _locale_from_path(rel: str) -> tuple[str | None, str]:
    """Return (locale, namespace) inferred from a resource path."""
    segs = rel.split("/")
    name = segs[-1]
    stem = name
    for ext in (".json", ".yml", ".yaml", ".po", ".pot", ".arb", ".properties", ".xlf", ".xliff", ".ftl",
                ".strings", ".xml", ".resx", ".toml", ".js", ".ts"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    if rel.lower().endswith("strings.xml"):
        d = segs[-2] if len(segs) >= 2 else ""
        if d == "values":
            return "default", ""
        m = re.match(r"values-(b\+)?([a-z]{2,3})(-r?([A-Z]{2}))?", d)
        return (m.group(2) + (("-" + m.group(4)) if m.group(4) else "")) if m else None, ""
    for s in segs[:-1]:
        if s.lower().endswith(".lproj"):
            return s[:-6], ""
    if "LC_MESSAGES" in segs:
        i = segs.index("LC_MESSAGES")
        return (segs[i - 1] if i > 0 else None), stem
    if _LOC.match(stem) and L.is_language_code(stem):
        return stem.replace("_", "-"), ""
    m = re.search(r"[._-]([a-z]{2,3}([-_][A-Za-z]{2,4})?)$", stem)
    if m and L.is_language_code(m.group(1)):
        return m.group(1).replace("_", "-"), stem[: m.start()]
    for s in reversed(segs[:-1]):
        if _LOC.match(s) and L.is_language_code(s):
            return s.replace("_", "-"), stem
    if rel.lower().endswith(".properties"):
        return "default", stem
    return None, stem


def _parse_resource(text: str, rel: str) -> tuple[dict, bool]:
    low = rel.lower()
    if low.endswith((".json", ".arb")):
        data = json.loads(text)
        flat = _flatten(data)
        if low.endswith(".arb"):
            flat = {k: v for k, v in flat.items() if not k.startswith("@")}
        return flat, False
    if low.endswith((".yml", ".yaml")):
        data = yaml.safe_load(text)
        if isinstance(data, dict) and len(data) == 1 and L.is_language_code(str(next(iter(data)))):
            data = next(iter(data.values()))  # Rails: {en: {...}}
        return _flatten(data), False
    if low.endswith((".po", ".pot")):
        from babel.messages.pofile import read_po
        import io
        cat = read_po(io.StringIO(text))
        out = {}
        for msg in cat:
            if not msg.id:
                continue
            mid = msg.id if isinstance(msg.id, str) else msg.id[0]
            s = msg.string if isinstance(msg.string, str) else (msg.string[0] if msg.string else "")
            out[mid] = s or ""
        return out, low.endswith(".pot")
    if low.endswith(".properties"):
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "!")):
                continue
            m = re.match(r"([^=:\s]+)\s*[=:]\s*(.*)", line)
            if m:
                out[m.group(1)] = m.group(2)
        return out, False
    if low.endswith(("strings.xml",)):
        root = ET.fromstring(text)
        return {el.get("name"): "".join(el.itertext()) for el in root.iter("string") if el.get("name")}, False
    if low.endswith((".xlf", ".xliff")):
        root = ET.fromstring(text)
        out = {}
        for el in root.iter():
            if el.tag.endswith("trans-unit") or el.tag.endswith("}unit") or el.tag == "unit":
                tid = el.get("id")
                tgt = next((c for c in el.iter() if c.tag.endswith("target")), None)
                src = next((c for c in el.iter() if c.tag.endswith("source")), None)
                if tid:
                    out[tid] = "".join(tgt.itertext()) if tgt is not None else ("" if src is None else "")
        return out, False
    if low.endswith(".strings"):
        return dict(re.findall(r'"((?:[^"\\]|\\.)+)"\s*=\s*"((?:[^"\\]|\\.)*)"\s*;', text)), False
    if low.endswith(".ftl"):
        return dict(re.findall(r"(?m)^([A-Za-z][\w-]*)\s*=\s*(.*)$", text)), False
    if low.endswith((".js", ".ts")):
        return js_object_strings(text, rel), False
    if low.endswith(".toml"):
        import tomllib
        return _flatten(tomllib.loads(text)), False
    raise ValueError("unsupported resource format")


def analyse(scan: ScanResult, texts: dict[str, str], deps: dict[str, list[str]]) -> I18nResult:
    res = I18nResult()
    for f in scan.included("translation"):
        locale, ns = _locale_from_path(f.rel)
        try:
            keys, is_tpl = _parse_resource(texts.get(f.rel) or read_text(scan.root, f.rel), f.rel)
        except Exception as exc:
            res.unparsed.append({"path": f.rel, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
            continue
        if not keys:
            res.unparsed.append({"path": f.rel, "error": "no string entries"})
            continue
        if locale is None and not is_tpl:
            res.unparsed.append({"path": f.rel, "error": "locale not identifiable from path"})
            continue
        res.resources.append(Resource(f.rel, "template" if is_tpl else locale, f.rel.rsplit(".", 1)[-1],
                                      keys, ns, is_tpl))
    per_locale: dict[str, dict] = {}
    for r in res.resources:
        if r.is_template:
            continue
        d = per_locale.setdefault(r.locale, {})
        for k, v in r.keys.items():
            d[(r.namespace, k)] = v
    res.locales = {loc: len(d) for loc, d in sorted(per_locale.items())}
    if per_locale:
        cands = [l for l in per_locale if L.primary(l) == "en" or l == "default"]
        res.base_locale = cands[0] if cands else max(per_locale, key=lambda l: len(per_locale[l]))
        base = per_locale[res.base_locale]
        for loc, d in per_locale.items():
            if loc == res.base_locale:
                continue
            shared = [k for k in base if k in d]
            identical = sum(1 for k in shared if d[k] and d[k] == base[k] and re.search(r"[^\W\d_]{3,}", d[k]))
            empty = sum(1 for k in shared if not str(d[k]).strip())
            res.coverage[loc] = {"coverage": round(len(shared) / len(base), 4) if base else 0.0,
                                 "keys": len(d), "identical_to_base": identical, "empty": empty}
    # library evidence
    for eco, libs in I18N_LIBS.items():
        for lib in libs:
            if lib in deps.get(eco, []):
                res.libs.append(f"{eco}:{lib}")
    # call patterns and loaders in first-party code
    code_files = [f for f in scan.included("template", "jsx", "script", "server") if f.category != "translation"]
    for f in code_files:
        text = texts.get(f.rel)
        if text is None:
            continue
        for rx, label in CALL_PATTERNS:
            for m in re.finditer(rx, text):
                if len(res.call_sites) < 400:
                    line = text.count("\n", 0, m.start()) + 1
                    res.call_sites.append({"path": f.rel, "line": line, "pattern": label})
                break
        for rx in LOADER_PATTERNS:
            m = re.search(rx, text)
            if m:
                res.loader_sites.append({"path": f.rel, "line": text.count("\n", 0, m.start()) + 1, "pattern": rx})
    # key references (sample up to 200 base keys, deterministic)
    if res.base_locale:
        base_keys = sorted({k for (ns, k) in per_locale[res.base_locale] if len(k) >= 3 and re.search(r"[A-Za-z]", k)})
        sample = base_keys[:: max(1, len(base_keys) // 200)][:200]
        res.keys_sampled = len(sample)
        big = "\n".join(texts.get(f.rel, "") for f in code_files)
        for k in sample:
            pat = re.compile(r"['\"`]" + re.escape(k) + r"['\"`]")
            m = pat.search(big)
            if m:
                res.keys_referenced += 1
                if len(res.key_examples) < 10:
                    res.key_examples.append({"key": k})
        # resource directory referenced from code (bundler import / loadPath)
        dirs = sorted({os.path.dirname(r.rel) for r in res.resources})
        for d in dirs:
            leaf = d.rsplit("/", 1)[-1] if d else ""
            if not leaf:
                continue
            for f in code_files:
                t = texts.get(f.rel, "")
                i = t.find(leaf + "/")
                if i >= 0:
                    res.path_references.append({"path": f.rel, "line": t.count("\n", 0, i) + 1, "dir": d})
                    break
    has_calls = bool(res.call_sites)
    needed = max(1, min(3, round(0.02 * res.keys_sampled)))
    if not res.resources:
        res.used, res.usage_reason = None, "no translation resources found"
    elif res.keys_referenced >= needed and has_calls:
        res.used, res.usage_reason = True, f"{res.keys_referenced}/{res.keys_sampled} sampled keys referenced; i18n calls present"
    elif has_calls and (res.loader_sites or res.path_references):
        res.used, res.usage_reason = True, "i18n calls present and resource directory/loader referenced (keys may be built dynamically)"
    elif any(r.rel.endswith(("strings.xml", ".strings", ".arb")) for r in res.resources) and has_calls:
        res.used, res.usage_reason = True, "platform string resources with platform lookup calls"
    elif res.keys_referenced >= needed:
        res.used, res.usage_reason = True, f"{res.keys_referenced} sampled keys referenced without recognised call pattern"
    else:
        lib_note = f"; i18n library declared ({', '.join(res.libs)}) but not used" if res.libs else ""
        res.used, res.usage_reason = False, (f"translation resources present but unused: {res.keys_referenced}/{res.keys_sampled} "
                                             f"sampled keys referenced, {len(res.call_sites)} i18n call sites{lib_note}")
    return res


def parse_dependencies(scan: ScanResult, texts: dict[str, str]) -> dict[str, list[str]]:
    deps: dict[str, set] = {"npm": set(), "python": set(), "ruby": set(), "php": set(), "dart": set()}
    for f in scan.included("manifest"):
        t = texts.get(f.rel) or ""
        name = f.rel.rsplit("/", 1)[-1]
        try:
            if name == "package.json":
                d = json.loads(t)
                for sec in ("dependencies", "devDependencies", "peerDependencies"):
                    deps["npm"].update((d.get(sec) or {}).keys())
            elif name in ("requirements.txt",):
                for line in t.splitlines():
                    m = re.match(r"\s*([A-Za-z0-9_.-]+)", line)
                    if m and not line.strip().startswith("#"):
                        deps["python"].add(m.group(1).lower())
            elif name in ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile"):
                deps["python"].update(x.lower() for x in re.findall(r"['\"]([A-Za-z0-9_.-]+)\s*(?:[<>=~!][^'\"]*)?['\"]", t))
            elif name == "Gemfile":
                deps["ruby"].update(re.findall(r"gem\s+['\"]([\w-]+)['\"]", t))
            elif name == "composer.json":
                d = json.loads(t)
                deps["php"].update((d.get("require") or {}).keys())
            elif name == "pubspec.yaml":
                d = yaml.safe_load(t) or {}
                deps["dart"].update((d.get("dependencies") or {}).keys())
        except Exception:
            continue
    return {k: sorted(v) for k, v in deps.items()}
