"""Step 1: locate the package's versions/builds and report the observed state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from . import lp as lpmod
from .excuses import ExcusesIndex
from .lp import BuildRec, LPClient, SourcePub
from .versions import strip_ppa, vkey
from .workspace import Transition


class State(StrEnum):
    BUILD_FAILED = "BUILD_FAILED"
    BUILD_PENDING = "BUILD_PENDING"
    PARTIAL_BUILD = "PARTIAL_BUILD"
    BUILT_IN_PPA_NOT_IN_PROPOSED = "BUILT_IN_PPA_NOT_IN_PROPOSED"
    IN_PROPOSED_NO_EXCUSE_YET = "IN_PROPOSED_NO_EXCUSE_YET"
    IN_PROPOSED_WITH_EXCUSE = "IN_PROPOSED_WITH_EXCUSE"
    MIGRATED = "MIGRATED"
    NOT_REBUILT = "NOT_REBUILT"
    NOT_FOUND = "NOT_FOUND"


# where a version is "further along" when versions tie
LOCATION_RANK = {"release": 3, "proposed": 2, "ppa": 1}


@dataclass
class LocateResult:
    pkg: str
    series: str
    state: State
    target_version: str | None
    target_location: str | None
    versions: dict[str, str | None]
    builds: list[BuildRec] = field(default_factory=list)
    failed_arches: list[str] = field(default_factory=list)
    pending_arches: list[str] = field(default_factory=list)
    built_arches: list[str] = field(default_factory=list)
    ignored_failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ppa: str | None = None
    ppa_private: bool = False

    @property
    def base_version(self) -> str | None:
        return strip_ppa(self.target_version) if self.target_version else None

    def build_for(self, arch: str) -> BuildRec | None:
        for b in self.builds:
            if b.arch == arch:
                return b
        return None

    def summary(self) -> str:
        if self.state == State.NOT_FOUND:
            return f"{self.pkg} not found in {self.series}"
        where = {"ppa": "PPA", "proposed": "-proposed", "release": "release pocket"}.get(
            self.target_location or "", self.target_location or "?")
        s = f"{self.target_version} in {where}"
        if self.failed_arches:
            states = sorted({(b.state) for b in self.builds if b.arch in self.failed_arches})
            s += f", {'/'.join(states)} on {','.join(self.failed_arches)}"
        elif self.pending_arches:
            s += f", pending on {','.join(self.pending_arches)}"
        return f"{s} [{self.state}]"

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["state"] = str(self.state)
        d["builds"] = [b.to_dict() for b in self.builds]
        return d


def _newest(pubs: list[SourcePub], location: str) -> SourcePub | None:
    cands = [p for p in pubs if p.location == location and p.status in lpmod.ACTIVE_STATUSES]
    return max(cands, key=lambda p: vkey(p.version)) if cands else None


def pick_target(cands: dict[str, SourcePub]) -> SourcePub | None:
    """Newest version (ignoring ~ppaN); on a tie prefer the location further along."""
    if not cands:
        return None
    return max(cands.values(), key=lambda p: (vkey(p.version), LOCATION_RANK[p.location]))


def build_state_of(builds: list[BuildRec], focus_arches: set[str] | None,
                   ignored: set[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    failed, pending, built, ignored_failures = [], [], [], []
    for b in builds:
        if focus_arches and b.arch not in focus_arches:
            continue
        if b.state == lpmod.BUILD_OK:
            built.append(b.arch)
        elif b.state in lpmod.BUILD_PENDING_STATES:
            pending.append(b.arch)
        elif b.state == lpmod.BUILD_SUPERSEDED:
            continue
        elif b.arch in ignored and not focus_arches:
            ignored_failures.append(b.arch)
        else:
            failed.append(b.arch)
    return sorted(failed), sorted(pending), sorted(built), sorted(ignored_failures)


def locate(lp: LPClient, transition: Transition, pkg: str,
           excuses: ExcusesIndex | None, arch: str | None = None) -> LocateResult:
    series = transition.series
    ppa = transition.ppa if transition.build_location == "ppa" else None
    pubs = lp.published_sources(pkg, series, ppa=ppa)
    cands = {loc: p for loc in ("ppa", "proposed", "release") if (p := _newest(pubs, loc))}
    versions = {loc: (cands[loc].version if loc in cands else None)
                for loc in ("ppa", "proposed", "release")}
    if not ppa:
        versions.pop("ppa")
    result = LocateResult(pkg=pkg, series=series, state=State.NOT_FOUND, target_version=None,
                          target_location=None, versions=versions, ppa=ppa)
    target = pick_target(cands)
    if target is None:
        result.notes.append(f"no active publication of {pkg} in {series}"
                            + (f" or {ppa}" if ppa else ""))
        return result
    result.target_version = target.version
    result.target_location = target.location

    if ppa and "ppa" in cands and target.location != "ppa":
        result.notes.append(
            f"PPA has {cands['ppa'].version}, which is not newer than "
            f"{target.version} in {target.location}")

    if target.location == "release":
        result.builds = lp.builds(target)
        failed, _, built, _ = build_state_of(result.builds, {arch} if arch else None,
                                             set(transition.ignored_arches))
        result.built_arches = built
        if failed:
            result.failed_arches = failed
            result.notes.append(f"builds of {target.version} failed on {','.join(failed)} "
                                "(binaries there may be stale)")
        if lp.went_through_proposed(pkg, series, target.version):
            result.state = State.MIGRATED
            result.notes.append(f"{target.version} went through {series}-proposed; "
                                "check it was built against the new toolchain")
        else:
            result.state = State.NOT_REBUILT
            result.notes.append(f"no upload newer than the release-pocket version {target.version}")
        return result

    builds = lp.builds(target)
    result.builds = builds
    focus = {arch} if arch else None
    ignored = set(transition.ignored_arches)
    failed, pending, built, ignored_failures = build_state_of(builds, focus, ignored)
    result.failed_arches, result.pending_arches = failed, pending
    result.built_arches, result.ignored_failures = built, ignored_failures
    if ignored_failures:
        result.notes.append(f"ignoring build failures on {','.join(ignored_failures)} "
                            "(ignored_arches; use --arch to include)")
    if arch and not any(b.arch == arch for b in builds):
        result.notes.append(f"no build record for {arch}")

    if not builds:
        # publications copied from another series/archive carry no builds of their own
        result.notes.append("no build records for this publication (copied?); treating as built")
    if failed:
        result.state = State.BUILD_FAILED
    elif pending and not built:
        result.state = State.BUILD_PENDING
    elif pending:
        result.state = State.PARTIAL_BUILD
    elif target.location == "ppa":
        result.state = State.BUILT_IN_PPA_NOT_IN_PROPOSED
        result.notes.append("all arches built in PPA; not in -proposed yet")
    elif excuses is not None and excuses.get(pkg) is not None \
            and excuses.get(pkg).new_version == target.version:
        result.state = State.IN_PROPOSED_WITH_EXCUSE
    else:
        result.state = State.IN_PROPOSED_NO_EXCUSE_YET
        if excuses is not None and (e := excuses.get(pkg)) is not None:
            result.notes.append(f"excuse is for {e.new_version}, not {target.version}")
    return result
