from __future__ import annotations

import gzip
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from transition_helper.workspace import Transition, create_workspace

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def transition() -> Transition:
    return Transition(name="python3.15", series="tenacious", toolchain_source="python3-defaults",
                      old_version="3.14", new_version="3.15", build_location="archive",
                      relevant_binaries=["python3-all-dev"], ignored_arches=["riscv64"],
                      lp_bug_tags=["python3.15"], docs=[])


@pytest.fixture
def workspace(tmp_path, transition):
    return create_workspace(tmp_path / "ws", transition)


def _git_env():
    from transition_helper.sources import git_env

    return git_env()


def make_source_package(dest: Path, name: str, version: str, fmt: str = "3.0 (quilt)",
                        files: dict[str, str] | None = None,
                        patches: dict[str, str] | None = None) -> Path:
    """Build a tiny real source package with dpkg-source -b; returns the .dsc path."""
    upstream = version.split(":")[-1].rsplit("-", 1)[0] if "native" not in fmt else version
    src = dest / f"build-{name}" / f"{name}-{upstream}"
    src.mkdir(parents=True)
    files = files or {"src/hello.py": "import sys\n\n\ndef main():\n    print('hello')\n"}
    for rel, content in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if "native" not in fmt:
        subprocess.run(["tar", "-czf", str(src.parent / f"{name}_{upstream}.orig.tar.gz"),
                        "-C", str(src.parent), src.name], check=True)
    deb = src / "debian"
    (deb / "source").mkdir(parents=True)
    (deb / "source" / "format").write_text(fmt + "\n")
    (deb / "changelog").write_text(textwrap.dedent(f"""\
        {name} ({version}) unstable; urgency=medium

          * Initial release.

         -- Test <test@example.com>  Mon, 01 Jan 2024 00:00:00 +0000
        """))
    (deb / "control").write_text(textwrap.dedent(f"""\
        Source: {name}
        Maintainer: Test <test@example.com>
        Build-Depends: debhelper-compat (= 13)

        Package: {name}
        Architecture: all
        Description: test
         test
        """))
    (deb / "rules").write_text("#!/usr/bin/make -f\n%:\n\tdh $@\n")
    if patches:
        (deb / "patches").mkdir()
        (deb / "patches" / "series").write_text("".join(f"{n}\n" for n in patches))
        for n, body in patches.items():
            (deb / "patches" / n).write_text(body)
    subprocess.run(["dpkg-source", "-b", src.name], cwd=src.parent, check=True,
                   capture_output=True)
    dsc = next(src.parent.glob(f"{name}_*.dsc"))
    return dsc


@pytest.fixture
def make_pkg(tmp_path):
    def _make(name="foo", version="1.0-1", **kw):
        return make_source_package(tmp_path / "pkgsrc", name, version, **kw)
    return _make


requires_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("dpkg-source", "quilt", "git", "patch")),
    reason="needs dpkg-dev, quilt, git and patch")


def gz_text(path: Path) -> str:
    with gzip.open(path, "rt") as f:
        return f.read()
