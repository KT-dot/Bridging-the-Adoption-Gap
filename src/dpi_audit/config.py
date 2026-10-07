"""Configuration loading, CLI overrides and the semantic configuration hash."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from .util import deep_merge, repo_root, sha256_obj

DEFAULT_CONFIG_PATH = repo_root() / "config" / "default.yaml"


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if path:
        with open(path, encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
        if not isinstance(user, dict):
            raise ValueError(f"config {path}: top level must be a mapping")
        cfg = deep_merge(cfg, user)
    if overrides:
        cfg = deep_merge(cfg, overrides)
    validate_config(cfg)
    return cfg


def cli_overrides(mode=None, workers=None, max_pages=None, timeout=None) -> dict:
    o: dict[str, Any] = {}
    if mode:
        o.setdefault("run", {})["mode"] = mode
    if workers is not None:
        o.setdefault("run", {})["workers"] = int(workers)
    if max_pages is not None:
        o.setdefault("runtime", {})["max_pages"] = int(max_pages)
    if timeout is not None:
        o.setdefault("runtime", {})["navigation_timeout_s"] = float(timeout)
    return o


def validate_config(cfg: dict) -> None:
    mode = cfg["run"]["mode"]
    if mode not in ("static", "runtime", "hybrid"):
        raise ValueError(f"run.mode must be static|runtime|hybrid, got {mode!r}")
    if int(cfg["run"]["workers"]) < 1:
        raise ValueError("run.workers must be >= 1")
    if int(cfg["run"]["project_timeout_s"]) <= 0:
        raise ValueError("run.project_timeout_s must be >= 1 second")
    rt = cfg["runtime"]
    if int(rt["max_pages"]) < 1:
        raise ValueError("runtime.max_pages must be >= 1")
    if float(rt["per_host_min_delay_s"]) < 0:
        raise ValueError("runtime.per_host_min_delay_s must be >= 0")
    if not rt.get("respect_robots", True):
        # Permitted only so that local fixtures without robots.txt can be served; documented.
        pass
    for name in ("desktop", "mobile"):
        if name not in rt["viewports"]:
            raise ValueError(f"runtime.viewports.{name} is required")


def _drop_key(d: dict, dotted: str) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.get(p)
        if not isinstance(cur, dict):
            return
    cur.pop(parts[-1], None)


def semantic_config(cfg: dict) -> dict:
    """Config subset that can change audit results (used for resume invalidation)."""
    c = copy.deepcopy(cfg)
    for key in cfg.get("non_semantic_keys", []):
        _drop_key(c, key)
    c.pop("non_semantic_keys", None)
    return c


def config_hash(cfg: dict) -> str:
    return sha256_obj(semantic_config(cfg))
