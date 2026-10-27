"""`th config init`: interactive creation (or update) of the user config."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Mapping

from .config import (Budgets, ConfigError, LLMConfig, coerce, get_raw, parse_config, read_raw,
                     render_config, set_raw, unset_raw, validate_raw, write_text)
from .init_wizard import Asker

DEFAULT_MODEL = "anthropic/claude-opus-5-5"
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
# LiteLLM provider prefix -> the env var it reads the key from by default
KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "xai": "XAI_API_KEY",
    "azure": "AZURE_API_KEY",
}
CLEAR = "-"
_FROM_DATA = object()

Check = Callable[[str], str | None]  # returns an error message, or None if the value is fine


def guess_key_env(model: str) -> str | None:
    provider = model.split("/", 1)[0] if "/" in model else ""
    if not provider and model.startswith("claude"):
        provider = "anthropic"
    return KEY_ENVS.get(provider)


def default_ping(llm: LLMConfig) -> str:
    from .llm.client import LiteLLMClient

    return LiteLLMClient(llm, max_output_tokens=16).ping()


class _Wizard:
    def __init__(self, data: dict, a: Asker, env: Mapping[str, str],
                 ping: Callable[[LLMConfig], str] | None):
        self.data, self.a, self.out, self.env, self.ping = data, a, a.out, env, ping

    def optional(self, key: str, label: str, default: str | None | object = _FROM_DATA,
                 check: Check | None = None) -> str | None:
        """Ask for an optional string; empty keeps the default (the current value unless
        given), '-' clears it."""
        current = get_raw(self.data, key) if default is _FROM_DATA else default
        hint = f" [{current}] ('{CLEAR}' to clear)" if current else ""
        while True:
            value = self.a.ask_fn(f"{label}{hint}: ").strip()
            if value == CLEAR:
                value = ""
            elif not value:
                value = current or ""
            problem = check(value) if value and check else None
            if not problem:
                break
            self.out(f"  {problem}")
        if value:
            set_raw(self.data, key, value)
        else:
            unset_raw(self.data, key)
        return value or None

    def integer(self, key: str, label: str, default: int, builtin: int) -> None:
        """Ask for an integer; a value equal to the built-in default is not stored."""
        while True:
            value = self.a.ask_fn(f"{label} [{default}]: ").strip() or str(default)
            try:
                n = coerce(key, value)
                break
            except ConfigError as e:
                self.out(f"  {e}")
        if n == builtin:
            unset_raw(self.data, key)
        else:
            set_raw(self.data, key, n)

    def soft_check(self, exists: Callable[[Path], bool], what: str) -> Check:
        def check(value: str) -> str | None:
            path = Path(value).expanduser()
            if exists(path) or self.a.yes(f"  {path} is not {what}. Use it anyway?"):
                return None
            return "enter another path"
        return check

    # --- sections ---

    def llm(self) -> bool:
        self.out("\n== LLM ==")
        self.out("The rules work without one; the LLM reads logs, finds the root cause and "
                 "proposes patches.")
        if not self.a.yes("Use an LLM for the AI analysis?", default=True):
            self.data.pop("llm", None)
            return False
        while True:
            self.llm_settings()
            if self.llm_works():
                return True
            if not self.a.yes("Change the LLM settings?", default=True):
                return True

    def llm_settings(self) -> None:
        old_model = get_raw(self.data, "llm.model") or ""
        model = self.a.text("Model (any LiteLLM model string)", old_model or DEFAULT_MODEL)
        set_raw(self.data, "llm.model", model)
        cur_env = get_raw(self.data, "llm.api_key_env")
        key_env = cur_env if model == old_model and cur_env else guess_key_env(model) or cur_env
        key_env = self.optional(
            "llm.api_key_env", "Name of the env var holding the API key (not the key itself)",
            key_env, lambda v: None if ENV_RE.match(v) else "not a valid variable name")
        if key_env and not self.env.get(key_env):
            self.out(f"  note: {key_env} is not set in this shell; export it before running th")
        self.optional(
            "llm.base_url", "Custom endpoint URL (proxy / self-hosted; empty for the provider's)",
            check=lambda v: None if re.match(r"https?://", v) else "must start with http:// or https://")

    def llm_works(self) -> bool:
        """Optionally ping the model. False only if the ping was tried and failed."""
        llm = parse_config(self.data).llm
        if not self.ping or (llm.api_key_env and not self.env.get(llm.api_key_env)):
            return True
        if not self.a.yes("Send a test request to check the model works?", default=True):
            return True
        try:
            reply = self.ping(llm)
        except Exception as e:
            first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            self.out(f"  failed: {first[:300]}")
            return False
        self.out(f"  ok: {llm.model} replied {reply[:40]!r}")
        return True

    def budgets(self) -> None:
        b = parse_config(self.data).budgets
        if not self.a.yes(f"Change the per-triage budgets ({b.max_iterations} LLM turns, "
                          f"{b.max_tokens} tokens, {b.max_tool_result_chars} chars per tool "
                          "result)?"):
            return
        builtin = Budgets()
        for name, label in (("max_iterations", "LLM turns per triage"),
                            ("max_tokens", "Total tokens per triage"),
                            ("max_tool_result_chars",
                             "Max chars per tool result (longer ones are truncated)")):
            self.integer(f"budgets.{name}", label, getattr(b, name), getattr(builtin, name))

    def private_data(self) -> None:
        current = get_raw(self.data, "allow_private_data") is True
        if self.a.yes("Allow logs and sources from private PPAs to be sent to the LLM provider?",
                      default=current):
            set_raw(self.data, "allow_private_data", True)
        else:
            unset_raw(self.data, "allow_private_data")

    def maintainer(self) -> None:
        self.out("\n== Maintainer ==")
        self.out("Used for debian/changelog stanzas in proposed patches.")
        self.optional("debfullname", "Full name",
                      get_raw(self.data, "debfullname") or self.env.get("DEBFULLNAME"))
        self.optional("debemail", "Email",
                      get_raw(self.data, "debemail") or self.env.get("DEBEMAIL"),
                      lambda v: None if EMAIL_RE.match(v) else "not an email address")

    def launchpad_and_kb(self) -> None:
        self.out("\n== Launchpad and knowledge base ==")
        lp_auth = get_raw(self.data, "lp_auth")
        self.optional("lp_auth",
                      "launchpadlib credentials file, for reading private PPAs "
                      "(empty for anonymous access)",
                      lp_auth if lp_auth and lp_auth != "anonymous" else None,
                      self.soft_check(Path.is_file, "a file"))
        self.optional("global_kb_path",
                      "Path to the team-shared KB signature repo (a git checkout; empty for none)",
                      check=self.soft_check(Path.is_dir, "a directory"))


def _existing(path: Path, out: Callable[[str], None]) -> tuple[str, dict]:
    try:
        text, data = read_raw(path)
        validate_raw(data, path)
        return text, data
    except ConfigError as e:
        out(f"warning: ignoring the current config, it is invalid: {e}")
        return path.read_text() if path.exists() else "", {}


def run_config_wizard(path: Path, ask: Callable[[str], str] = input,
                      out: Callable[[str], None] = print,
                      ping: Callable[[LLMConfig], str] | None = default_ping,
                      env: Mapping[str, str] = os.environ) -> bool:
    """Ask for every user config setting and write `path`. True if it was written."""
    old_text, data = _existing(path, out)
    a = Asker(ask, out)
    if path.exists():
        out(f"Updating {path}. Current values are the defaults: press Enter to keep one, "
            f"'{CLEAR}' to clear it.")
    else:
        out(f"Creating {path}. Press Enter to accept a [default].")

    w = _Wizard(data, a, env, ping)
    if w.llm():
        w.budgets()
        w.private_data()
    w.maintainer()
    w.launchpad_and_kb()

    validate_raw(data, path)
    text = render_config(data)
    out(f"\n== {path} ==")
    out(text.rstrip("\n") or "# (empty: all defaults)")
    if path.exists() and text == old_text:
        out("no changes")
        return False
    if not a.yes(f"Write {path}?", default=True):
        out("not saved")
        return False
    if old_text:
        backup = path.with_name(path.name + ".bak")
        write_text(backup, old_text)
        out(f"previous version saved as {backup}")
    write_text(path, text)
    out(f"saved {path}; run 'th doctor' to check the setup")
    return True
