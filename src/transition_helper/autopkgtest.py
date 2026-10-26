"""autopkgtest.ubuntu.com client: results history, queues, retry URLs."""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass, field
from urllib.parse import urlencode

from .cache import HTTP, Cache, FetchError

BASE = "https://autopkgtest.ubuntu.com"
HISTORY_TTL = 30 * 60
QUEUE_TTL = 5 * 60

# autopkgtest exit codes (see autopkgtest(1))
EXIT_PASS = {0, 2, 8}
EXIT_FAIL = {4, 6}
EXIT_TMPFAIL = {16, 20}
EXIT_BADPKG = {12, 14}


def pkg_prefix(pkg: str) -> str:
    return pkg[:4] if pkg.startswith("lib") and len(pkg) > 3 else pkg[0]


def results_container(series: str, ppa: str | None = None) -> str:
    if ppa:
        # private/PPA results live in per-PPA containers; owner-name form
        owner, _, name = ppa.lstrip("~").partition("/ubuntu/")
        return f"autopkgtest-{series}-{owner}-{name}"
    return f"autopkgtest-{series}"


def run_log_url(series: str, arch: str, pkg: str, run_id: str, ppa: str | None = None) -> str:
    return (f"{BASE}/results/{results_container(series, ppa)}/{series}/{arch}/"
            f"{pkg_prefix(pkg)}/{pkg}/{run_id}/log.gz")


def retry_url(series: str, arch: str, pkg: str, triggers: list[str],
              ppa: str | None = None, all_proposed: bool = False) -> str:
    params: list[tuple[str, str]] = [("release", series), ("arch", arch), ("package", pkg)]
    params += [("trigger", t) for t in triggers]
    if ppa:
        params.append(("ppa", ppa.lstrip("~").replace("/ubuntu/", "/")))
    if all_proposed:
        params.append(("all-proposed", "1"))
    return f"{BASE}/request.cgi?{urlencode(params)}"


def outcome(exitcode: int | None) -> str:
    if exitcode is None:
        return "unknown"
    if exitcode in EXIT_PASS:
        return "pass"
    if exitcode in EXIT_FAIL:
        return "fail"
    if exitcode in EXIT_TMPFAIL:
        return "tmpfail"
    if exitcode in EXIT_BADPKG:
        return "badpkg"
    return "fail"


@dataclass
class RunResult:
    run_id: str
    exitcode: int | None
    version: str | None
    triggers: list[str] = field(default_factory=list)
    log_url: str | None = None

    @property
    def outcome(self) -> str:
        return outcome(self.exitcode)

    @property
    def is_baseline(self) -> bool:
        """A run against the release pocket (no trigger from -proposed)."""
        return not self.triggers or any(t.startswith("migration-reference/") for t in self.triggers)

    def to_dict(self) -> dict:
        return dict(self.__dict__) | {"outcome": self.outcome, "is_baseline": self.is_baseline}


def parse_result_tar(data: bytes) -> tuple[int | None, str | None, list[str]]:
    exitcode = version = None
    triggers: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            f = tar.extractfile(member) if member.isfile() else None
            if f is None:
                continue
            content = f.read(1_000_000).decode("utf-8", "replace")
            if member.name.endswith("exitcode"):
                try:
                    exitcode = int(content.strip())
                except ValueError:
                    pass
            elif member.name.endswith("testpkg-version"):
                parts = content.split()
                version = parts[1] if len(parts) > 1 else None
            elif member.name.endswith("testinfo.json"):
                try:
                    info = json.loads(content)
                except json.JSONDecodeError:
                    continue
                for env in info.get("custom_environment", []):
                    if env.startswith("ADT_TEST_TRIGGERS="):
                        triggers = env.split("=", 1)[1].split()
    return exitcode, version, triggers


