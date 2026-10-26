"""Knowledge base of YAML failure signatures; deterministic matcher."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SEED_DIR = Path(__file__).parent / "kb_seed"
APPLIES = ("build", "autopkgtest")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9.+_-]*$")


class KBError(Exception):
    pass


@dataclass
class Signature:
    id: str
    scope: str
    applies_to: list[str]
    patterns: list[str]
    explanation: str = ""
    fix_recipe: str = ""
    examples: list[dict] = field(default_factory=list)
    source: str = ""
    compiled: list[re.Pattern] = field(default_factory=list, repr=False)

    def to_dict(self, with_source: bool = False) -> dict:
        d = {"id": self.id, "scope": self.scope, "applies_to": self.applies_to,
             "patterns": self.patterns, "explanation": self.explanation,
             "fix_recipe": self.fix_recipe, "examples": self.examples}
        if with_source:
            d["source"] = self.source
        return d


@dataclass
class KBMatch:
    sig_id: str
    log_id: str
    lines: list[tuple[int, str]]

    def to_dict(self) -> dict:
        return {"sig_id": self.sig_id, "log_id": self.log_id, "lines": [list(x) for x in self.lines]}


def validate_signature(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["signature must be a mapping"]
    sid = data.get("id")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        errors.append("id must be a lowercase slug")
    if not isinstance(data.get("scope"), str) or not data.get("scope"):
        errors.append("scope is required (transition name or 'global')")
    applies = data.get("applies_to")
    if not isinstance(applies, list) or not applies or any(a not in APPLIES for a in applies):
        errors.append(f"applies_to must be a non-empty list of {APPLIES}")
    pats = data.get("patterns")
    if not isinstance(pats, list) or not pats or not all(isinstance(p, str) and p for p in pats):
        errors.append("patterns must be a non-empty list of regex strings")
    else:
        for p in pats:
            try:
                re.compile(p)
            except re.error as e:
                errors.append(f"pattern {p!r}: {e}")
    ex = data.get("examples", [])
    if ex is not None and not isinstance(ex, list):
        errors.append("examples must be a list")
    for key in ("explanation", "fix_recipe"):
        if data.get(key) is not None and not isinstance(data.get(key), str):
            errors.append(f"{key} must be a string")
    unknown = set(data) - {"id", "scope", "applies_to", "patterns", "explanation", "fix_recipe",
                           "examples"}
    if unknown:
        errors.append(f"unknown keys: {', '.join(sorted(unknown))}")
    return errors


def make_signature(data: dict, source: str) -> Signature:
    errors = validate_signature(data)
    if errors:
        raise KBError(f"{source}: " + "; ".join(errors))
    return Signature(
        id=data["id"], scope=data["scope"], applies_to=list(data["applies_to"]),
        patterns=list(data["patterns"]), explanation=(data.get("explanation") or "").strip(),
        fix_recipe=(data.get("fix_recipe") or "").strip(), examples=list(data.get("examples") or []),
        source=source, compiled=[re.compile(p) for p in data["patterns"]],
    )


def load_file(path: Path) -> list[Signature]:
    try:
        docs = list(yaml.safe_load_all(path.read_text()))
    except yaml.YAMLError as e:
        raise KBError(f"{path}: invalid YAML: {e}") from e
    out = []
    for doc in docs:
        if doc is None:
            continue
        for item in doc if isinstance(doc, list) else [doc]:
            out.append(make_signature(item, str(path)))
    return out


class KB:
    def __init__(self, signatures: list[Signature], errors: list[str] | None = None):
        self.signatures = signatures
        self.errors = errors or []

    @classmethod
    def load(cls, dirs: list[Path | None], include_seed: bool = True) -> "KB":
        """Later directories override earlier ones by id (seed < global < workspace)."""
        by_id: dict[str, Signature] = {}
        errors: list[str] = []
        all_dirs = ([SEED_DIR] if include_seed else []) + [d for d in dirs if d]
        for d in all_dirs:
            if not d.is_dir():
                continue
            for path in sorted(list(d.rglob("*.yaml")) + list(d.rglob("*.yml"))):
                if any(part.startswith(".") for part in path.relative_to(d).parts):
                    continue
                try:
                    for sig in load_file(path):
                        by_id[sig.id] = sig
                except KBError as e:
                    errors.append(str(e))
        return cls(list(by_id.values()), errors)

    def get(self, sig_id: str) -> Signature | None:
        return next((s for s in self.signatures if s.id == sig_id), None)

    def match_lines(self, log_id: str, kind: str, lines, max_lines: int = 5) -> list[KBMatch]:
        sigs = [s for s in self.signatures if kind in s.applies_to]
        found: dict[str, KBMatch] = {}
        for n, line in lines:
            for s in sigs:
                m = found.get(s.id)
                if m and len(m.lines) >= max_lines:
                    continue
                if any(rx.search(line) for rx in s.compiled):
                    if m is None:
                        m = found[s.id] = KBMatch(s.id, log_id, [])
                    m.lines.append((n, line))
        return list(found.values())

    def match_log(self, store, log_id: str) -> list[KBMatch]:
        from .logs import log_kind

        return self.match_lines(log_id, log_kind(log_id), enumerate(store.iter_lines(log_id), 1))

    def lookup(self, query: str) -> list[Signature]:
        sig = self.get(query)
        if sig:
            return [sig]
        try:
            rx = re.compile(query, re.I)
        except re.error:
            rx = re.compile(re.escape(query), re.I)
        return [s for s in self.signatures
                if rx.search(s.id) or rx.search(s.explanation) or rx.search(s.fix_recipe)
                or any(rx.search(p) for p in s.patterns)]
