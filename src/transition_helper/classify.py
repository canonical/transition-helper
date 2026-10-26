"""Step 2: deterministic classification of the blocker."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from . import lp as lpmod
from .excuses import Excuse
from .locate import LocateResult, State


class Phase(StrEnum):
    BUILD = "build"
    MIGRATION = "migration"
    DONE = "done"
    NOT_STARTED = "not-started"
    UNKNOWN = "unknown"


class Category(StrEnum):
    # build
    FTBFS = "FTBFS"
    DEPWAIT = "DEPWAIT"
    BUILD_INFRA = "BUILD_INFRA"
    BUILD_PENDING = "BUILD_PENDING"
    SUPERSEDED = "SUPERSEDED"
    # migration
    AUTOPKGTEST_REGRESSION = "AUTOPKGTEST_REGRESSION"
    AUTOPKGTEST_PENDING = "AUTOPKGTEST_PENDING"
    DEPENDS_ON_UNMIGRATED = "DEPENDS_ON_UNMIGRATED"
    UNINSTALLABLE = "UNINSTALLABLE"
    MISSING_BUILD = "MISSING_BUILD"
    BLOCKED = "BLOCKED"
    OTHER = "OTHER"
    # nothing blocking from our point of view
    NONE = "NONE"


BUILD_PRIORITY = [Category.FTBFS, Category.DEPWAIT, Category.BUILD_INFRA,
                  Category.BUILD_PENDING, Category.SUPERSEDED]
MIGRATION_PRIORITY = [Category.UNINSTALLABLE, Category.DEPENDS_ON_UNMIGRATED,
                      Category.AUTOPKGTEST_REGRESSION, Category.MISSING_BUILD,
                      Category.BLOCKED, Category.AUTOPKGTEST_PENDING, Category.OTHER]

BLOCK_LINE_RE = re.compile(r"block request|Not touching package|freeze|block-proposed", re.I)


@dataclass
class FailingTest:
    test_pkg: str
    test_version: str | None
    arch: str
    status: str
    triggers: list[str]
    log_url: str | None
    history_url: str | None
    retry_url: str | None
    run_id: str | None
    own: bool  # the triaged package's own test, as opposed to a reverse dependency's

    @property
    def kind(self) -> str:
        return "own test" if self.own else "reverse-dependency test"

    def to_dict(self) -> dict:
        return dict(self.__dict__) | {"kind": self.kind}


@dataclass
class BuildIssue:
    arch: str
    category: Category
    state: str
    log_url: str | None
    web_link: str | None
    missing_dependency: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__) | {"category": str(self.category)}


@dataclass
class Classification:
    phase: Phase
    categories: list[Category]
    build_issues: list[BuildIssue] = field(default_factory=list)
    failing_tests: list[FailingTest] = field(default_factory=list)
    pending_tests: list[FailingTest] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    uninstallable: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def primary(self) -> Category:
        return self.categories[0] if self.categories else Category.NONE

    def to_dict(self) -> dict:
        return {
            "phase": str(self.phase),
            "categories": [str(c) for c in self.categories],
            "primary": str(self.primary),
            "build_issues": [b.to_dict() for b in self.build_issues],
            "failing_tests": [t.to_dict() for t in self.failing_tests],
            "pending_tests": [t.to_dict() for t in self.pending_tests],
            "blockers": self.blockers,
            "uninstallable": self.uninstallable,
            "notes": self.notes,
        }


def build_category(state: str) -> Category | None:
    if state == lpmod.BUILD_OK:
        return None
    if state == lpmod.BUILD_FAILED:
        return Category.FTBFS
    if state == lpmod.BUILD_DEPWAIT:
        return Category.DEPWAIT
    if state in lpmod.BUILD_PENDING_STATES:
        return Category.BUILD_PENDING
    if state == lpmod.BUILD_SUPERSEDED:
        return Category.SUPERSEDED
    # chroot problem, failed to upload, cancelled, and anything new LP invents
    return Category.BUILD_INFRA


def _ordered(cats: set[Category], priority: list[Category]) -> list[Category]:
    return [c for c in priority if c in cats]


def to_failing_test(t, source: str) -> FailingTest:
    return FailingTest(
        test_pkg=t.test_pkg, test_version=t.test_version, arch=t.arch, status=t.status,
        triggers=list(t.triggers), log_url=t.log_url, history_url=t.history_url,
        retry_url=t.retry_url, run_id=t.run_id, own=(t.test_pkg == source),
    )


def classify_migration(excuse: Excuse, arch: str | None = None,
                       ignored_arches: set[str] | None = None) -> Classification:
    ignored = set(ignored_arches or ())

    def keep(a: str) -> bool:
        return a == arch if arch else a not in ignored

    cats: set[Category] = set()
    c = Classification(phase=Phase.MIGRATION, categories=[])
    if excuse.blocked_by or excuse.migrate_after or excuse.invalidated_by_other_package:
        cats.add(Category.DEPENDS_ON_UNMIGRATED)
        c.blockers = sorted(set(excuse.blocked_by) | set(excuse.migrate_after))
    depends_rejected = excuse.depends_verdict not in (None, "PASS")
    has_detail = bool(excuse.impossible_depends or excuse.unsatisfiable or excuse.component_mismatch)
    if depends_rejected or ("depends" in excuse.reasons and has_detail):
        if has_detail:
            cats.add(Category.UNINSTALLABLE)
            c.uninstallable = sorted(
                {f"{b}/{a} (impossible depends on {v})" for b, v, a in excuse.impossible_depends
                 if keep(a)}
                | {f"{b}/{a} unsatisfiable" for b, a in excuse.unsatisfiable if keep(a)}
                | set(excuse.component_mismatch[:20])
            )
        elif Category.DEPENDS_ON_UNMIGRATED not in cats:
            cats.add(Category.OTHER)
            c.notes.append("depends policy rejected without parseable detail")
    failing = [to_failing_test(t, excuse.source) for t in excuse.failing_tests() if keep(t.arch)]
    pending = [to_failing_test(t, excuse.source) for t in excuse.pending_tests() if keep(t.arch)]
    c.failing_tests, c.pending_tests = failing, pending
    if failing:
        cats.add(Category.AUTOPKGTEST_REGRESSION)
    elif excuse.autopkgtest_verdict == "REJECTED_PERMANENTLY":
        if excuse.failing_tests():
            c.notes.append("autopkgtest regressions only on ignored arches")
        else:
            cats.add(Category.AUTOPKGTEST_REGRESSION)
    if pending or excuse.autopkgtest_verdict == "REJECTED_TEMPORARILY":
        cats.add(Category.AUTOPKGTEST_PENDING)
    if [a for a in excuse.missing_builds if keep(a)] or "no-binaries" in excuse.reasons:
        cats.add(Category.MISSING_BUILD)
    block_lines = [line for line in excuse.lines if BLOCK_LINE_RE.search(line)]
    if (excuse.block_verdict not in (None, "PASS")
            or excuse.block_bugs_verdict not in (None, "PASS")
            or "block" in excuse.reasons or block_lines):
        cats.add(Category.BLOCKED)
        if excuse.block_bugs:
            c.notes.append("blocked by bug(s): " + ", ".join(f"LP: #{b}" for b in excuse.block_bugs))
        c.notes.extend(block_lines[:5])
    if not cats and not excuse.is_candidate:
        cats.add(Category.OTHER)
        c.notes.append(f"not a candidate (verdict {excuse.verdict}); reasons: "
                       f"{', '.join(excuse.reasons) or 'none given'}")
    c.categories = _ordered(cats, MIGRATION_PRIORITY)
    if not c.categories:
        c.categories = [Category.NONE]
        c.notes.append("excuse shows the package as a valid candidate; it should migrate soon")
    return c


def classify(loc: LocateResult, excuse: Excuse | None,
             ignored_arches: set[str] | None = None, arch: str | None = None) -> Classification:
    if loc.state == State.NOT_FOUND:
        return Classification(phase=Phase.UNKNOWN, categories=[Category.OTHER],
                              notes=list(loc.notes))
    if loc.state == State.MIGRATED:
        notes = ["migrated"] + [n for n in loc.notes if "failed on" in n]
        return Classification(phase=Phase.DONE, categories=[Category.NONE], notes=notes)
    if loc.state == State.NOT_REBUILT:
        return Classification(phase=Phase.NOT_STARTED, categories=[Category.NONE],
                              notes=["no rebuild uploaded yet for this transition"])

    if loc.state in (State.BUILD_FAILED, State.BUILD_PENDING, State.PARTIAL_BUILD):
        cats: set[Category] = set()
        c = Classification(phase=Phase.BUILD, categories=[])
        for b in loc.builds:
            if b.arch not in loc.failed_arches and b.arch not in loc.pending_arches:
                continue
            cat = build_category(b.state)
            if cat is None:
                continue
            cats.add(cat)
            c.build_issues.append(BuildIssue(
                arch=b.arch, category=cat, state=b.state, log_url=b.log_url,
                web_link=b.web_link,
                missing_dependency=b.dependencies if cat == Category.DEPWAIT else None,
            ))
        c.categories = _ordered(cats, BUILD_PRIORITY) or [Category.OTHER]
        if excuse is not None and excuse.new_version == loc.target_version:
            mig = classify_migration(excuse, arch, ignored_arches)
            extra = [cat for cat in mig.categories
                     if cat not in (Category.MISSING_BUILD, Category.NONE)]
            if extra:
                c.notes.append("excuse also reports: " + ", ".join(extra))
        return c

    if loc.state == State.BUILT_IN_PPA_NOT_IN_PROPOSED:
        return Classification(phase=Phase.BUILD, categories=[Category.NONE],
                              notes=["all arches built in PPA; not in -proposed yet"])

    if loc.state == State.IN_PROPOSED_NO_EXCUSE_YET:
        return Classification(phase=Phase.MIGRATION, categories=[Category.NONE],
                              notes=["in -proposed but britney has not produced an excuse yet"])

    assert excuse is not None
    return classify_migration(excuse, arch, ignored_arches)
