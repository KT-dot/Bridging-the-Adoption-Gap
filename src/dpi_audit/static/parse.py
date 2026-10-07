"""Syntax-aware extraction of UI elements from templates and JSX/TSX.

* HTML-like templates (HTML, Vue/Svelte templates, Angular, Django/Jinja, ERB, PHP, ...)
  are parsed with BeautifulSoup's ``html.parser`` (lenient, keeps source line numbers).
  Framework bindings (``:alt``, ``v-bind:alt``, ``[alt]``, ``[attr.aria-label]``,
  ``alt={x}``) count as *dynamic* attributes: present, value unknown.
* JS/JSX/TSX are parsed with tree-sitter; every JSX element becomes an Element.

Template engines' own syntax is not executed. Elements produced inside loops or
conditionals are still single source elements; they are static indications, not proof
that the element is rendered.
"""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

try:  # tree-sitter grammars are pinned dependencies; degrade gracefully if missing
    import tree_sitter_javascript as _tsj
    import tree_sitter_typescript as _tst
    from tree_sitter import Language, Parser

    _LANGS = {"js": Language(_tsj.language()), "ts": Language(_tst.language_typescript()),
              "tsx": Language(_tst.language_tsx())}
except Exception:  # pragma: no cover
    _LANGS = {}

COMPONENT_ALIASES = {"image": "img", "img": "img", "link": "a", "navlink": "a", "routerlink": "a",
                     "router-link": "a", "nuxt-link": "a", "nuxtlink": "a", "a": "a"}


@dataclass
class Element:
    tag: str                       # lower-case tag (components lower-cased; alias in .kind)
    attrs: dict                    # name -> static string value, or None when dynamic/boolean
    dynamic: set = field(default_factory=set)   # attribute names bound to expressions
    text: str = ""                 # static direct text content (trimmed, truncated)
    file: str = ""
    line: int = 0
    end_line: int = 0
    syntax: str = "html"           # html | jsx
    has_spread: bool = False
    parent_tags: tuple = ()
    idx: int = -1                  # index within the file's element list
    parent_idx: int = -1           # index of the nearest parsed parent element (-1 = none)

    @property
    def kind(self) -> str:
        return COMPONENT_ALIASES.get(self.tag, self.tag)

    def has(self, name: str) -> bool:
        return name in self.attrs or name in self.dynamic

    def get(self, name: str):
        return self.attrs.get(name)


_BIND = re.compile(r"^(?::|v-bind:|\[attr\.|\[|bind:)?(?P<name>[A-Za-z_][\w:.-]*?)\]?$")


def _norm_attr(name: str) -> tuple[str, bool]:
    """Return (canonical attribute name, is_dynamic)."""
    n = name.strip()
    dyn = n.startswith((":", "v-bind:", "[", "bind:"))
    m = _BIND.match(n)
    base = m.group("name") if m else n
    base = base.lower()
    jsx_map = {"classname": "class", "htmlfor": "for", "tabindex": "tabindex"}
    return jsx_map.get(base, base), dyn


def extract_vue_template(text: str) -> tuple[str, int]:
    m = re.search(r"<template[^>]*>(.*)</template>", text, re.S | re.I)
    if not m:
        return "", 0
    return m.group(1), text[: m.start(1)].count("\n")


def strip_script_style(text: str) -> str:
    """Blank out <script>/<style> bodies while preserving line numbers."""
    def blank(mo):
        return re.sub(r"[^\n]", " ", mo.group(0))
    return re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", blank, text)


def parse_markup(text: str, rel: str, line_offset: int = 0) -> list[Element]:
    if rel.lower().endswith(".vue"):
        text, line_offset = extract_vue_template(text)
    elif rel.lower().endswith(".svelte"):
        text = strip_script_style(text)
    soup = BeautifulSoup(text, "html.parser")
    out: list[Element] = []
    pos: dict[int, int] = {}
    for tag in soup.find_all(True):
        attrs: dict = {}
        dyn: set = set()
        for k, v in tag.attrs.items():
            if k.startswith(("@", "v-on:", "(", "on:")):
                ev = re.sub(r"^(@|v-on:|\(|on:)", "", k).rstrip(")").split(".")[0].lower()
                dyn.add("on" + ev)
                continue
            name, is_dyn = _norm_attr(k)
            if is_dyn:
                dyn.add(name)
                continue
            if isinstance(v, list):
                v = " ".join(v)
            if v is not None and ("{{" in v or "{%" in v or "<?" in v or "<%" in v or re.search(r"\{[^}]*\}", v) and rel.endswith(".svelte")):
                dyn.add(name)
                continue
            attrs[name] = v
        direct = "".join(s for s in tag.find_all(string=True, recursive=False))
        direct = re.sub(r"\s+", " ", direct).strip()
        line = (getattr(tag, "sourceline", None) or 1) + line_offset
        parents = tuple(p.name for p in tag.parents if getattr(p, "name", None) and p.name != "[document]")[:6]
        par = tag.parent
        pidx = pos.get(id(par), -1) if par is not None else -1
        pos[id(tag)] = len(out)
        out.append(Element(tag=tag.name.lower(), attrs=attrs, dynamic=dyn, text=direct[:300], file=rel,
                           line=line, end_line=line + str(tag).count("\n"), syntax="html",
                           parent_tags=parents, idx=len(out), parent_idx=pidx))
    return out


