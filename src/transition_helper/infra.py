"""Step 4: infra / flaky heuristics for failing tests and builds."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .autopkgtest import RunResult

INFRA_MARKERS: list[tuple[str, re.Pattern]] = [
    (name, re.compile(rx, re.I)) for name, rx in [
        ("timeout", r"\b(timed out|timeout reached|: ERROR: timed out|Killed by signal|exceeded the time limit)\b"),
        ("network", r"(Temporary failure (in name resolution|resolving)|Could not resolve|Network is unreachable"
                    r"|Connection (timed out|refused|reset by peer)|Failed to fetch http"
                    r"|Unable to connect to|Name or service not known|SSL: CERTIFICATE_VERIFY_FAILED)"),
        ("oom", r"(Out of memory|oom-kill|Cannot allocate memory|MemoryError\b|virtual memory exhausted)"),
        ("disk", r"No space left on device"),
        ("testbed", r"(testbed failure|testbed .*(failed|died)"
                    r"|<VirtSubproc>: failure|lxd.*(error|failed)|nova.*error|setup-testbed.*fail"
                    r"|Hash Sum mismatch|apt-get update failed|Unable to lock the administration directory)"),
        ("buildd", r"(Build killed with signal|session terminated|chroot problem"
                   r"|E: Failed to (execute|lock)|Unable to allocate)"),
    ]
]

APT_DEP_MARKERS = re.compile(
    r"(Unsatisfiable dependencies|unmet dependencies|Depends: .* but .* is to be installed"
    r"|but it is not (going to be )?installable|Broken packages|Impossible to satisfy)", re.I)


@dataclass
class InfraVerdict:
    likely_infra: bool = False
    likely_flaky: bool = False
    reasons: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    baseline_fails: bool | None = None
    history: list[str] = field(default_factory=list)  # outcomes, newest first
    needs_all_proposed: bool = False

    @property
    def flagged(self) -> bool:
        return self.likely_infra or self.likely_flaky

    def to_dict(self) -> dict:
        return dict(self.__dict__) | {"flagged": self.flagged}


def scan_markers(lines: Iterable[str]) -> list[str]:
    found: dict[str, None] = {}
    for line in lines:
        for name, rx in INFRA_MARKERS:
            if name not in found and rx.search(line):
                found[name] = None
    return list(found)


def intermittent(outcomes: list[str]) -> bool:
    """At least two pass<->fail transitions among recent (non-tmpfail) runs."""
    seq = [o for o in outcomes if o in ("pass", "fail")]
    flips = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    return flips >= 2


def assess_test(test_status: str, arch: str, log_lines: list[str] | None,
                history: list[RunResult], failing_arches: set[str], all_arches: set[str],
                trigger_count: int = 1, has_unmigrated_deps: bool = False,
                latest_outcome: str | None = None) -> InfraVerdict:
    v = InfraVerdict()
    v.history = [r.outcome for r in history]
    if latest_outcome is None and history:
        latest_outcome = history[0].outcome
    if latest_outcome == "tmpfail" or test_status.upper() in ("TMPFAIL",):
        v.likely_infra = True
        v.reasons.append("latest result is tmpfail (testbed/infra error)")
    markers = scan_markers(log_lines or [])
    v.markers = markers
    if markers:
        v.likely_infra = True
        v.reasons.append("log contains infra markers: " + ", ".join(markers))
    baseline = [r for r in history if r.is_baseline and r.outcome in ("pass", "fail")]
    if baseline:
        v.baseline_fails = baseline[0].outcome == "fail"
        if v.baseline_fails:
            v.likely_flaky = True
            v.reasons.append(f"also fails with the release-pocket baseline (run {baseline[0].run_id})")
    if intermittent(v.history):
        v.likely_flaky = True
        v.reasons.append("pass/fail history is intermittent: " + " ".join(v.history[:10]))
    if len(failing_arches) == 1 and len(all_arches) > 1:
        if markers or latest_outcome == "tmpfail":
            v.reasons.append(f"fails only on {arch} and looks like infra")
        else:
            v.reasons.append(f"fails only on {arch} (arch-specific?)")
    if log_lines and any(APT_DEP_MARKERS.search(line) for line in log_lines):
        v.needs_all_proposed = True
        v.reasons.append("test dependencies could not be installed; the trigger set may be incomplete")
    elif has_unmigrated_deps and trigger_count <= 1:
        v.needs_all_proposed = True
    return v


def assess_build(log_lines: list[str] | None, failing_arches: set[str],
                 all_arches: set[str]) -> InfraVerdict:
    v = InfraVerdict()
    markers = scan_markers(log_lines or [])
    v.markers = markers
    if markers:
        v.likely_infra = True
        v.reasons.append("build log contains infra markers: " + ", ".join(markers))
        if len(failing_arches) == 1 and len(all_arches) > 1:
            v.reasons.append("single-arch failure that looks like infra")
    return v
