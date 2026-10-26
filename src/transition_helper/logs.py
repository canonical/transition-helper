"""Step 5: local log store (gzipped), failure-window extraction, cross-arch dedupe."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .cache import HTTP

LOG_ID_RE = re.compile(r"^(build:[^:/\s]+:[^:/\s]+|test:[^:/\s]+:[^:/\s]+:[^:/\s]+)$")


class LogError(Exception):
    pass


def build_log_id(pkg: str, arch: str) -> str:
    return f"build:{pkg}:{arch}"


def test_log_id(pkg: str, arch: str, run_id: str) -> str:
    return f"test:{pkg}:{arch}:{run_id}"


def log_kind(log_id: str) -> str:
    return "build" if log_id.startswith("build:") else "autopkgtest"


def log_arch(log_id: str) -> str:
    return log_id.split(":")[2]


@dataclass
class LogMeta:
    log_id: str
    url: str
    lines: int
    bytes: int
    version: str | None = None
    note: str | None = None


class LogStore:
    def __init__(self, root: Path, http: HTTP | None = None, refresh: bool = False):
        self.root = root
        self.http = http
        self.refresh = refresh
        root.mkdir(parents=True, exist_ok=True)

    def _file(self, log_id: str) -> Path:
        if not LOG_ID_RE.match(log_id):
            raise LogError(f"invalid log id {log_id!r}")
        return self.root / (log_id.replace(":", "__") + ".log.gz")

    def _meta_file(self, log_id: str) -> Path:
        return self._file(log_id).with_suffix("").with_suffix(".json")

    def has(self, log_id: str) -> bool:
        try:
            return self._file(log_id).exists()
        except LogError:
            return False

    def fetch(self, log_id: str, url: str, version: str | None = None) -> LogMeta:
        path = self._file(log_id)
        if path.exists() and not self.refresh:
            meta = self.meta(log_id)
            if meta:
                return meta
        if self.http is None:
            raise LogError("no HTTP client configured")
        self.http.download(url, path, gzip_output=True)
        return self._write_meta(log_id, url, version)

    def add_text(self, log_id: str, text: str, url: str = "local", version: str | None = None) -> LogMeta:
        """Store a log from memory (tests, fixtures)."""
        path = self._file(log_id)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(text)
        return self._write_meta(log_id, url, version)

    def _write_meta(self, log_id: str, url: str, version: str | None) -> LogMeta:
        n = sum(1 for _ in self.iter_lines(log_id))
        meta = LogMeta(log_id=log_id, url=url, lines=n, bytes=self._file(log_id).stat().st_size,
                       version=version)
        self._meta_file(log_id).write_text(json.dumps(meta.__dict__))
        return meta

    def meta(self, log_id: str) -> LogMeta | None:
        try:
            return LogMeta(**json.loads(self._meta_file(log_id).read_text()))
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def list(self) -> list[LogMeta]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(LogMeta(**json.loads(p.read_text())))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return out

    def iter_lines(self, log_id: str) -> Iterator[str]:
        path = self._file(log_id)
        if not path.exists():
            raise LogError(f"unknown log {log_id!r}; use list_evidence or fetch_log")
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line.rstrip("\n").rstrip("\r")

    # -- model-facing reads -------------------------------------------------
    def read(self, log_id: str, start: int, end: int) -> list[tuple[int, str]]:
        start = max(1, start)
        out = []
        for n, line in enumerate(self.iter_lines(log_id), 1):
            if n > end:
                break
            if n >= start:
                out.append((n, line))
        return out

    def tail(self, log_id: str, n: int) -> list[tuple[int, str]]:
        buf: deque[tuple[int, str]] = deque(maxlen=max(1, n))
        for i, line in enumerate(self.iter_lines(log_id), 1):
            buf.append((i, line))
        return list(buf)

    def grep(self, log_id: str, pattern: str, context: int = 3,
             max_matches: int = 50) -> tuple[list[list[tuple[int, str]]], int]:
        """Streaming grep. Returns (groups of numbered lines, total match count)."""
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise LogError(f"invalid regex: {e}") from e
        context = max(0, min(context, 50))
        groups: list[list[tuple[int, str]]] = []
        before: deque[tuple[int, str]] = deque(maxlen=context)
        current: list[tuple[int, str]] | None = None
        after_left = 0
        total = 0
        for n, line in enumerate(self.iter_lines(log_id), 1):
            is_match = bool(rx.search(line))
            if is_match:
                total += 1
            if is_match and total <= max_matches:
                if current is None:
                    current = list(before)
                    groups.append(current)
                current.append((n, line))
                after_left = context
                before.clear()
            elif current is not None and after_left > 0:
                current.append((n, line))
                after_left -= 1
            else:
                current = None
                before.append((n, line))
        return groups, total


# -- failure windows ------------------------------------------------------------

BUILD_ERROR_RE = re.compile(
    r"(\berror:|\bError \d+\b|\*\*\* \[|^E: |dh_\S+: error|dpkg-buildpackage: error"
    r"|^\s*(FAILED|FAIL:|ERROR:)|Traceback \(most recent call last\)|\bModuleNotFoundError\b"
    r"|\bImportError\b|undefined reference to|fatal error:|cannot find -l|No such file or directory$"
    r"|CMake Error|configure: error|ld returned \d+ exit status|SyntaxError:|AttributeError:"
    r"|=+ FAILURES =+|=+ \d+ failed)")
TEST_ERROR_RE = re.compile(
    r"(\bFAIL\b|\bFAILED\b|\bERROR\b|Traceback \(most recent call last\)|\bError\b|badpkg"
    r"|\bModuleNotFoundError\b|\bImportError\b|SyntaxError:|AttributeError:|Segmentation fault"
    r"|non-zero exit status|stderr:|Assertion|assert )")
TEST_SUMMARY_RE = re.compile(r"^\S+\s+(PASS|FAIL|SKIP|FLAKY)\b.*$")
# noise that matches the error regexes but carries no signal
IGNORE_RE = re.compile(r"(-Werror=|-Wno-error|Werror=format-security|error\.c|error\.h|errors\.py"
                       r"|test_error|_error\.|on_error|\berror_|\bERROR=|Wno-)", re.I)


@dataclass
class Window:
    start: int
    end: int
    trigger_lines: list[tuple[int, str]]
    lines: list[tuple[int, str]] = field(default_factory=list)

    def render(self, max_line_len: int = 600) -> str:
        return "\n".join(f"{n:>7}: {line if len(line) <= max_line_len else line[:max_line_len] + ' […]'}"
                         for n, line in self.lines)

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end,
                "trigger_lines": [list(t) for t in self.trigger_lines]}


def failure_windows(store: LogStore, log_id: str, before: int = 25, after: int = 8,
                    max_windows: int = 4) -> list[Window]:
    kind = log_kind(log_id)
    rx = BUILD_ERROR_RE if kind == "build" else TEST_ERROR_RE
    hits: list[tuple[int, str]] = []
    summary: list[tuple[int, str]] = []
    total = 0
    for n, line in enumerate(store.iter_lines(log_id), 1):
        total = n
        if kind != "build" and TEST_SUMMARY_RE.match(line) and " FAIL" in line:
            summary.append((n, line))
        if rx.search(line) and not IGNORE_RE.search(line):
            hits.append((n, line))
    if not hits and not summary:
        if total == 0:
            return []
        # nothing recognisable: fall back to the tail
        start = max(1, total - before - after)
        return [_fill(store, log_id, [Window(start, total, [])])[0]]

    # cluster hits that are close together
    clusters: list[list[tuple[int, str]]] = []
    for h in hits:
        if clusters and h[0] - clusters[-1][-1][0] <= before:
            clusters[-1].append(h)
        else:
            clusters.append([h])
    # first cluster usually holds the root cause, last one the final failure
    chosen = clusters if len(clusters) <= max_windows else clusters[: max_windows - 1] + clusters[-1:]
    windows = [Window(max(1, c[0][0] - before), min(total, c[-1][0] + after), c[:20]) for c in chosen]
    if summary:
        s0, s1 = summary[0][0], summary[-1][0]
        windows.append(Window(max(1, s0 - 2), min(total, s1 + 2), summary[:20]))
    windows.sort(key=lambda w: w.start)
    merged: list[Window] = []
    for w in windows:
        if merged and w.start <= merged[-1].end + 1:
            merged[-1].end = max(merged[-1].end, w.end)
            merged[-1].trigger_lines.extend(t for t in w.trigger_lines
                                            if t not in merged[-1].trigger_lines)
        else:
            merged.append(w)
    # cap window size so a pathological log doesn't produce megabyte excerpts
    for w in merged:
        if w.end - w.start > 400:
            w.end = w.start + 400
    return _fill(store, log_id, merged)


def _fill(store: LogStore, log_id: str, windows: list[Window]) -> list[Window]:
    idx = 0
    windows = sorted(windows, key=lambda w: w.start)
    for n, line in enumerate(store.iter_lines(log_id), 1):
        while idx < len(windows) and n > windows[idx].end:
            idx += 1
        if idx >= len(windows):
            break
        if n >= windows[idx].start:
            windows[idx].lines.append((n, line))
    return windows


# -- normalisation and dedupe -----------------------------------------------------

ARCH_WORDS = (r"\b(amd64v3|amd64|arm64|armhf|i386|ppc64el|ppc64le|s390x|riscv64|x86_64|aarch64|"
              r"i686|powerpc64le|arm-linux-gnueabihf|x86_64-linux-gnu|aarch64-linux-gnu|"
              r"powerpc64le-linux-gnu|s390x-linux-gnu|riscv64-linux-gnu|i686-linux-gnu|"
              r"i386-linux-gnu)\b")
NORMALISERS = [
    (re.compile(r"^\s*\d+s\s+"), ""),  # autopkgtest elapsed-time prefix
    (re.compile(r"^\S+ \d\d:\d\d:\d\d(\.\d+)?\s*"), ""),
    (re.compile(r"\d\d:\d\d:\d\d(\.\d+)?"), "TIME"),
    (re.compile(r"/<<PKGBUILDDIR>>|/build/[^/\s]+/[^/\s]+|/tmp/[^\s:'\"]+|/var/tmp/[^\s:'\"]+"), "DIR"),
    (re.compile(r"0x[0-9a-fA-F]+"), "ADDR"),
    (re.compile(ARCH_WORDS), "ARCH"),
    (re.compile(r"\b\d+(\.\d+)*\b"), "N"),
    (re.compile(r"\s+"), " "),
]


def normalise(line: str) -> str:
    for rx, repl in NORMALISERS:
        line = rx.sub(repl, line)
    return line.strip()


def failure_signature(windows: list[Window]) -> str:
    lines = sorted({normalise(t[1]) for w in windows for t in w.trigger_lines})
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()[:12]


ARCH_PREFERENCE = ["amd64", "arm64", "amd64v3", "ppc64el", "s390x", "armhf", "riscv64", "i386"]


def arch_rank(arch: str) -> int:
    return ARCH_PREFERENCE.index(arch) if arch in ARCH_PREFERENCE else len(ARCH_PREFERENCE)


@dataclass
class FailureGroup:
    key: str
    representative: str  # log id
    members: list[str]
    kb_ids: list[str]

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def dedupe(logs: dict[str, tuple[str, list[str]]]) -> list[FailureGroup]:
    """Group identical failures. `logs` maps log_id -> (signature, kb_ids).

    Logs are grouped by (package, kind, kb ids or signature); the preferred arch
    is the representative."""
    groups: dict[str, FailureGroup] = {}
    for log_id, (sig, kb_ids) in logs.items():
        parts = log_id.split(":")
        key_basis = ",".join(sorted(kb_ids)) if kb_ids else sig
        key = f"{parts[0]}:{parts[1]}:{key_basis}"
        g = groups.get(key)
        if g is None:
            groups[key] = FailureGroup(key=key, representative=log_id, members=[log_id],
                                       kb_ids=sorted(kb_ids))
        else:
            g.members.append(log_id)
            if arch_rank(log_arch(log_id)) < arch_rank(log_arch(g.representative)):
                g.representative = log_id
    return list(groups.values())
