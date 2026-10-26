"""Debian version arithmetic and changelog stanza generation."""

from __future__ import annotations

import re
import textwrap
from email.utils import format_datetime
from datetime import datetime, timezone

from debian.debian_support import Version

PPA_SUFFIX_RE = re.compile(r"~ppa[0-9.]*$")
BUILD_RE = re.compile(r"build\d+$")
UBUNTU_RE = re.compile(r"ubuntu(\d+)(?:\.\d+)*$")


def strip_ppa(version: str) -> str:
    return PPA_SUFFIX_RE.sub("", version)


def vkey(version: str) -> Version:
    """Comparison key for a version, ignoring any ~ppaN suffix."""
    return Version(strip_ppa(version))


def newest(versions) -> str | None:
    versions = [v for v in versions if v]
    if not versions:
        return None
    return max(versions, key=vkey)


def is_native(version: str) -> bool:
    upstream = Version(strip_ppa(version)).upstream_version
    return Version(strip_ppa(version)).debian_revision is None and bool(upstream)


def next_ubuntu_version(version: str) -> str:
    """Next devel-series Ubuntu version for an upload fixing `version`.

    X-Y / X-YbuildN      -> X-Yubuntu1
    X-YubuntuN[buildM]   -> X-Yubuntu(N+1)
    X (native)           -> Xubuntu1
    """
    base = strip_ppa(version)
    base = BUILD_RE.sub("", base)
    m = UBUNTU_RE.search(base)
    if m:
        return base[: m.start()] + f"ubuntu{int(m.group(1)) + 1}"
    return base + "ubuntu1"


def changelog_stanza(
    package: str,
    version: str,
    series: str,
    entries: list[str],
    maintainer: tuple[str, str],
    bug: int | None = None,
    when: datetime | None = None,
) -> str:
    when = when or datetime.now(timezone.utc)
    lines = [f"{package} ({version}) {series}; urgency=medium", ""]
    for i, entry in enumerate(entries):
        text = entry
        if bug and i == 0 and f"LP: #{bug}" not in text:
            head, sep, rest = text.partition("\n")
            text = f"{head} (LP: #{bug}){sep}{rest}"
        lines.extend(_wrap_entry(text))
    lines.append("")
    name, email = maintainer
    lines.append(f" -- {name} <{email}>  {format_datetime(when)}")
    return "\n".join(lines) + "\n"


def _wrap_entry(text: str, width: int = 79) -> list[str]:
    """Format one changelog bullet; continuation lines of `text` become sub-items."""
    out: list[str] = []
    for n, para in enumerate(text.splitlines() or [""]):
        first, rest = ("  * ", "    ") if n == 0 else ("    ", "      ")
        out.extend(
            textwrap.wrap(para.strip(), width, initial_indent=first, subsequent_indent=rest)
            or [first.rstrip()]
        )
    return out


def default_entry(kind: str, toolchain: str, new_version: str) -> str:
    what = {"build": "FTBFS", "autopkgtest": "autopkgtest"}.get(kind, "FTBFS/autopkgtest")
    return f"Fix {what} with {toolchain} {new_version}"
