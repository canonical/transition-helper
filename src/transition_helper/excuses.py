"""Tolerant parser for britney's update_excuses.yaml."""

from __future__ import annotations

import html
import lzma
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from .cache import HTTP, Cache, FetchError

EXCUSES_URL = "https://ubuntu-archive-team.ubuntu.com/proposed-migration/{series}/update_excuses.yaml.xz"
# britney regenerates excuses regularly; older than this means it is likely stuck
EXCUSES_STALE_HOURS = 30

TAG_RE = re.compile(r"<[^>]+>")
IMPOSSIBLE_RE = re.compile(r"Impossible Depends: (\S+) -> ([^/\s]+)/([^/\s]+)/(\S+)")
UNSAT_RE = re.compile(r"^(\S+)/(\S+) has unsatisfiable dependency")
COMPONENT_RE = re.compile(r"^(\S+)/(\S+) in (\S+) cannot depend on (\S+) in (\S+)")

# autopkgtest result strings that mean "this blocks migration"
FAIL_STATUSES = {"REGRESSION"}
PENDING_STATUSES = {"RUNNING", "RUNNING-REFERENCE"}
# informative but non-blocking
NONBLOCKING_STATUSES = {"PASS", "ALWAYSFAIL", "NEUTRAL", "OLD_PASS", "OLD_NEUTRAL",
                        "IGNORE-FAIL", "RUNNING-ALWAYSFAIL", "OLD_FAIL"}


class ExcusesError(Exception):
    pass


def strip_html(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text)).replace("♻", "").strip()


@dataclass
class TestResult:
    test_pkg: str
    test_version: str | None
    arch: str
    status: str
    log_url: str | None
    history_url: str | None
    retry_url: str | None
    triggers: list[str] = field(default_factory=list)

    @property
    def run_id(self) -> str | None:
        # .../<series>/<arch>/<p>/<pkg>/<run_id>/log.gz
        if self.log_url and self.log_url.endswith("/log.gz"):
            return self.log_url.rstrip("/").split("/")[-2]
        return None

    def to_dict(self) -> dict:
        return dict(self.__dict__) | {"run_id": self.run_id}


@dataclass
class Excuse:
    source: str
    item_name: str
    old_version: str | None
    new_version: str | None
    verdict: str
    reasons: list[str]
    is_candidate: bool
    lines: list[str]
    tests: list[TestResult]
    autopkgtest_verdict: str | None
    depends_verdict: str | None
    missing_builds: list[str]
    missing_builds_unimportant: list[str]
    blocked_by: list[str]
    migrate_after: list[str]
    impossible_depends: list[tuple[str, str, str]]  # (binary, version, arch)
    unsatisfiable: list[tuple[str, str]]  # (binary, arch)
    component_mismatch: list[str]
    block_verdict: str | None
    block_bugs: list[str]
    block_bugs_verdict: str | None
    policy_verdicts: dict[str, str]
    invalidated_by_other_package: bool = False

    def failing_tests(self) -> list[TestResult]:
        return [t for t in self.tests if t.status in FAIL_STATUSES]

    def pending_tests(self) -> list[TestResult]:
        return [t for t in self.tests if t.status in PENDING_STATUSES]

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["tests"] = [t.to_dict() for t in self.tests]
        return d

    def summary(self) -> str:
        out = [f"excuse for {self.item_name} ({self.old_version} -> {self.new_version}): "
               f"verdict {self.verdict}; reasons {', '.join(self.reasons) or '-'}"]
        out.extend("  " + line for line in self.lines if not line.startswith("autopkgtest for "))
        for t in self.tests:
            if t.status not in ("PASS", "OLD_PASS"):
                out.append(f"  test {t.test_pkg} {t.test_version or ''} {t.arch}: {t.status} {t.log_url or ''}")
        return "\n".join(out)


def _triggers_from_retry(url: str | None) -> list[str]:
    if not url:
        return []
    try:
        return parse_qs(urlparse(url).query).get("trigger", [])
    except ValueError:
        return []


def _str_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return []


def parse_tests(source: str, new_version: str | None, autopkgtest: dict) -> list[TestResult]:
    out: list[TestResult] = []
    if not isinstance(autopkgtest, dict):
        return out
    for key, per_arch in autopkgtest.items():
        if key == "verdict" or not isinstance(per_arch, dict):
            continue
        test_pkg, _, test_version = str(key).partition("/")
        for arch, info in per_arch.items():
            if not isinstance(info, list) or not info:
                continue
            info = list(info) + [None] * (5 - len(info))
            status, log_url, history_url, _, retry_url = info[:5]
            triggers = _triggers_from_retry(retry_url)
            if not triggers and new_version:
                triggers = [f"{source}/{new_version}"]
            out.append(TestResult(
                test_pkg=test_pkg, test_version=test_version or None, arch=str(arch),
                status=str(status), log_url=log_url if log_url and "/running" not in log_url else None,
                history_url=history_url, retry_url=retry_url, triggers=triggers,
            ))
    return out


