"""Where the triaged package blocks the migration of a *watched* source.

A package can hold up the transition without having anything of its own in -proposed:
e.g. python3-defaults is stuck because package X's autopkgtest regresses when triggered by
python3-defaults. X's own excuse (if any) says nothing about that, so we look for X inside
the excuses of the watched sources (the transition's toolchain_source plus watch_sources)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .classify import Category, Classification, FailingTest, Phase, to_failing_test
from .excuses import FAIL_STATUSES, PENDING_STATUSES, Excuse, ExcusesIndex
from .versions import strip_ppa
from .workspace import Transition


@dataclass
class WatchedBlock:
    watched: str                  # the source whose migration is held up
    watched_version: str | None   # its version in -proposed (the excuse's new-version)
    kind: str                     # autopkgtest-regression | autopkgtest-pending | uninstallable
                                  # | blocked-by | migrate-after
    detail: str
    arch: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    def render(self) -> str:
        return f"{self.watched} {self.watched_version or ''}: {self.detail}".replace("  ", " ")


@dataclass
class WatchResult:
    pkg: str
    watched: list[str]                       # sources whose excuses were checked
    missing: list[str] = field(default_factory=list)  # watched sources without an excuse
    blocks: list[WatchedBlock] = field(default_factory=list)
    failing_tests: list[FailingTest] = field(default_factory=list)
    pending_tests: list[FailingTest] = field(default_factory=list)
    uninstallable: list[str] = field(default_factory=list)
    # tested arches of pkg's test across the watched excuses (for the infra heuristics)
    tested_arches: set[str] = field(default_factory=set)
    # a watched excuse also waits on other unmigrated packages (tests may need all-proposed)
    watched_has_unmigrated: bool = False

    @property
    def blocked(self) -> list[str]:
        """Watched sources that the package blocks, in order."""
        return list(dict.fromkeys(b.watched for b in self.blocks))

    def to_dict(self) -> dict:
        return {"pkg": self.pkg, "watched": self.watched, "missing": self.missing,
                "blocked": self.blocked, "blocks": [b.to_dict() for b in self.blocks],
                "failing_tests": [t.to_dict() for t in self.failing_tests],
                "pending_tests": [t.to_dict() for t in self.pending_tests],
                "uninstallable": self.uninstallable}

    def summary(self) -> list[str]:
        """One line per blocked watched source."""
        out = []
        for w in self.blocked:
            bs = [b for b in self.blocks if b.watched == w]
            kinds: dict[str, list[str]] = {}
            for b in bs:
                kinds.setdefault(b.kind, [])
                if b.arch and b.arch not in kinds[b.kind]:
                    kinds[b.kind].append(b.arch)
            parts = [k + (f" on {','.join(sorted(a))}" if a else "") for k, a in kinds.items()]
            out.append(f"blocks {w} {bs[0].watched_version or ''}: {'; '.join(parts)}"
                       .replace("  ", " "))
        return out


def watched_sources(t: Transition, pkg: str) -> list[str]:
    """toolchain_source + watch_sources, deduplicated, without the triaged package itself
    (its own excuse is analysed anyway)."""
    return [s for s in dict.fromkeys([t.toolchain_source, *t.watch_sources]) if s and s != pkg]


def _version_set(versions: dict[str, str | None]) -> set[str]:
    out = set()
    for v in versions.values():
        if v:
            out.add(v)
            out.add(strip_ppa(v))
    return out


def find_blocks(excuses: ExcusesIndex, pkg: str, watched: list[str],
                bin_to_src: Callable[[str, str], str | None] | None = None,
                pkg_versions: dict[str, str | None] | None = None,
                arch: str | None = None, ignored_arches: set[str] | None = None) -> WatchResult:
    """Find pkg in the excuses of the watched sources.

    Impossible Depends name a binary, not a source. Resolving each binary costs a Launchpad
    call, so only binaries whose version is one of pkg's known source versions are resolved
    (binaries normally carry their source's version). Without pkg_versions every binary is
    resolved."""
    ignored = set(ignored_arches or ())

    def keep(a: str) -> bool:
        return a == arch if arch else a not in ignored

    res = WatchResult(pkg=pkg, watched=list(watched))
    versions = _version_set(pkg_versions) if pkg_versions else None
    for w in watched:
        excuse = excuses.get(w)
        if excuse is None:
            res.missing.append(w)
            continue
        _scan(excuse, pkg, res, keep, bin_to_src, versions)
    return res


def _scan(excuse: Excuse, pkg: str, res: WatchResult, keep, bin_to_src, versions) -> None:
    w, wv = excuse.source, excuse.new_version
    before = len(res.blocks)
    for dep, kind in [(b, "blocked-by") for b in excuse.blocked_by] + \
                     [(b, "migrate-after") for b in excuse.migrate_after]:
        if dep == pkg:
            res.blocks.append(WatchedBlock(w, wv, kind, f"{kind} {pkg}: waits for {pkg} to migrate"))
    for t in excuse.tests:
        if t.test_pkg != pkg:
            continue
        res.tested_arches.add(t.arch)
        if not keep(t.arch):
            continue
        ft = to_failing_test(t, pkg)
        ft.via = w
        if t.status in FAIL_STATUSES:
            res.failing_tests.append(ft)
            res.blocks.append(WatchedBlock(
                w, wv, "autopkgtest-regression",
                f"{pkg} {t.test_version or ''} autopkgtest {t.status} on {t.arch} "
                f"(triggers {' '.join(t.triggers)})", arch=t.arch))
        elif t.status in PENDING_STATUSES:
            res.pending_tests.append(ft)
            res.blocks.append(WatchedBlock(
                w, wv, "autopkgtest-pending",
                f"{pkg} autopkgtest {t.status} on {t.arch}", arch=t.arch))
    if bin_to_src:
        resolved: dict[str, str | None] = {}
        for binary, bversion, a in excuse.impossible_depends:
            if not keep(a) or (versions is not None and bversion not in versions):
                continue
            if binary not in resolved:
                resolved[binary] = bin_to_src(binary, a)
            if resolved[binary] != pkg:
                continue
            line = f"{w} needs {binary}/{bversion} on {a} (impossible depends)"
            res.uninstallable.append(f"{binary}/{a} (needed by {w}; impossible depends on {bversion})")
            res.blocks.append(WatchedBlock(w, wv, "uninstallable", line, arch=a))
    if len(res.blocks) > before and (set(excuse.blocked_by) | set(excuse.migrate_after)) - {pkg}:
        res.watched_has_unmigrated = True


def classify_watched(res: WatchResult) -> Classification:
    """Classification for a package that has nothing of its own in -proposed but blocks a
    watched source: the watched excuses' findings about it become its problems."""
    cats: set[Category] = set()
    c = Classification(phase=Phase.MIGRATION, categories=[])
    c.failing_tests = list(res.failing_tests)
    c.pending_tests = list(res.pending_tests)
    c.uninstallable = sorted(set(res.uninstallable))
    if c.failing_tests:
        cats.add(Category.AUTOPKGTEST_REGRESSION)
    if c.pending_tests:
        cats.add(Category.AUTOPKGTEST_PENDING)
    if c.uninstallable:
        cats.add(Category.UNINSTALLABLE)
    order = [Category.AUTOPKGTEST_REGRESSION, Category.UNINSTALLABLE, Category.AUTOPKGTEST_PENDING]
    c.categories = [x for x in order if x in cats] or [Category.OTHER]
    c.notes = [f"no upload of {res.pkg} of its own is pending; it " + s for s in res.summary()]
    if any(b.kind in ("blocked-by", "migrate-after") for b in res.blocks):
        c.notes.append("a watched source waits for this package to migrate")
    return c
