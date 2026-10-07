"""Language utilities built on Babel/CLDR: tag validation, likely scripts, language-name
index for recognising switcher options, Unicode script shares and a small stopword proxy.

All language identification here is a documented PROXY: scripts are reliable, the
stopword check for Latin-script languages is coarse.
"""
from __future__ import annotations

import functools
import re
import unicodedata
from collections import Counter

from babel import Locale, UnknownLocaleError
from babel.core import get_global
from babel.localedata import locale_identifiers

RTL_LANGS = {"ar", "he", "iw", "fa", "ur", "ps", "sd", "ug", "yi", "dv", "ckb", "ks", "syr", "arc", "ku-arab", "pa-arab"}
# Unicode block-name prefix -> ISO 15924 code
SCRIPT_PREFIX = {
    "LATIN": "Latn", "DEVANAGARI": "Deva", "BENGALI": "Beng", "GURMUKHI": "Guru", "GUJARATI": "Gujr",
    "ORIYA": "Orya", "TAMIL": "Taml", "TELUGU": "Telu", "KANNADA": "Knda", "MALAYALAM": "Mlym",
    "SINHALA": "Sinh", "THAI": "Thai", "LAO": "Laoo", "TIBETAN": "Tibt", "MYANMAR": "Mymr",
    "KHMER": "Khmr", "ARABIC": "Arab", "HEBREW": "Hebr", "CYRILLIC": "Cyrl", "GREEK": "Grek",
    "ARMENIAN": "Armn", "GEORGIAN": "Geor", "ETHIOPIC": "Ethi", "HANGUL": "Hang", "HIRAGANA": "Jpan",
    "KATAKANA": "Jpan", "CJK": "Hani", "THAANA": "Thaa", "OL CHIKI": "Olck", "MEETEI MAYEK": "Mtei",
    "TIFINAGH": "Tfng", "SYRIAC": "Syrc", "MONGOLIAN": "Mong", "CHEROKEE": "Cher", "CANADIAN": "Cans",
}
STOPWORDS = {
    "en": {"the", "and", "of", "to", "in", "for", "is", "on", "with", "your", "you", "are", "this", "by"},
    "fr": {"le", "la", "les", "des", "et", "est", "pour", "dans", "une", "vous", "sur", "du", "au"},
    "es": {"el", "los", "las", "y", "es", "para", "por", "una", "con", "del", "su", "que", "en"},
    "pt": {"o", "os", "as", "e", "para", "com", "uma", "não", "do", "da", "dos", "seu", "que"},
    "de": {"der", "die", "und", "das", "ist", "nicht", "mit", "für", "auf", "den", "ein", "eine", "sie"},
    "id": {"dan", "yang", "di", "untuk", "dengan", "ini", "dari", "anda", "pada", "atau", "tidak"},
    "sw": {"na", "ya", "wa", "kwa", "ni", "za", "katika", "hii", "kwenye", "huduma"},
    "it": {"il", "di", "che", "per", "una", "con", "non", "sono", "gli", "della", "del"},
    "nl": {"de", "het", "een", "en", "van", "voor", "met", "niet", "zijn", "op", "uw"},
}


def normalize_tag(tag: str | None) -> str | None:
    if not tag:
        return None
    t = tag.strip().replace("_", "-")
    return t or None


def primary(tag: str | None) -> str | None:
    t = normalize_tag(tag)
    return t.split("-")[0].lower() if t else None


