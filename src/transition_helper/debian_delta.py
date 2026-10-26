"""Debian unstable/experimental version check and changelog delta."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from debian.changelog import Changelog, ChangelogParseError
from debian.debian_support import Version

from .cache import HTTP, Cache, FetchError
from .versions import vkey

MADISON_URL = "https://api.ftp-master.debian.org/madison"
CHANGELOG_URL = "https://metadata.ftp-master.debian.org/changelogs/{component}/{prefix}/{pkg}/{suite}_changelog"
TTL = 6 * 3600
SUITES = ("unstable", "experimental")


def pool_prefix(pkg: str) -> str:
    return pkg[:4] if pkg.startswith("lib") and len(pkg) > 3 else pkg[0]


@dataclass
class ChangelogEntry:
    version: str
    distribution: str
    text: str
    relevant: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class DebianDelta:
    pkg: str
    ubuntu_version: str | None
    versions: dict[str, str] = field(default_factory=dict)  # suite -> source version
    components: dict[str, str] = field(default_factory=dict)
    newer_suite: str | None = None
    entries: list[ChangelogEntry] = field(default_factory=list)
    hint: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["entries"] = [e.to_dict() for e in self.entries]
        return d

    def summary(self) -> str:
        if self.error:
            return f"Debian check failed: {self.error}"
        if not self.versions:
            return "not in Debian unstable/experimental"
        vs = ", ".join(f"{s} {v}" for s, v in self.versions.items())
        return f"Debian: {vs}" + (f"; {self.hint}" if self.hint else "")


def parse_madison(data, pkg: str) -> tuple[dict[str, str], dict[str, str]]:
    """madison f=json -> ({suite: newest source version}, {suite: component})."""
    versions: dict[str, str] = {}
    components: dict[str, str] = {}
    if not isinstance(data, list):
        return versions, components
    for block in data:
        if not isinstance(block, dict):
            continue
        for suite, by_version in (block.get(pkg) or {}).items():
            if not isinstance(by_version, dict):
                continue
            for ver, info in by_version.items():
                src_ver = (info or {}).get("source_version") or ver
                if (info or {}).get("source", pkg) != pkg:
                    continue
                cur = versions.get(suite)
                if cur is None or Version(src_ver) > Version(cur):
                    versions[suite] = src_ver
                    components[suite] = (info or {}).get("component", "main")
    return versions, components


def changelog_delta(text: str, since: str, keywords: list[str]) -> list[ChangelogEntry]:
    """Changelog blocks newer than `since` (Ubuntu version), flagging keyword hits."""
    try:
        cl = Changelog(text, strict=False)
    except ChangelogParseError:
        return []
    out: list[ChangelogEntry] = []
    kw = [re.compile(re.escape(k), re.I) for k in keywords if k]
    since_v = vkey(since)
    for block in cl:
        try:
            if Version(str(block.version)) <= since_v:
                break
        except ValueError:
            continue
        body = "\n".join(line for line in block.changes() if line.strip())
        out.append(ChangelogEntry(
            version=str(block.version), distribution=str(block.distributions), text=body,
            relevant=any(rx.search(body) for rx in kw),
        ))
    return out


def debian_delta(http: HTTP, cache: Cache, pkg: str, ubuntu_version: str | None,
                 keywords: list[str]) -> DebianDelta:
    delta = DebianDelta(pkg=pkg, ubuntu_version=ubuntu_version)
    try:
        data = cache.cached_json(
            "debian", f"madison:{pkg}", TTL,
            lambda: http.get_json(MADISON_URL, params={"package": pkg, "f": "json",
                                                       "s": ",".join(SUITES)}))
    except (FetchError, FileNotFoundError) as e:
        delta.error = str(e)
        return delta
    delta.versions, delta.components = parse_madison(data, pkg)
    if not ubuntu_version or not delta.versions:
        return delta
    newer = [s for s in SUITES if s in delta.versions
             and Version(delta.versions[s]) > vkey(ubuntu_version)]
    if not newer:
        return delta
    suite = newer[0]  # prefer unstable over experimental
    delta.newer_suite = suite
    url = CHANGELOG_URL.format(component=delta.components.get(suite, "main"),
                               prefix=pool_prefix(pkg), pkg=pkg, suite=suite)
    try:
        text = cache.cached_json("debian", f"changelog:{pkg}:{suite}:{delta.versions[suite]}",
                                 None, lambda: {"text": http.get_text(url)})["text"]
    except (FetchError, FileNotFoundError) as e:
        delta.error = f"changelog: {e}"
        text = ""
    delta.entries = changelog_delta(text, ubuntu_version, keywords)
    relevant = [e for e in delta.entries if e.relevant]
    delta.hint = (f"Debian {suite} has newer {delta.versions[suite]}; a sync or merge may fix this"
                  + (f" ({len(relevant)} changelog entr{'y' if len(relevant) == 1 else 'ies'} "
                     "mention the transition)" if relevant else ""))
    return delta
