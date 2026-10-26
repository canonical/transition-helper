"""`th doctor`: environment checks."""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass
from typing import Callable

from .config import ConfigError, UserConfig, config_path
from .kb import KB
from .workspace import WorkspaceError, find_workspace

TOOLS = ["dpkg-source", "quilt", "git", "patch"]


@dataclass
class Check:
    name: str
    ok: bool | None  # None = skipped/warning
    detail: str


def run_checks(cfg: UserConfig | None, cfg_error: str | None, ping_llm: bool = True,
               lp_factory: Callable | None = None) -> list[Check]:
    out: list[Check] = []
    for tool in TOOLS:
        path = shutil.which(tool)
        out.append(Check(f"tool {tool}", bool(path), path or "not on PATH"))
    for mod in ("launchpadlib", "debian", "litellm", "yaml", "jsonschema"):
        out.append(Check(f"python module {mod}", importlib.util.find_spec(mod) is not None, ""))

    if cfg_error:
        out.append(Check("user config", False, cfg_error))
        return out
    assert cfg is not None
    out.append(Check("user config", True, str(cfg.path) if cfg.path else
                     f"{config_path()} missing; using defaults"))
    try:
        name, email = cfg.maintainer()
        out.append(Check("maintainer", True, f"{name} <{email}>"))
    except ConfigError as e:
        out.append(Check("maintainer", False, str(e)))

    ws = None
    try:
        ws = find_workspace()
        out.append(Check("workspace", True, f"{ws.root} ({ws.transition.name}, {ws.transition.series})"))
    except WorkspaceError as e:
        out.append(Check("workspace", None, str(e)))

    kb_dirs = [cfg.global_kb_path, ws.kb_dir if ws else None]
    if cfg.global_kb_path and not cfg.global_kb_path.is_dir():
        out.append(Check("global KB", False, f"{cfg.global_kb_path} does not exist"))
    kb = KB.load(kb_dirs)
    out.append(Check("knowledge base", not kb.errors,
                     f"{len(kb.signatures)} signatures" + (f"; errors: {'; '.join(kb.errors)}" if kb.errors else "")))

    # Launchpad
    creds = cfg.lp_credentials_file
    if creds:
        out.append(Check("LP credentials", creds.exists(), str(creds)))
    try:
        from .lp import LaunchpadClient

        cache = (ws.cache_dir if ws else config_path().parent / "cache")
        lp = lp_factory() if lp_factory else LaunchpadClient(cache, credentials_file=creds)
        series = ws.transition.series if ws else None
        if series:
            ok = lp.series_exists(series)
            out.append(Check("Launchpad", ok, f"series {series} {'found' if ok else 'NOT found'}"))
            if ws.transition.build_location == "ppa" and ws.transition.ppa:
                try:
                    info = lp.ppa_info(ws.transition.ppa)
                    priv = info.private
                    detail = f"{info.reference}{' (private)' if priv else ''}"
                    if priv and not cfg.allow_private_data:
                        detail += "; LLM analysis disabled (allow_private_data: false)"
                    out.append(Check("PPA", True, detail))
                except Exception as e:
                    out.append(Check("PPA", False, str(e)))
        else:
            out.append(Check("Launchpad", True, f"reachable; current series {lp.current_series()}"))
    except Exception as e:
        out.append(Check("Launchpad", False, str(e)))

    # LLM
    if not cfg.llm.model:
        out.append(Check("LLM", None, "llm.model not configured (triage works with --no-llm)"))
    elif cfg.llm.api_key_env and not cfg.llm.api_key():
        out.append(Check("LLM", False, f"{cfg.llm.api_key_env} is not set"))
    elif ping_llm:
        try:
            from .llm.client import LiteLLMClient

            reply = LiteLLMClient(cfg.llm, max_output_tokens=16).ping()
            out.append(Check("LLM", True, f"{cfg.llm.model} replied {reply[:40]!r}"))
        except Exception as e:
            out.append(Check("LLM", False, f"{cfg.llm.model}: {e}"))
    else:
        out.append(Check("LLM", None, f"{cfg.llm.model} (ping skipped)"))
    return out


def format_checks(checks: list[Check]) -> str:
    mark = {True: "ok  ", False: "FAIL", None: "warn"}
    return "\n".join(f"[{mark[c.ok]}] {c.name}: {c.detail}".rstrip(": ") for c in checks)
