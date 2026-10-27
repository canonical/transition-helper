"""User configuration (~/.config/transition-helper/config.yaml)."""

from __future__ import annotations

import copy
import difflib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

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
    for name in ("max_iterations", "max_tokens", "max_tool_result_chars"):
        if getattr(b, name) <= 0:
            raise ConfigError(f"config: budgets.{name} must be positive")
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


def read_raw(path: Path) -> tuple[str, dict]:
    """The config file's text and parsed (unvalidated) data; ("", {}) if missing."""
    if not path.exists():
        return "", {}
    text = path.read_text()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return text, data


def load_config(path: Path | None = None, overrides: Iterable[str] = ()) -> UserConfig:
    """Load the user config; `overrides` are "key=value" strings (`th -c`)."""
    path = path or config_path()
    _, data = read_raw(path)
    data = apply_overrides(data, overrides)
    return parse_config(data, path if path.exists() else None)


# --- keys and editing (`th config`) ---

@dataclass(frozen=True)
class ConfigKey:
    kind: str  # "str" | "int" | "bool" | "path"
    help: str


KEYS: dict[str, ConfigKey] = {
    "llm.model": ConfigKey("str", "any LiteLLM model string, e.g. anthropic/claude-opus-5-5"),
    "llm.base_url": ConfigKey("str", "optional endpoint (proxy / self-hosted)"),
    "llm.api_key_env": ConfigKey("str", "name of the env var holding the API key"),
    "budgets.max_iterations": ConfigKey("int", "LLM turns per triage"),
    "budgets.max_tokens": ConfigKey("int", "total tokens per triage"),
    "budgets.max_tool_result_chars": ConfigKey("int", "longer tool results are truncated"),
    "global_kb_path": ConfigKey("path", "team-shared KB signature repo (a git checkout)"),
    "debfullname": ConfigKey("str", "maintainer name (fallback: $DEBFULLNAME)"),
    "debemail": ConfigKey("str", "maintainer email (fallback: $DEBEMAIL)"),
    "lp_auth": ConfigKey("str", "'anonymous' or a path to a launchpadlib credentials file"),
    "allow_private_data": ConfigKey("bool", "allow data from a private PPA to reach the LLM"),
}
SECTIONS = ("llm", "budgets")

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def key_spec(key: str) -> ConfigKey:
    if key in KEYS:
        return KEYS[key]
    close = difflib.get_close_matches(key, KEYS, n=1)
    hint = f"; did you mean {close[0]!r}?" if close else "; see 'th config keys'"
    raise ConfigError(f"unknown config key {key!r}{hint}")


def coerce(key: str, value: str) -> Any:
    """Turn a command-line string into the typed value stored for `key`."""
    spec = key_spec(key)
    if spec.kind == "int":
        try:
            n = int(value)
        except ValueError:
            raise ConfigError(f"{key}: expected an integer, got {value!r}") from None
        if n <= 0:
            raise ConfigError(f"{key}: must be positive")
        return n
    if spec.kind == "bool":
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
        raise ConfigError(f"{key}: expected true/false, got {value!r}")
    if not value.strip():
        raise ConfigError(f"{key}: empty value (use 'th config unset {key}' to clear it)")
    return value


def _walk(data: dict) -> Iterator[tuple[str, Any]]:
    """(dotted key, value) for every leaf in raw config data."""
    for k, v in data.items():
        if k in SECTIONS:
            if v is None:
                continue
            if not isinstance(v, dict):
                raise ConfigError(f"config: '{k}' must be a mapping")
            for sub, subv in v.items():
                yield f"{k}.{sub}", subv
        else:
            yield str(k), v


