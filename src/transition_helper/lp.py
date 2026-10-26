"""Launchpad access. Everything returns plain dataclasses so the pipeline can be
tested against fakes (see tests/fakes.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .versions import vkey

ACTIVE_STATUSES = ("Pending", "Published")
OPEN_BUG_STATUSES = [
    "New",
    "Incomplete",
    "Confirmed",
    "Triaged",
    "In Progress",
    "Fix Committed",
]

# buildstate strings as reported by Launchpad
BUILD_OK = "Successfully built"
BUILD_FAILED = "Failed to build"
BUILD_DEPWAIT = "Dependency wait"
BUILD_SUPERSEDED = "Build for superseded Source"
BUILD_PENDING_STATES = (
    "Needs building",
    "Currently building",
    "Gathering build output",
    "Uploading build",
)
BUILD_INFRA_STATES = (
    "Chroot problem",
    "Failed to upload",
    "Cancelled build",
    "Cancelling build",
)


class LPError(Exception):
    pass


@dataclass
class SourcePub:
    name: str
    version: str
    location: str  # "ppa" | "proposed" | "release" | "other"
    pocket: str
    status: str
    component: str | None = None
    date_published: str | None = None
    web_link: str | None = None
    raw: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "raw"}


@dataclass
class BuildRec:
    arch: str
    state: str
    log_url: str | None = None
    web_link: str | None = None
    dependencies: str | None = None
    upload_log_url: str | None = None
    datebuilt: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class BugInfo:
    id: int
    title: str
    status: str
    tags: list[str]
    web_link: str
    importance: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class PPAInfo:
    reference: str
    private: bool
    web_link: str | None = None


class LPClient(Protocol):
    def series_exists(self, series: str) -> bool: ...
    def ppa_info(self, ppa: str) -> PPAInfo: ...
    def source_exists(self, pkg: str, series: str) -> bool: ...
    def published_sources(self, pkg: str, series: str, ppa: str | None = None) -> list[SourcePub]: ...
    def builds(self, pub: SourcePub) -> list[BuildRec]: ...
    def went_through_proposed(self, pkg: str, series: str, version: str) -> bool: ...
    def source_file_urls(self, pkg: str, version: str, ppa: str | None = None) -> list[str]: ...
    def binary_source(self, binary: str, series: str, arch: str) -> str | None: ...
    def open_bugs(self, pkg: str) -> list[BugInfo]: ...
    def create_bug(self, pkg: str, title: str, description: str, tags: list[str],
                   attachments: list[tuple[str, bytes, str]]) -> BugInfo: ...
    def attach(self, bug_id: int, filename: str, data: bytes, comment: str) -> None: ...


def pocket_location(pocket: str) -> str:
    return {"Proposed": "proposed", "Release": "release"}.get(pocket, "other")


class LaunchpadClient:
    """launchpadlib-backed implementation of LPClient."""

    def __init__(self, cache_dir: Path, credentials_file: Path | None = None,
                 service_root: str = "production"):
        self.cache_dir = cache_dir
        self.credentials_file = credentials_file
        self.service_root = service_root
        self._lp = None
        self._lp_auth = None
        self._series: dict[str, Any] = {}
        self._ppas: dict[str, Any] = {}

    # -- connection -------------------------------------------------------
    @property
    def lp(self):
        if self._lp is None:
            if self.credentials_file:
                self._lp = self._login()
            else:
                from launchpadlib.launchpad import Launchpad

                try:
                    self._lp = Launchpad.login_anonymously(
                        "transition-helper", self.service_root,
                        str(self.cache_dir / "launchpadlib"), version="devel",
                    )
                except Exception as e:  # launchpadlib raises many types
                    raise LPError(f"cannot reach Launchpad: {e}") from e
        return self._lp

    def _login(self):
        from launchpadlib.launchpad import Launchpad

        if self._lp_auth is None:
            kwargs = {}
            if self.credentials_file:
                kwargs["credentials_file"] = str(self.credentials_file)
            try:
                self._lp_auth = Launchpad.login_with(
                    "transition-helper", self.service_root,
                    str(self.cache_dir / "launchpadlib"), version="devel", **kwargs,
                )
            except Exception as e:
                raise LPError(f"Launchpad login failed: {e}") from e
        return self._lp_auth

    def authenticated(self):
        """An authenticated Launchpad handle (only used for bug creation)."""
        return self._login()

    @property
    def ubuntu(self):
        return self.lp.distributions["ubuntu"]

    def _get_series(self, series: str):
        if series not in self._series:
            try:
                self._series[series] = self.ubuntu.getSeries(name_or_version=series)
            except Exception as e:
                raise LPError(f"unknown series {series!r}: {e}") from e
        return self._series[series]

    def _get_ppa(self, ppa: str):
        if ppa not in self._ppas:
            try:
                archive = self.lp.archives.getByReference(reference=ppa)
            except Exception as e:
                raise LPError(f"cannot look up PPA {ppa}: {e}") from e
            if archive is None:
                raise LPError(f"PPA {ppa} not found (or not visible with current credentials)")
            self._ppas[ppa] = archive
        return self._ppas[ppa]

    # -- queries -------------------------------------------------------------
    def series_exists(self, series: str) -> bool:
        try:
            self._get_series(series)
            return True
        except LPError:
            return False

    def current_series(self) -> str:
        return self.ubuntu.current_series.name

    def ppa_info(self, ppa: str) -> PPAInfo:
        a = self._get_ppa(ppa)
        return PPAInfo(reference=ppa, private=bool(a.private), web_link=a.web_link)

    def source_exists(self, pkg: str, series: str) -> bool:
        pubs = self.ubuntu.main_archive.getPublishedSources(
            source_name=pkg, exact_match=True, distro_series=self._get_series(series)
        )
        return len(pubs[:1]) > 0

    def _pub(self, p, location: str | None) -> SourcePub:
        return SourcePub(
            name=p.source_package_name,
            version=p.source_package_version,
            location=location or pocket_location(p.pocket),
            pocket=p.pocket,
            status=p.status,
            component=p.component_name,
            date_published=str(p.date_published) if p.date_published else None,
            web_link=getattr(p, "web_link", None),
            raw=p,
        )

    def published_sources(self, pkg: str, series: str, ppa: str | None = None) -> list[SourcePub]:
        s = self._get_series(series)
        out: list[SourcePub] = []
        try:
            for status in ACTIVE_STATUSES:
                for p in self.ubuntu.main_archive.getPublishedSources(
                    source_name=pkg, exact_match=True, distro_series=s, status=status
                ):
                    out.append(self._pub(p, None))
                if ppa:
                    for p in self._get_ppa(ppa).getPublishedSources(
                        source_name=pkg, exact_match=True, distro_series=s, status=status
                    ):
                        out.append(self._pub(p, "ppa"))
        except LPError:
            raise
        except Exception as e:
            raise LPError(f"getPublishedSources({pkg}): {e}") from e
        out.sort(key=lambda p: vkey(p.version), reverse=True)
        return out

    def builds(self, pub: SourcePub) -> list[BuildRec]:
        if pub.raw is None:
            return []
        try:
            raw_builds = list(pub.raw.getBuilds())
        except Exception as e:
            raise LPError(f"getBuilds({pub.name} {pub.version}): {e}") from e
        return [
            BuildRec(
                arch=b.arch_tag,
                state=b.buildstate,
                log_url=b.build_log_url,
                web_link=b.web_link,
                dependencies=b.dependencies,
                upload_log_url=getattr(b, "upload_log_url", None),
                datebuilt=str(b.datebuilt) if b.datebuilt else None,
            )
            for b in raw_builds
        ]

    def went_through_proposed(self, pkg: str, series: str, version: str) -> bool:
        pubs = self.ubuntu.main_archive.getPublishedSources(
            source_name=pkg, exact_match=True, version=version,
            distro_series=self._get_series(series), pocket="Proposed",
        )
        return len(pubs[:1]) > 0

    def source_file_urls(self, pkg: str, version: str, ppa: str | None = None) -> list[str]:
        archives = []
        if ppa:
            archives.append(self._get_ppa(ppa))
        archives.append(self.ubuntu.main_archive)
        for archive in archives:
            pubs = archive.getPublishedSources(source_name=pkg, exact_match=True, version=version)
            for p in pubs[:1]:
                return list(p.sourceFileUrls())
        raise LPError(f"no publication of {pkg} {version} found")

    def binary_source(self, binary: str, series: str, arch: str) -> str | None:
        try:
            das = self._get_series(series).getDistroArchSeries(archtag=arch)
            for b in self.ubuntu.main_archive.getPublishedBinaries(
                binary_name=binary, exact_match=True, distro_arch_series=das
            )[:1]:
                return b.source_package_name
        except Exception:
            return None
        return None

    def open_bugs(self, pkg: str) -> list[BugInfo]:
        try:
            tasks = self.ubuntu.getSourcePackage(name=pkg).searchTasks(status=OPEN_BUG_STATUSES)
            out = []
            for t in tasks[:100]:
                bug = t.bug
                out.append(BugInfo(id=bug.id, title=bug.title, status=t.status,
                                   tags=list(bug.tags), web_link=bug.web_link,
                                   importance=t.importance))
            return out
        except Exception as e:
            raise LPError(f"bug search for {pkg}: {e}") from e

    def create_bug(self, pkg: str, title: str, description: str, tags: list[str],
                   attachments: list[tuple[str, bytes, str]]) -> BugInfo:
        lp = self.authenticated()
        try:
            target = lp.distributions["ubuntu"].getSourcePackage(name=pkg)
            bug = lp.bugs.createBug(target=target, title=title, description=description, tags=tags)
            for filename, data, comment in attachments:
                bug.addAttachment(comment=comment, data=data, filename=filename,
                                  content_type="text/plain", is_patch=True)
        except Exception as e:
            raise LPError(f"bug creation failed: {e}") from e
        return BugInfo(id=bug.id, title=bug.title, status="New", tags=list(bug.tags),
                       web_link=bug.web_link)

    def attach(self, bug_id: int, filename: str, data: bytes, comment: str) -> None:
        try:
            bug = self.authenticated().bugs[bug_id]
            bug.addAttachment(comment=comment, data=data, filename=filename,
                              content_type="text/plain", is_patch=True)
        except Exception as e:
            raise LPError(f"attach to LP: #{bug_id}: {e}") from e
