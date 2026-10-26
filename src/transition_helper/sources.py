"""Source retrieval: exact version from LP, dpkg-source -x, git init + commit."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .cache import HTTP
from .lp import LPClient
from .progress import Progress

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
VERSION_RE = re.compile(r"^[A-Za-z0-9.+~:-]+$")


class SourceError(Exception):
    pass


def git_env() -> dict:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "transition-helper", "GIT_AUTHOR_EMAIL": "th@localhost",
        "GIT_COMMITTER_NAME": "transition-helper", "GIT_COMMITTER_EMAIL": "th@localhost",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, env: dict | None = None,
        input: str | None = None) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env or git_env(),
                           input=input)
    except FileNotFoundError as e:
        raise SourceError(f"{cmd[0]} not found on PATH") from e
    if check and p.returncode != 0:
        raise SourceError(f"{' '.join(cmd)} failed ({p.returncode}): {(p.stderr or p.stdout)[-2000:]}")
    return p


def tree_name(pkg: str, version: str) -> str:
    # epochs are not part of file names
    return f"{pkg}_{version.split(':', 1)[-1]}"


@dataclass
class SourceTree:
    pkg: str
    version: str
    root: Path
    format: str

    @property
    def is_quilt(self) -> bool:
        return self.format.strip() == "3.0 (quilt)"

    @property
    def is_native(self) -> bool:
        return "native" in self.format or self.format.strip() == "1.0" and not any(
            self.root.glob("debian/patches/series"))

    def to_dict(self) -> dict:
        return {"pkg": self.pkg, "version": self.version, "root": str(self.root),
                "format": self.format}


def read_format(root: Path) -> str:
    f = root / "debian" / "source" / "format"
    try:
        return f.read_text().strip() or "1.0"
    except OSError:
        return "1.0"


class SourceManager:
    def __init__(self, cache_dir: Path, lp: LPClient | None, http: HTTP | None,
                 progress: Progress, ppa: str | None = None):
        self.root = cache_dir / "sources"
        self.downloads = cache_dir / "source-downloads"
        self.lp = lp
        self.http = http
        self.progress = progress
        self.ppa = ppa
        self.root.mkdir(parents=True, exist_ok=True)

    def tree_path(self, pkg: str, version: str) -> Path:
        return self.root / tree_name(pkg, version)

    def existing(self, pkg: str, version: str) -> SourceTree | None:
        path = self.tree_path(pkg, version)
        if (path / ".git").is_dir() and (path / "debian").is_dir():
            return SourceTree(pkg, version, path, read_format(path))
        return None

    def get(self, pkg: str, version: str) -> SourceTree:
        if not NAME_RE.match(pkg) or not VERSION_RE.match(version):
            raise SourceError(f"invalid package/version {pkg!r} {version!r}")
        tree = self.existing(pkg, version)
        if tree:
            return tree
        if self.lp is None or self.http is None:
            raise SourceError("no Launchpad/HTTP access configured")
        urls = self.lp.source_file_urls(pkg, version, ppa=self.ppa)
        dl = self.downloads / tree_name(pkg, version)
        dl.mkdir(parents=True, exist_ok=True)
        dsc = None
        for url in urls:
            name = url.rsplit("/", 1)[-1]
            dest = dl / name
            if not dest.exists():
                self.http.download(url, dest)
            if name.endswith(".dsc"):
                dsc = dest
        if dsc is None:
            raise SourceError(f"no .dsc among source files of {pkg} {version}")
        return self.unpack(pkg, version, dsc)

    def unpack(self, pkg: str, version: str, dsc: Path) -> SourceTree:
        path = self.tree_path(pkg, version)
        if path.exists():
            shutil.rmtree(path)
        tmp = path.with_name(path.name + ".unpack")
        if tmp.exists():
            shutil.rmtree(tmp)
        # dpkg-source verifies checksums; an unknown signing key is only a warning
        run(["dpkg-source", "--no-copy", "-x", str(dsc), str(tmp)], cwd=dsc.parent,
            env=dict(os.environ))
        init_repo(tmp, f"{pkg} {version} as unpacked by dpkg-source -x")
        tmp.rename(path)
        return SourceTree(pkg, version, path, read_format(path))


def init_repo(path: Path, message: str) -> None:
    for sub in path.rglob(".git"):
        # nested repos in upstream tarballs would turn into submodules
        if sub != path / ".git" and sub.is_dir():
            shutil.rmtree(sub)
    run(["git", "init", "-q", "-b", "main"], cwd=path)
    run(["git", "add", "-A", "-f", "."], cwd=path)
    run(["git", "commit", "-q", "--no-verify", "-m", message, "--allow-empty"], cwd=path)