class AutopkgtestClient:
    def __init__(self, http: HTTP, cache: Cache):
        self.http = http
        self.cache = cache

    def run_ids(self, series: str, arch: str, pkg: str, ppa: str | None = None) -> list[str]:
        """Run ids (oldest first) from the swift container listing."""
        container = results_container(series, ppa)
        prefix = f"{series}/{arch}/{pkg_prefix(pkg)}/{pkg}/"

        def produce():
            ids: list[str] = []
            marker = None
            while True:
                params = {"format": "json", "prefix": prefix}
                if marker:
                    params["marker"] = marker
                listing = self.http.get_json(f"{BASE}/results/{container}/", params=params)
                if not isinstance(listing, list) or not listing:
                    break
                for obj in listing:
                    name = obj.get("name", "") if isinstance(obj, dict) else str(obj)
                    if name.endswith("/result.tar"):
                        ids.append(name[len(prefix):].split("/")[0])
                marker = listing[-1].get("name") if isinstance(listing[-1], dict) else None
                if len(listing) < 1000 or not marker:
                    break
            return ids

        return self.cache.cached_json("autopkgtest", f"ids:{container}:{prefix}", HISTORY_TTL, produce)

    def run_result(self, series: str, arch: str, pkg: str, run_id: str,
                   ppa: str | None = None) -> RunResult:
        container = results_container(series, ppa)
        key = f"run:{container}:{series}/{arch}/{pkg}/{run_id}"

        def produce():
            url = (f"{BASE}/results/{container}/{series}/{arch}/{pkg_prefix(pkg)}/{pkg}/"
                   f"{run_id}/result.tar")
            try:
                exitcode, version, triggers = parse_result_tar(self.http.get_bytes(url))
            except (tarfile.TarError, FileNotFoundError):
                exitcode, version, triggers = None, None, []
            return {"exitcode": exitcode, "version": version, "triggers": triggers}

        # results are immutable
        d = self.cache.cached_json("autopkgtest", key, None, produce)
        return RunResult(run_id=run_id, exitcode=d["exitcode"], version=d["version"],
                         triggers=d["triggers"],
                         log_url=run_log_url(series, arch, pkg, run_id, ppa))

    def history(self, series: str, arch: str, pkg: str, limit: int = 10,
                ppa: str | None = None) -> list[RunResult]:
        """Most recent `limit` runs, newest first."""
        try:
            ids = self.run_ids(series, arch, pkg, ppa)
        except (FetchError, FileNotFoundError):
            return []
        out = []
        for run_id in reversed(ids[-limit:]):
            try:
                out.append(self.run_result(series, arch, pkg, run_id, ppa))
            except FetchError:
                continue
        return out

    def queued(self, series: str, pkg: str) -> list[dict]:
        """Queued + running requests for `pkg` in `series` (all arches)."""
        out: list[dict] = []
        try:
            queues = self.cache.cached_json("autopkgtest", "queues.json", QUEUE_TTL,
                                            lambda: self.http.get_json(f"{BASE}/queues.json"))
            running = self.cache.cached_json("autopkgtest", "running.json", QUEUE_TTL,
                                             lambda: self.http.get_json(f"{BASE}/static/running.json"))
        except (FetchError, FileNotFoundError):
            return out
        for context, by_release in (queues or {}).items():
            for arch, items in ((by_release or {}).get(series) or {}).items():
                for item in items or []:
                    name, _, params = str(item).partition("\n")
                    if name.strip() == pkg:
                        try:
                            p = json.loads(params) if params else {}
                        except json.JSONDecodeError:
                            p = {}
                        out.append({"state": "queued", "queue": context, "arch": arch,
                                    "triggers": p.get("triggers", [])})
        for runs in ((running or {}).get(pkg) or {}).values():
            for release, by_arch in (runs or {}).items():
                if release != series:
                    continue
                for arch, info in (by_arch or {}).items():
                    params = info[0] if isinstance(info, list) and info else {}
                    out.append({"state": "running", "arch": arch,
                                "triggers": params.get("triggers", []) if isinstance(params, dict) else []})
        return out