@functools.lru_cache(maxsize=4096)
def is_language_code(tag: str) -> bool:
    t = normalize_tag(tag)
    if not t or not re.match(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$", t):
        return False
    try:
        Locale.parse(t, sep="-")
        return True
    except (UnknownLocaleError, ValueError):
        try:
            Locale.parse(primary(t))
            return True
        except (UnknownLocaleError, ValueError):
            return False


@functools.lru_cache(maxsize=4096)
def likely_script(tag: str | None) -> str | None:
    t = normalize_tag(tag)
    if not t:
        return None
    parts = t.split("-")
    for p in parts[1:]:
        if len(p) == 4 and p.isalpha():
            return p.title()
    likely = get_global("likely_subtags")
    for key in (t.replace("-", "_").lower(), parts[0].lower()):
        v = likely.get(key)
        if v:
            for seg in v.split("_")[1:]:
                if len(seg) == 4:
                    return seg
    return None


def is_rtl(tag: str | None) -> bool:
    t = (normalize_tag(tag) or "").lower()
    if not t:
        return False
    if primary(t) in RTL_LANGS or t in RTL_LANGS:
        return True
    return likely_script(t) in ("Arab", "Hebr", "Thaa", "Syrc", "Nkoo", "Adlm")


def char_script(ch: str) -> str | None:
    if not ch.isalpha():
        return None
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    for prefix, code in SCRIPT_PREFIX.items():
        if name.startswith(prefix):
            return code
    return "Other"


def script_shares(text: str, max_chars: int = 20000) -> dict[str, float]:
    c: Counter = Counter()
    for ch in text[:max_chars]:
        s = char_script(ch)
        if s:
            c[s] += 1
    total = sum(c.values())
    return {k: round(v / total, 4) for k, v in c.most_common()} if total else {}


def dominant_script(text: str, threshold: float = 0.6) -> str | None:
    shares = script_shares(text)
    if not shares:
        return None
    top, share = next(iter(shares.items()))
    return top if share >= threshold else None


def stopword_guess(text: str) -> tuple[str | None, dict]:
    words = re.findall(r"[^\W\d_]+", text.lower())[:3000]
    if len(words) < 20:
        return None, {}
    scores = {lang: sum(1 for w in words if w in sw) / len(words) for lang, sw in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return (best if scores[best] >= 0.05 else None), {k: round(v, 3) for k, v in scores.items()}


def text_language_signals(text: str) -> dict:
    shares = script_shares(text)
    dom = dominant_script(text)
    guess, scores = stopword_guess(text) if dom == "Latn" else (None, {})
    return {"script_shares": shares, "dominant_script": dom, "latin_stopword_guess": guess}


def same_language(a: str | None, b: str | None) -> bool:
    return bool(a and b and primary(a) == primary(b))


def consistent_with_script(tag: str | None, text: str) -> tuple[bool | None, str]:
    """(consistent?, explanation) for declared tag vs dominant script of text."""
    if not tag:
        return None, "no declared language"
    exp = likely_script(tag)
    dom = dominant_script(text)
    if not exp or not dom:
        return None, f"expected script {exp or 'unknown'}, dominant script {dom or 'none (mixed/too little text)'}"
    if exp == dom or (exp == "Jpan" and dom in ("Hani", "Jpan")) or (exp == "Kore" and dom == "Hang"):
        return True, f"declared {tag} ({exp}) matches dominant script {dom}"
    return False, f"declared {tag} expects {exp} but dominant script of visible text is {dom}"


@functools.lru_cache(maxsize=1)
def language_name_index() -> dict[str, str]:
    """Lower-cased language names (endonyms, English and Hindi exonyms) -> primary code."""
    idx: dict[str, str] = {}
    for ident in locale_identifiers():
        if "_" in ident:
            continue
        try:
            loc = Locale.parse(ident)
        except Exception:
            continue
        for disp in (ident, "en", "hi"):
            try:
                name = loc.get_display_name(disp)
            except Exception:
                name = None
            if name and len(name) >= 2:
                idx.setdefault(name.lower(), ident)
                idx.setdefault(re.sub(r"\s*\(.*\)$", "", name.lower()), ident)
    for extra, code in {"english": "en", "hindi": "hi", "हिंदी": "hi", "हिन्दी": "hi", "eng": "en",
                        "español": "es", "français": "fr", "বাংলা": "bn", "اردو": "ur", "ଓଡ଼ିଆ": "or",
                        "ਪੰਜਾਬੀ": "pa", "অসমীয়া": "as", "मराठी": "mr", "ગુજરાતી": "gu", "தமிழ்": "ta",
                        "తెలుగు": "te", "ಕನ್ನಡ": "kn", "മലയാളം": "ml", "kiswahili": "sw",
                        "bahasa indonesia": "id", "bahasa melayu": "ms", "filipino": "fil", "नेपाली": "ne", "සිංහල": "si"}.items():
        idx.setdefault(extra, code)
    return idx


def recognise_language_label(label: str) -> str | None:
    """Return a primary language code if a UI label names a language (or is a code)."""
    s = re.sub(r"\s+", " ", (label or "").strip()).strip(" -|/·•")
    if not s or len(s) > 40:
        return None
    low = s.lower()
    idx = language_name_index()
    if low in idx:
        return idx[low]
    if re.fullmatch(r"[a-z]{2}([-_][a-z]{2})?", low) and is_language_code(low) and low not in {"go", "to", "do", "me", "us", "ok", "no", "on", "in", "at", "is", "it", "or", "an", "as", "be", "by", "he", "if", "my", "of", "so", "up", "we"}:
        return primary(low)
    if re.fullmatch(r"[A-Z]{2}", s) and is_language_code(low):
        return low
    return None
