"""Per-transition workspace: discovery, transition.yaml model, directory layout."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

TRANSITION_FILE = "transition.yaml"
BUILD_LOCATIONS = ("ppa", "archive")
PPA_RE = re.compile(r"^~[a-z0-9][a-z0-9+.-]*/ubuntu/[a-z0-9][a-z0-9+.-]*$")


class WorkspaceError(Exception):
    pass


class WorkspaceNotFound(WorkspaceError):
    pass


@dataclass
class Transition:
    name: str
    series: str
    toolchain_source: str
    old_version: str
    new_version: str
    build_location: str = "archive"
    ppa: str | None = None
    relevant_binaries: list[str] = field(default_factory=list)
    ignored_arches: list[str] = field(default_factory=list)
    lp_bug_tags: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        errors = []
        for key in ("name", "series", "toolchain_source", "old_version", "new_version"):
            if not getattr(self, key):
                errors.append(f"{key} is required")
        if self.build_location not in BUILD_LOCATIONS:
            errors.append(f"build_location must be one of {', '.join(BUILD_LOCATIONS)}")
        if self.build_location == "ppa":
            if not self.ppa:
                errors.append("ppa is required when build_location is ppa")
            elif not PPA_RE.match(self.ppa):
                errors.append("ppa must look like ~owner/ubuntu/name")
        for key in ("relevant_binaries", "ignored_arches", "lp_bug_tags", "docs"):
            value = getattr(self, key)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append(f"{key} must be a list of strings")
        return errors

    @classmethod
    def from_dict(cls, data: dict) -> "Transition":
        if not isinstance(data, dict):
            raise WorkspaceError(f"{TRANSITION_FILE}: top level must be a mapping")
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise WorkspaceError(f"{TRANSITION_FILE}: unknown keys: {', '.join(sorted(unknown))}")
        kwargs = {}
        for key in known:
            if key in data and data[key] is not None:
                value = data[key]
                if key in ("old_version", "new_version", "name", "series"):
                    value = str(value)
                kwargs[key] = value
        try:
            t = cls(**kwargs)
        except TypeError as e:
            raise WorkspaceError(f"{TRANSITION_FILE}: {e}") from e
        errors = t.validate()
        if errors:
            raise WorkspaceError(f"{TRANSITION_FILE}: " + "; ".join(errors))
        return t

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.build_location != "ppa":
            d.pop("ppa")
        return d

    def toolchain_label(self) -> str:
        return f"{self.toolchain_source} {self.new_version}"


@dataclass
class Workspace:
    root: Path
    transition: Transition

    @property
    def docs_dir(self) -> Path:
        return self.root / "docs"

    @property
    def kb_dir(self) -> Path:
        return self.root / "kb"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def cache_dir(self) -> Path:
        return self.root / ".cache"

    def new_report_dir(self, pkg: str, now: datetime | None = None) -> Path:
        now = now or datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%dT%H%M%SZ")
        path = self.reports_dir / pkg / ts
        n = 1
        while path.exists():
            n += 1
            path = self.reports_dir / pkg / f"{ts}-{n}"
        path.mkdir(parents=True)
        return path

    @classmethod
    def load(cls, root: Path) -> "Workspace":
        path = root / TRANSITION_FILE
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError as e:
            raise WorkspaceError(f"{path}: invalid YAML: {e}") from e
        return cls(root=root, transition=Transition.from_dict(data or {}))


def find_workspace_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default cwd) to the first dir containing transition.yaml."""
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        if (d / TRANSITION_FILE).is_file():
            return d
    raise WorkspaceNotFound(
        f"no {TRANSITION_FILE} found in {cur} or any parent directory; run 'th init' first"
    )


def find_workspace(start: Path | None = None) -> Workspace:
    return Workspace.load(find_workspace_root(start))


def create_workspace(root: Path, transition: Transition) -> Workspace:
    errors = transition.validate()
    if errors:
        raise WorkspaceError("; ".join(errors))
    for sub in ("docs", "kb", "reports", ".cache"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / TRANSITION_FILE).write_text(
        yaml.safe_dump(transition.to_dict(), sort_keys=False, default_flow_style=None)
    )
    return Workspace(root=root, transition=transition)
