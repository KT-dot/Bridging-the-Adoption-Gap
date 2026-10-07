"""Parse and validate list.txt (see docs/input_format.md).

Syntax (UTF-8; a BOM is tolerated):

* blank lines are ignored; a line whose first non-space character is ``#`` is a comment
  (inline comments are NOT supported because URLs may contain ``#``);
* simple entry: one repository URL, deployed website URL, or local repository directory;
* rich entry: ``project_id | repository_url_or_path | website_url | interface_type`` with
  any optional field left empty (trailing fields may be omitted).

Several rich lines with the same project_id are merged into one project with several
interfaces (e.g. a service UI plus a separate documentation site). Exact duplicate lines
are reported and ignored. Nothing is guessed: a value that cannot be classified is an error.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .util import sha256_text, short_hash, slugify

INTERFACE_TYPES = {
    "service_ui": "Transactional or interactive service interface (the product UI people use)",
    "public_information_site": "Government/public information website that is itself the deployed interface",
    "data_portal": "Open-data or statistics portal interface",
    "informational_site": "The software project's own informational/marketing website (not a deployment)",
    "documentation_site": "Project documentation site",
    "api_only": "API without a web user interface",
    "backend_only": "Backend service or library without its own user interface",
    "library": "Reusable software library/component",
    "cli": "Command-line tool",
    "mobile_app": "Native mobile application (not web)",
    "desktop_app": "Native desktop application (not web)",
    "other_non_web": "Other non-web interface",
    "unknown": "Not supplied",
}
DEPLOYED_UI_TYPES = {"service_ui", "public_information_site", "data_portal"}
PROJECT_INFO_TYPES = {"informational_site", "documentation_site"}
NON_WEB_TYPES = {"api_only", "backend_only", "library", "cli", "mobile_app", "desktop_app", "other_non_web"}
WEB_TYPES = DEPLOYED_UI_TYPES | PROJECT_INFO_TYPES | {"unknown"}

REPO_HOSTS = {
    "github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "gitee.com", "git.sr.ht",
    "salsa.debian.org", "framagit.org", "code.europa.eu", "invent.kde.org", "gitlab.gnome.org",
    "openforge.gov.in", "sourceforge.net",
}
PROJECT_ID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SCP_RX = re.compile(r"^(?P<user>[\w.-]+)@(?P<host>[\w.-]+):(?P<path>[\w./~-]+?)(\.git)?/?$")


class InputError(ValueError):
    pass


@dataclass
class Location:
    raw: str
    kind: str                     # repository | website | local_path
    url: str | None = None        # normalized URL (repository/website)
    path: str | None = None       # absolute path (local_path)
    host: str | None = None
    forge: str | None = None
    owner: str | None = None
    name: str | None = None
    ref: str | None = None
    subdir: str | None = None

    @property
    def key(self) -> str:
        if self.kind == "local_path":
            return "path:" + (self.path or "")
        u = self.url or ""
        if self.kind == "repository":
            return "repo:" + u.lower()
        return "web:" + u

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None} | {"key": self.key}


@dataclass
class Interface:
    interface_id: str
    kind: str                     # source | web
    interface_type: str
    location: Location
    lines: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"interface_id": self.interface_id, "kind": self.kind,
                "interface_type": self.interface_type, "location": self.location.to_dict(),
                "lines": self.lines}


@dataclass
class Project:
    project_id: str
    id_source: str                # supplied | generated
    interfaces: list[Interface] = field(default_factory=list)
    declared_types: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def sources(self) -> list[Interface]:
        return [i for i in self.interfaces if i.kind == "source"]

    @property
    def webs(self) -> list[Interface]:
        return [i for i in self.interfaces if i.kind == "web"]

    def entry_fingerprint(self) -> str:
        return sha256_text("\n".join(sorted(i.location.key + "|" + i.interface_type for i in self.interfaces))
                           + "\n" + ",".join(sorted(self.declared_types)))

    def to_dict(self) -> dict:
        return {"project_id": self.project_id, "id_source": self.id_source,
                "interfaces": [i.to_dict() for i in self.interfaces],
                "declared_types": self.declared_types, "lines": self.lines,
                "warnings": self.warnings}


@dataclass
class ParseResult:
    projects: list[Project]
    errors: list[dict]
    warnings: list[dict]
    duplicates: list[dict]
    n_lines: int
    n_entries: int
    input_sha256: str

    @property
    def ok(self) -> bool:
        return not self.errors


def _normalize_web_url(raw: str, drop_params: tuple[str, ...] = ()) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(raw.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        raise InputError(f"URL has no host: {raw!r}")
    port = parts.port
    netloc = host if port is None or (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = parts.query
    if drop_params and query:
        q = [(k, v) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True) if k not in drop_params]
        query = urllib.parse.urlencode(q)
    return urllib.parse.urlunsplit((scheme, netloc, path, query, "")), host


def classify_location(raw: str, base_dir: Path | None = None) -> Location:
    """Classify one field as repository URL, website URL, or local directory."""
    s = raw.strip()
    if not s:
        raise InputError("empty location")
    if any(c.isspace() for c in s):
        raise InputError(f"whitespace inside location {s!r} (use '|' to separate fields)")
    if s.startswith("file://"):
        return _local(urllib.parse.unquote(urllib.parse.urlsplit(s).path), raw, base_dir)
    m = SCP_RX.match(s)
    if m and not s.startswith(("http://", "https://")):
        host = m.group("host").lower()
        if host in REPO_HOSTS:
            s = f"https://{host}/{m.group('path')}"
        else:
            raise InputError(f"SSH repository URL {s!r}: only https URLs are cloned (no credentials are used)")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s):
        parts = urllib.parse.urlsplit(s)
        if parts.scheme.lower() not in ("http", "https"):
            raise InputError(f"unsupported URL scheme in {s!r}")
        host = (parts.hostname or "").lower()
        path = parts.path
        is_repo = (host in REPO_HOSTS) or path.rstrip("/").endswith(".git")
        if host.endswith(".github.io") or host.endswith(".gitlab.io") or host.endswith(".pages.dev"):
            is_repo = False
        if is_repo:
            return _repo(s, host, path, raw)
        url, host = _normalize_web_url(s)
        return Location(raw=raw, kind="website", url=url, host=host)
    if s.startswith(("/", "./", "../", "~")) or (base_dir and (base_dir / s).exists()):
        return _local(s, raw, base_dir)
    if re.match(r"^[\w-]+(\.[\w-]+)+(/.*)?$", s):
        raise InputError(f"{s!r} looks like a domain without http(s):// — add the scheme explicitly (nothing is guessed)")
    raise InputError(f"cannot classify {s!r} as a repository URL, website URL, or existing local directory")


def _local(p: str, raw: str, base_dir: Path | None) -> Location:
    path = Path(os.path.expanduser(p))
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    path = path.resolve()
    if not path.is_dir():
        raise InputError(f"local repository path does not exist or is not a directory: {p!r}")
    return Location(raw=raw, kind="local_path", path=str(path), name=path.name)


def _repo(s: str, host: str, path: str, raw: str) -> Location:
    segs = [x for x in path.split("/") if x]
    ref = subdir = None
    if host == "github.com" or host == "bitbucket.org" or host == "codeberg.org" or host == "gitee.com":
        if len(segs) < 2:
            raise InputError(f"{s!r} is an organisation/user page, not a repository URL")
        owner, name = segs[0], segs[1]
        if name.endswith(".git"):
            name = name[:-4]
        if len(segs) >= 4 and segs[2] in ("tree", "blob", "src"):
            ref = segs[3]
            subdir = "/".join(segs[4:]) or None
        url = f"https://{host}/{owner}/{name}"
    else:
        if "-" in segs:  # GitLab-style /-/tree/<ref>/<subdir>
            i = segs.index("-")
            repo_segs, rest = segs[:i], segs[i + 1:]
            if len(rest) >= 2 and rest[0] in ("tree", "blob"):
                ref = rest[1]
                subdir = "/".join(rest[2:]) or None
        else:
            repo_segs = segs
        if not repo_segs:
            raise InputError(f"{s!r} has no repository path")
        if repo_segs[-1].endswith(".git"):
            repo_segs[-1] = repo_segs[-1][:-4]
        if len(repo_segs) < 2 and host in REPO_HOSTS:
            raise InputError(f"{s!r} is a group/user page, not a repository URL")
        owner = "/".join(repo_segs[:-1]) or None
        name = repo_segs[-1]
        url = f"https://{host}/{'/'.join(repo_segs)}"
    return Location(raw=raw, kind="repository", url=url, host=host,
                    forge=host if host in REPO_HOSTS else "git", owner=owner, name=name,
                    ref=ref, subdir=subdir)


def normalize_interface_type(raw: str) -> str:
    t = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if not t:
        return "unknown"
    if t not in INTERFACE_TYPES:
        raise InputError(f"unknown interface_type {raw!r}; allowed: {', '.join(INTERFACE_TYPES)}")
    return t


def _auto_id(loc: Location) -> str:
    if loc.kind == "repository":
        base = f"{loc.owner.split('/')[-1]}-{loc.name}" if loc.owner else loc.name
    elif loc.kind == "local_path":
        base = loc.name or "local"
    else:
        parts = urllib.parse.urlsplit(loc.url)
        host = (parts.hostname or "").removeprefix("www.")
        base = host + ("-" + parts.path.strip("/") if parts.path.strip("/") else "")
    return slugify(base, 56)


def _interface_id(project_id: str, kind: str, loc: Location) -> str:
    if kind == "source":
        label = slugify(loc.name or "repo", 24)
        return f"{project_id}::src-{label}-{short_hash(loc.key, 6)}"
    host = slugify((loc.host or "site").removeprefix("www."), 28)
    return f"{project_id}::web-{host}-{short_hash(loc.key, 6)}"


def parse_list(path: str | Path) -> ParseResult:
    p = Path(path)
    raw_bytes = p.read_bytes()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(f"{p}: not valid UTF-8 ({exc})") from exc
    return parse_text(text, base_dir=p.resolve().parent)


def parse_text(text: str, base_dir: Path | None = None) -> ParseResult:
    errors: list[dict] = []
    warnings: list[dict] = []
    duplicates: list[dict] = []
    entries: list[dict] = []
    lines = text.splitlines()
    seen_lines: dict[str, int] = {}
    for no, line in enumerate(lines, start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        norm_line = re.sub(r"\s*\|\s*", "|", s)
        if norm_line in seen_lines:
            duplicates.append({"line": no, "duplicate_of_line": seen_lines[norm_line], "text": s})
            continue
        seen_lines[norm_line] = no
        try:
            entries.append(_parse_entry(s, no, base_dir))
        except InputError as exc:
            errors.append({"line": no, "text": s, "error": str(exc)})

    # Generate IDs: collisions between generated slugs (or with supplied IDs) get hash suffixes
    # for every colliding generated entry, so the result does not depend on line order.
    supplied = {e["project_id"].lower() for e in entries if e["project_id"]}
    gen: dict[str, set[str]] = {}
    for e in entries:
        if not e["project_id"]:
            e["auto_id"] = _auto_id(e["primary"])
            gen.setdefault(e["auto_id"], set()).add(e["primary"].key)
    for e in entries:
        if not e["project_id"]:
            aid = e["auto_id"]
            if len(gen[aid]) > 1 or aid.lower() in supplied:
                aid = f"{aid}-{short_hash(e['primary'].key, 6)}"
            e["project_id"], e["id_source"] = aid, "generated"
        else:
            e["id_source"] = "supplied"

    projects: dict[str, Project] = {}
    case_map: dict[str, str] = {}
    for e in entries:
        pid = e["project_id"]
        low = pid.lower()
        if low in case_map and case_map[low] != pid:
            errors.append({"line": e["line"], "text": e["text"],
                           "error": f"project_id {pid!r} differs only in letter case from {case_map[low]!r}"})
            continue
        case_map[low] = pid
        proj = projects.get(pid)
        if proj is None:
            proj = projects[pid] = Project(project_id=pid, id_source=e["id_source"])
        elif e["id_source"] == "generated":
            duplicates.append({"line": e["line"], "duplicate_of_project": pid, "text": e["text"],
                               "note": "same location supplied again without a project_id"})
            continue
        proj.lines.append(e["line"])
        proj.raw_lines.append(e["text"])
        if e["declared_type"] and e["declared_type"] not in proj.declared_types:
            proj.declared_types.append(e["declared_type"])
        for kind, loc, itype in e["interfaces"]:
            existing = next((i for i in proj.interfaces if i.location.key == loc.key and i.kind == kind), None)
            if existing:
                if existing.interface_type != itype and itype != "unknown":
                    if existing.interface_type == "unknown":
                        existing.interface_type = itype
                    else:
                        proj.warnings.append(
                            f"line {e['line']}: {loc.raw} already declared as {existing.interface_type}; "
                            f"conflicting type {itype} ignored")
                existing.lines.append(e["line"])
                continue
            proj.interfaces.append(Interface(_interface_id(pid, kind, loc), kind, itype, loc, [e["line"]]))

    # Same location under different project IDs: keep both (labels are preserved), but flag.
    loc_owner: dict[str, list[str]] = {}
    for proj in projects.values():
        for i in proj.interfaces:
            loc_owner.setdefault(i.location.key, []).append(proj.project_id)
    for key, owners in loc_owner.items():
        if len(set(owners)) > 1:
            for pid in sorted(set(owners)):
                others = sorted(set(owners) - {pid})
                projects[pid].warnings.append(f"possible duplicate project: {key} also listed under {', '.join(others)}")
            warnings.append({"warning": "shared_location", "location": key, "projects": sorted(set(owners))})

    for proj in projects.values():
        nonweb = [t for t in proj.declared_types if t in NON_WEB_TYPES]
        if nonweb and proj.webs:
            proj.warnings.append(
                f"declared non-web type(s) {nonweb} for the project but website(s) supplied; "
                "websites are inventoried with that type and browser UI checks are not applied")
    return ParseResult(projects=list(projects.values()), errors=errors, warnings=warnings,
                       duplicates=duplicates, n_lines=len(lines), n_entries=len(entries),
                       input_sha256=sha256_text(text))


def _parse_entry(s: str, no: int, base_dir: Path | None) -> dict:
    if "|" in s:
        fields = [f.strip() for f in s.split("|")]
        if len(fields) > 4:
            raise InputError(f"rich entry has {len(fields)} fields; expected at most 4 "
                             "(project_id | repository_url_or_path | website_url | interface_type)")
        fields += [""] * (4 - len(fields))
        pid, repo_raw, web_raw, type_raw = fields
        if pid and not PROJECT_ID_RX.match(pid):
            raise InputError(f"invalid project_id {pid!r}: use letters, digits, '.', '_' or '-' (max 64, start alphanumeric)")
        itype = normalize_interface_type(type_raw)
        interfaces = []
        if repo_raw:
            loc = classify_location(repo_raw, base_dir)
            if loc.kind == "website":
                raise InputError(f"field 2 must be a repository URL or local path; {repo_raw!r} is a website URL")
            interfaces.append(("source", loc, "source_repository"))
        if web_raw:
            loc = classify_location(web_raw, base_dir)
            if loc.kind != "website":
                raise InputError(f"field 3 must be a website URL; {web_raw!r} is a {loc.kind.replace('_', ' ')}")
            interfaces.append(("web", loc, itype))
        if not interfaces:
            raise InputError("rich entry needs a repository/path (field 2) or a website URL (field 3)")
        primary = interfaces[0][1]
        return {"line": no, "text": s, "project_id": pid, "interfaces": interfaces,
                "declared_type": itype if itype != "unknown" else "", "primary": primary}
    loc = classify_location(s, base_dir)
    if loc.kind == "website":
        interfaces = [("web", loc, "unknown")]
    else:
        interfaces = [("source", loc, "source_repository")]
    return {"line": no, "text": s, "project_id": "", "interfaces": interfaces, "declared_type": "",
            "primary": loc}


def project_ui_applicability(project: Project) -> tuple[str, str]:
    """Return (status, explanation) for whether browser UI checks apply to the project."""
    nonweb = [t for t in project.declared_types if t in NON_WEB_TYPES]
    web_ui = [i for i in project.webs if i.interface_type not in NON_WEB_TYPES]
    if nonweb and not web_ui:
        return "not_applicable", f"declared interface_type {', '.join(nonweb)}: no web user interface to test in a browser"
    if nonweb and web_ui:
        return "mixed", f"declared {', '.join(nonweb)}; web interface(s) also supplied and tested as declared"
    if project.webs:
        return "applicable", "web interface supplied"
    return "unknown", "no website supplied; web UI applicability is determined only from static source evidence"