def _ts_lang_for(rel: str) -> str | None:
    low = rel.lower()
    if low.endswith(".tsx"):
        return "tsx"
    if low.endswith((".ts", ".mts", ".cts")):
        return "ts"
    if low.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "js"
    return None


def parse_jsx(text: str, rel: str) -> list[Element]:
    lang = _ts_lang_for(rel)
    if not lang or lang not in _LANGS or "<" not in text:
        return []
    parser = Parser(_LANGS[lang])
    src = text.encode("utf-8")
    tree = parser.parse(src)
    out: list[Element] = []

    def node_text(n) -> str:
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    stack: list[int] = []

    def record(n, el_node, parents: tuple) -> str:
        name_node = n.child_by_field_name("name")
        name = node_text(name_node) if name_node else "?"
        attrs: dict = {}
        dyn: set = set()
        spread = False
        for c in n.children:
            if c.type == "jsx_attribute":
                parts = [x for x in c.children if x.type != "="]
                if not parts:
                    continue
                aname, _ = _norm_attr(node_text(parts[0]))
                if len(parts) == 1:
                    attrs[aname] = None  # boolean attribute
                else:
                    val = parts[-1]
                    if val.type == "string":
                        attrs[aname] = node_text(val)[1:-1]
                    else:
                        inner = node_text(val)
                        m = re.fullmatch(r"\{\s*(['\"`])(.*)\1\s*\}", inner, re.S)
                        if m and "${" not in m.group(2):
                            attrs[aname] = m.group(2)
                        else:
                            dyn.add(aname)
            elif c.type == "jsx_expression" and node_text(c).lstrip("{ ").startswith("..."):
                spread = True
        text_parts = []
        if el_node is not n:
            for c in el_node.children:
                if c.type == "jsx_text":
                    text_parts.append(node_text(c))
        direct = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
        tag = name.split(".")[-1].lower()
        out.append(Element(tag=tag, attrs=attrs, dynamic=dyn, text=direct[:300], file=rel,
                           line=el_node.start_point[0] + 1, end_line=el_node.end_point[0] + 1,
                           syntax="jsx", has_spread=spread, parent_tags=parents[:6],
                           idx=len(out), parent_idx=stack[-1] if stack else -1))
        return tag

    def visit(n, parents: tuple):
        if n.type == "jsx_element":
            opening = next((c for c in n.children if c.type == "jsx_opening_element"), None)
            tag = record(opening, n, parents) if opening is not None else "?"
            stack.append(len(out) - 1)
            for c in n.children:
                if c is not opening and c.type != "jsx_closing_element":
                    visit(c, (tag,) + parents)
            stack.pop()
            return
        if n.type == "jsx_self_closing_element":
            record(n, n, parents)
            for c in n.children:  # attribute expressions may contain JSX
                visit(c, parents)
            return
        for c in n.children:
            visit(c, parents)

    try:
        visit(tree.root_node, ())
    except RecursionError:  # pathological nesting
        pass
    return out


def js_object_strings(text: str, rel: str) -> dict[str, str]:
    """Flatten the first object literal of a JS/TS translation module to {dotted.key: value}."""
    lang = _ts_lang_for(rel)
    if not lang or lang not in _LANGS:
        return {}
    parser = Parser(_LANGS[lang])
    src = text.encode("utf-8")
    tree = parser.parse(src)

    def node_text(n) -> str:
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    def find_obj(n):
        if n.type == "object":
            return n
        for c in n.children:
            r = find_obj(c)
            if r is not None:
                return r
        return None

    out: dict[str, str] = {}

    def walk(obj, prefix: str):
        for pair in obj.children:
            if pair.type != "pair":
                continue
            k = pair.child_by_field_name("key")
            v = pair.child_by_field_name("value")
            if k is None or v is None:
                continue
            key = node_text(k).strip("'\"`")
            full = f"{prefix}.{key}" if prefix else key
            if v.type == "object":
                walk(v, full)
            elif v.type in ("string", "template_string"):
                out[full] = node_text(v)[1:-1]

    root = find_obj(tree.root_node)
    if root is not None:
        walk(root, "")
    return out