def validate_raw(data: Any, path: Path | None = None) -> UserConfig:
    """Strict check used before writing: no unknown keys, values of the right type."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("config: top level must be a mapping")
    for key, value in _walk(data):
        spec = key_spec(key)
        if value is None and spec.kind != "int":
            continue
        ok = {
            "int": isinstance(value, int) and not isinstance(value, bool),
            "bool": isinstance(value, bool),
        }.get(spec.kind, isinstance(value, str))
        if not ok:
            want = {"int": "an integer", "bool": "true or false"}.get(spec.kind, "a string")
            raise ConfigError(f"{key}: expected {want}, got {value!r}")
    return parse_config(data, path)


_UNSET = object()


def get_raw(data: dict, key: str, default: Any = None) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_raw(data: dict, key: str, value: Any) -> None:
    *parents, leaf = key.split(".")
    node = data
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[leaf] = value


def unset_raw(data: dict, key: str) -> bool:
    """Remove `key`; drops a section left empty. False if it was not set."""
    *parents, leaf = key.split(".")
    node: Any = data
    for part in parents:
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, dict) or leaf not in node:
        return False
    del node[leaf]
    if parents and not node:
        del data[parents[0]]
    return True


def apply_overrides(data: dict, overrides: Iterable[str]) -> dict:
    """Apply "key=value" overrides to a copy of `data`; "key=" resets to the default."""
    overrides = list(overrides)
    if not overrides:
        return data
    data = copy.deepcopy(data)
    for item in overrides:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep:
            raise ConfigError(f"-c {item!r}: expected KEY=VALUE")
        if value == "":
            key_spec(key)
            unset_raw(data, key)
        else:
            set_raw(data, key, coerce(key, value))
    return data


def effective_value(cfg: UserConfig, key: str) -> Any:
    obj: Any = cfg
    for part in key.split("."):
        obj = getattr(obj, part)
    return str(obj) if isinstance(obj, Path) else obj


def effective_dict(cfg: UserConfig) -> dict:
    out: dict = {}
    for key in KEYS:
        set_raw(out, key, effective_value(cfg, key))
    return out


def _scalar(value: Any) -> str:
    return yaml.safe_dump([value], default_flow_style=True, width=1 << 30).strip()[1:-1]


def render_config(data: dict) -> str:
    """YAML for (validated) `data` in KEYS order, each line commented with the key's help."""
    rows: list[tuple[str, str]] = []
    for key, spec in KEYS.items():
        value = get_raw(data, key, _UNSET)
        if value is _UNSET:
            continue
        section, _, leaf = key.rpartition(".")
        if section and (section + ":", "") not in rows:
            rows.append((section + ":", ""))
        rows.append((f"{'  ' if section else ''}{leaf}: {_scalar(value)}", spec.help))
    width = max((len(line) for line, help_ in rows if help_), default=0)
    return "".join(f"{line:<{width}}  # {help_}\n" if help_ else f"{line}\n"
                   for line, help_ in rows)


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


def write_text(path: Path, text: str) -> None:
    """Atomically replace `path`, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _has_comments(text: str) -> bool:
    return any(line.lstrip().startswith("#") or " #" in line for line in text.splitlines())


def update_config(path: Path, change: Callable[[dict], Any]) -> tuple[Any, Path | None]:
    """Read, change, validate and write the config file.

    Returns (change's result, backup path). PyYAML cannot round-trip comments, so a file
    that had any is first copied to `<name>.bak`.
    """
    text, data = read_raw(path)
    result = change(data)
    validate_raw(data, path)
    backup = None
    if _has_comments(text):
        backup = path.with_name(path.name + ".bak")
        write_text(backup, text)
    write_text(path, dump_yaml(data) if data else "")
    return result, backup


EDIT_ERROR = "# ERROR: "


def edit_template() -> str:
    lines = "".join(f"# {line}\n" if line else "#\n" for line in EXAMPLE_CONFIG.splitlines())
    return f"# transition-helper user config: uncomment and change what you need.\n{lines}"


def edit_config(path: Path, edit: Callable[[str], str], input_fn: Callable[[str], str] = input,
                out: Callable[[str], None] = print) -> bool:
    """`th config edit`: edit the file's text, re-prompting until it validates.

    Comments are kept. Returns True if the file was written.
    """
    original = path.read_text() if path.exists() else edit_template()
    text = original
    while True:
        text = edit(text)
        text = "".join(line for line in text.splitlines(keepends=True)
                       if not line.startswith(EDIT_ERROR))
        if text == original:
            out("no changes")
            return False
        try:
            validate_raw(yaml.safe_load(text), path)
            break
        except yaml.YAMLError as e:
            problem = f"invalid YAML: {e}"
        except ConfigError as e:
            problem = str(e)
        problem = " ".join(problem.split())
        out(f"invalid: {problem}")
        if input_fn("Edit again? [Y/n] ").strip().lower() in ("n", "no"):
            out("aborted; not saved")
            return False
        text = f"{EDIT_ERROR}{problem}\n" + text
    write_text(path, text)
    out(f"saved {path}")
    return True


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
