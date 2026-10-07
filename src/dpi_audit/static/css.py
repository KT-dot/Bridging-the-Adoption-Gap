"""CSS analysis. Plain CSS is parsed with tinycss2; SCSS/LESS/Stylus fall back to a
brace-matching scanner (results from that path are graded heuristic)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import tinycss2

LOGICAL_PROPS = re.compile(r"^(margin|padding|border|inset)-(inline|block)(-start|-end)?|^(inset-inline|inset-block)$")
VISIBLE_FOCUS_PROPS = ("outline", "outline-style", "outline-width", "outline-color", "box-shadow",
                       "border", "border-color", "border-bottom", "background", "background-color",
                       "text-decoration", "text-decoration-line")


@dataclass
class Rule:
    selector: str
    decls: dict
    media: str
    file: str
    line: int
    parsed: bool


@dataclass
class CssSummary:
    rules: list[Rule] = field(default_factory=list)
    media_queries: list[tuple[str, str, int]] = field(default_factory=list)  # (prelude, file, line)
    files_parsed: int = 0
    files_heuristic: int = 0

    def focus_rules(self) -> list[Rule]:
        return [r for r in self.rules if ":focus" in r.selector]

    def media_matching(self, rx: str) -> list[tuple[str, str, int]]:
        c = re.compile(rx, re.I)
        return [m for m in self.media_queries if c.search(m[0])]


def _decls_from_tokens(content) -> dict:
    out = {}
    for d in tinycss2.parse_declaration_list(content, skip_comments=True, skip_whitespace=True):
        if d.type == "declaration":
            out[d.lower_name] = tinycss2.serialize(d.value).strip() + (" !important" if d.important else "")
    return out


def parse_css(text: str, rel: str, summary: CssSummary) -> None:
    if not rel.lower().endswith(".css") and not rel.lower().endswith(".pcss"):
        _parse_heuristic(text, rel, summary)
        summary.files_heuristic += 1
        return
    try:
        rules = tinycss2.parse_stylesheet(text, skip_comments=True, skip_whitespace=True)
    except Exception:
        _parse_heuristic(text, rel, summary)
        summary.files_heuristic += 1
        return
    summary.files_parsed += 1
    _walk(rules, "", rel, summary)


def _walk(rules, media: str, rel: str, summary: CssSummary) -> None:
    for r in rules:
        if r.type == "qualified-rule":
            sel = tinycss2.serialize(r.prelude).strip()
            summary.rules.append(Rule(sel, _decls_from_tokens(r.content), media, rel, r.source_line, True))
        elif r.type == "at-rule" and r.content is not None:
            prelude = tinycss2.serialize(r.prelude).strip()
            if r.lower_at_keyword in ("media", "supports", "layer", "container"):
                if r.lower_at_keyword == "media":
                    summary.media_queries.append((prelude, rel, r.source_line))
                inner = tinycss2.parse_rule_list(r.content, skip_comments=True, skip_whitespace=True)
                _walk(inner, (media + " and " if media else "") + prelude, rel, summary)


_BLOCK = re.compile(r"([^{};]+)\{([^{}]*)\}", re.S)


def _parse_heuristic(text: str, rel: str, summary: CssSummary) -> None:
    text_nc = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    text_nc = re.sub(r"(?m)//[^\n]*", "", text_nc)
    for m in re.finditer(r"@media([^{]+)\{", text_nc):
        summary.media_queries.append((m.group(1).strip(), rel, text_nc[: m.start()].count("\n") + 1))
    for m in _BLOCK.finditer(text_nc):
        sel = m.group(1).strip().split("\n")[-1].strip()
        decls = {}
        for part in m.group(2).split(";"):
            if ":" in part:
                k, _, v = part.partition(":")
                decls[k.strip().lower()] = v.strip()
        summary.rules.append(Rule(sel, decls, "", rel, text_nc[: m.start(1)].count("\n") + 1 + m.group(1).count("\n"), False))


def removes_outline(decls: dict) -> bool:
    for k in ("outline", "outline-style", "outline-width"):
        v = decls.get(k, "").replace("!important", "").strip().lower()
        if v in ("none", "0", "0px", "0 none", "none 0"):
            return True
    return False


def has_visible_focus_style(decls: dict) -> bool:
    for k in VISIBLE_FOCUS_PROPS:
        v = decls.get(k, "").replace("!important", "").strip().lower()
        if not v:
            continue
        if k.startswith("outline") and v in ("none", "0", "0px", "0 none", "none 0", "transparent"):
            continue
        if k == "box-shadow" and v == "none":
            continue
        return True
    return False


def logical_property_count(summary: CssSummary) -> int:
    return sum(1 for r in summary.rules for k in r.decls if LOGICAL_PROPS.match(k))


def rtl_selectors(summary: CssSummary) -> list[Rule]:
    rx = re.compile(r"\[dir\s*=\s*['\"]?rtl|:dir\(rtl\)|\.rtl\b", re.I)
    return [r for r in summary.rules if rx.search(r.selector)]
