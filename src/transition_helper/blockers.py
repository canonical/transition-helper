"""Step 3: walk DEPENDS_ON_UNMIGRATED / UNINSTALLABLE blockers through excuses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .excuses import Excuse, ExcusesIndex

MAX_DEPTH = 6


@dataclass
class BlockerEdge:
    pkg: str
    blocker: str
    reason: str  # "blocked-by" | "migrate-after" | "uninstallable via <binary>"


@dataclass
class BlockerChain:
    start: str
    edges: list[BlockerEdge] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)  # stuck for their own reasons
    not_in_proposed: list[str] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "edges": [e.__dict__ for e in self.edges],
            "roots": self.roots,
            "not_in_proposed": self.not_in_proposed,
            "cycles": self.cycles,
            "truncated": self.truncated,
        }

    def render(self) -> list[str]:
        lines = [f"{e.pkg} -> {e.blocker} ({e.reason})" for e in self.edges]
        for cyc in self.cycles:
            lines.append("cycle: " + " -> ".join(cyc))
        if self.truncated:
            lines.append(f"(stopped at depth {MAX_DEPTH})")
        return lines


def direct_blockers(excuse: Excuse, bin_to_src: Callable[[str, str], str | None] | None,
                    ) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for b in excuse.blocked_by:
        out.append((b, "blocked-by"))
    for b in excuse.migrate_after:
        out.append((b, "migrate-after"))
    if bin_to_src:
        seen = set()
        for binary, _version, arch in excuse.impossible_depends:
            if binary in seen:
                continue
            seen.add(binary)
            src = bin_to_src(binary, arch)
            if src and src != excuse.source:
                out.append((src, f"uninstallable via {binary}"))
    # stable order, one edge per blocker
    dedup: dict[str, str] = {}
    for b, why in out:
        dedup.setdefault(b, why)
    return list(dedup.items())


def blocker_chain(excuses: ExcusesIndex, pkg: str,
                  bin_to_src: Callable[[str, str], str | None] | None = None,
                  max_depth: int = MAX_DEPTH) -> BlockerChain:
    chain = BlockerChain(start=pkg)
    roots: list[str] = []
    expanded: set[str] = set()

    def walk(node: str, path: list[str], depth: int) -> None:
        excuse = excuses.get(node)
        if excuse is None:
            if node not in chain.not_in_proposed:
                chain.not_in_proposed.append(node)
            return
        blockers = direct_blockers(excuse, bin_to_src)
        if not blockers:
            if node != pkg and node not in roots:
                roots.append(node)
            return
        if node in expanded:
            return
        expanded.add(node)
        if depth >= max_depth:
            chain.truncated = True
            return
        for blocker, why in blockers:
            chain.edges.append(BlockerEdge(node, blocker, why))
            if blocker in path:
                chain.cycles.append(path[path.index(blocker):] + [blocker])
                continue
            walk(blocker, path + [blocker], depth + 1)

    walk(pkg, [pkg], 0)
    # a cycle with nothing else stuck: every member of the cycle is a root of sorts
    if not roots and chain.cycles:
        for cyc in chain.cycles:
            for n in cyc:
                if n not in roots:
                    roots.append(n)
    chain.roots = roots
    return chain
