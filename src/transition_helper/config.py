"""User configuration (~/.config/transition-helper/config.yaml)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


@dataclass
class LLMConfig:
    model: str = ""
    base_url: str | None = None
    api_key_env: str | None = None

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env)


@dataclass
class Budgets:
    max_iterations: int = 20
    max_tokens: int = 400_000
    max_tool_result_chars: int = 12_000


@dataclass
class UserConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    budgets: Budgets = field(default_factory=Budgets)
    global_kb_path: Path | None = None
    debfullname: str | None = None
    debemail: str | None = None
    # "anonymous" or a path to a launchpadlib credentials file
    lp_auth: str = "anonymous"
    allow_private_data: bool = False
    path: Path | None = None

    def maintainer(self) -> tuple[str, str]:
        name = self.debfullname or os.environ.get("DEBFULLNAME")
        email = self.debemail or os.environ.get("DEBEMAIL")
        if not name or not email:
            raise ConfigError(
                "maintainer identity unknown: set debfullname/debemail in "
                f"{config_path()} or DEBFULLNAME/DEBEMAIL in the environment"
            )
        return name, email

    def maintainer_or_placeholder(self) -> tuple[str, str]:
        try:
            return self.maintainer()
        except ConfigError:
            return "UNKNOWN", "unknown@example.invalid"

    @property
    def lp_credentials_file(self) -> Path | None:
        if self.lp_auth and self.lp_auth != "anonymous":
            return Path(self.lp_auth).expanduser()
        return None


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "transition-helper" / "config.yaml"


def _section(data: dict, key: str) -> dict:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"config: '{key}' must be a mapping")
    return value


def parse_config(data: dict, path: Path | None = None) -> UserConfig:
    if not isinstance(data, dict):
        raise ConfigError("config: top level must be a mapping")
    llm = _section(data, "llm")
    budgets = _section(data, "budgets")
    default_budgets = Budgets()
    try:
        b = Budgets(
            max_iterations=int(budgets.get("max_iterations", default_budgets.max_iterations)),
            max_tokens=int(budgets.get("max_tokens", default_budgets.max_tokens)),
            max_tool_result_chars=int(
                budgets.get("max_tool_result_chars", default_budgets.max_tool_result_chars)
            ),
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f"config: budgets must be integers ({e})") from e
    kb = data.get("global_kb_path")
    lp_auth = str(data.get("lp_auth") or "anonymous")
    return UserConfig(
        llm=LLMConfig(
            model=str(llm.get("model") or ""),
            base_url=llm.get("base_url"),
            api_key_env=llm.get("api_key_env"),
        ),
        budgets=b,
        global_kb_path=Path(kb).expanduser() if kb else None,
        debfullname=data.get("debfullname"),
        debemail=data.get("debemail"),
        lp_auth=lp_auth,
        allow_private_data=bool(data.get("allow_private_data", False)),
        path=path,
    )


def load_config(path: Path | None = None) -> UserConfig:
    path = path or config_path()
    if not path.exists():
        return UserConfig(path=None)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    return parse_config(data, path)


EXAMPLE_CONFIG = """\
llm:
  model: anthropic/claude-opus-5-5   # any LiteLLM model string
  base_url: null                     # optional, for proxies / self-hosted
  api_key_env: ANTHROPIC_API_KEY     # name of the env var holding the key
budgets:
  max_iterations: 20
  max_tokens: 400000
  max_tool_result_chars: 12000
global_kb_path: ~/src/transition-kb   # team-shared signature repo
debfullname: Your Name
debemail: you@ubuntu.com
lp_auth: anonymous                    # or a path to a launchpadlib credentials file
allow_private_data: false
"""