def parse_excuse(entry: dict) -> Excuse:
    if not isinstance(entry, dict):
        raise ExcusesError(f"excuse entry is not a mapping: {entry!r:.80}")
    source = str(entry.get("source") or entry.get("item-name") or "")
    item = str(entry.get("item-name") or source)
    if not source:
        raise ExcusesError("excuse entry without source/item-name")
    policy = entry.get("policy_info") or {}
    if not isinstance(policy, dict):
        policy = {}
    new_version = entry.get("new-version")
    new_version = None if new_version in (None, "-") else str(new_version)
    old_version = entry.get("old-version")
    old_version = None if old_version in (None, "-") else str(old_version)
    lines = [strip_html(str(x)) for x in entry.get("excuses") or []]

    impossible, unsat, component = [], [], []
    for line in lines:
        if m := IMPOSSIBLE_RE.search(line):
            impossible.append((m.group(2), m.group(3), m.group(4)))
        elif m := UNSAT_RE.search(line):
            unsat.append((m.group(1), m.group(2)))
        elif COMPONENT_RE.search(line):
            component.append(line)

    deps = entry.get("dependencies") or {}
    if not isinstance(deps, dict):
        deps = {}
    missing = entry.get("missing-builds") or {}
    if not isinstance(missing, dict):
        missing = {}
    block_bugs = policy.get("block-bugs") or {}
    verdicts = {k: str(v.get("verdict")) for k, v in policy.items()
                if isinstance(v, dict) and "verdict" in v}
    autopkgtest = policy.get("autopkgtest") or {}

    return Excuse(
        source=source,
        item_name=item,
        old_version=old_version,
        new_version=new_version,
        verdict=str(entry.get("migration-policy-verdict") or ""),
        reasons=_str_list(entry.get("reason")),
        is_candidate=bool(entry.get("is-candidate")),
        lines=lines,
        tests=parse_tests(source, new_version, autopkgtest),
        autopkgtest_verdict=verdicts.get("autopkgtest"),
        depends_verdict=verdicts.get("depends"),
        missing_builds=_str_list(missing.get("on-architectures")),
        missing_builds_unimportant=_str_list(missing.get("on-unimportant-architectures")),
        blocked_by=_str_list(deps.get("blocked-by")),
        migrate_after=_str_list(deps.get("migrate-after")),
        impossible_depends=sorted(set(impossible)),
        unsatisfiable=sorted(set(unsat)),
        component_mismatch=component,
        block_verdict=verdicts.get("block"),
        block_bugs=[k for k in block_bugs if k != "verdict"] if isinstance(block_bugs, dict) else [],
        block_bugs_verdict=verdicts.get("block-bugs"),
        policy_verdicts=verdicts,
        invalidated_by_other_package=bool(entry.get("invalidated-by-other-package")),
    )


class ExcusesIndex:
    def __init__(self, excuses: dict[str, Excuse], generated: str | None = None,
                 errors: list[str] | None = None):
        self.excuses = excuses
        self.generated = generated
        self.errors = errors or []

    def age_hours(self, now: datetime | None = None) -> float | None:
        """Hours since britney generated this file (generated-date is UTC)."""
        try:
            gen = datetime.fromisoformat(str(self.generated)).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return ((now or datetime.now(timezone.utc)) - gen).total_seconds() / 3600

    def get(self, pkg: str) -> Excuse | None:
        return self.excuses.get(pkg)

    def __contains__(self, pkg: str) -> bool:
        return pkg in self.excuses

    @classmethod
    def from_data(cls, data) -> "ExcusesIndex":
        if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
            raise ExcusesError("update_excuses.yaml: expected a mapping with a 'sources' list "
                               "(has the format changed?)")
        out: dict[str, Excuse] = {}
        errors: list[str] = []
        for entry in data["sources"]:
            try:
                e = parse_excuse(entry)
            except ExcusesError as err:
                errors.append(str(err))
                continue
            # item names like "pkg/arch" (binNMU-ish) or "-pkg" (removal) are not source uploads
            if e.item_name == e.source:
                out[e.source] = e
            else:
                out.setdefault(e.item_name, e)
        return cls(out, str(data.get("generated-date") or ""), errors)

    @classmethod
    def from_file(cls, path: Path) -> "ExcusesIndex":
        opener = lzma.open if path.suffix == ".xz" else open
        with opener(path, "rt") as f:
            try:
                data = yaml.load(f, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
            except yaml.YAMLError as e:
                raise ExcusesError(f"{path}: invalid YAML: {e}") from e
        return cls.from_data(data)


def _last_modified(headers: dict[str, str]) -> float | None:
    try:
        return parsedate_to_datetime(headers["last-modified"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def load_excuses(cache: Cache, http: HTTP, series: str, warn=None) -> ExcusesIndex:
    """Cached excuses, re-downloaded only when the server has a newer file.

    We remember the Last-Modified time of the copy we downloaded; each load asks the
    server (HEAD) for its Last-Modified and downloads only if that is newer. Publish
    times vary, so there is no fixed TTL. The server ignores conditional GETs, hence
    HEAD. If the server cannot be reached, the cached copy is used (with a warning)."""
    url = EXCUSES_URL.format(series=series)
    path = cache.path("excuses", f"{series}_update_excuses.yaml.xz")
    meta_key = f"{series}_meta"
    meta = cache.get_json("excuses", meta_key, None) or {}
    have = path.exists() and not cache.refresh
    try:
        remote = _last_modified(http.head(url))
    except (FetchError, FileNotFoundError) as e:
        if not have:
            # nothing cached: the GET itself may still work (e.g. HEAD blocked)
            http.download(url, path)
            return ExcusesIndex.from_file(path)
        if warn:
            warn(f"cannot check for newer excuses ({e}); using the cached copy")
        return ExcusesIndex.from_file(path)
    local = meta.get("last_modified")
    if not have or remote is None or local is None or remote > local:
        http.download(url, path)
        cache.put_json("excuses", meta_key, {"last_modified": remote})
    return ExcusesIndex.from_file(path)
