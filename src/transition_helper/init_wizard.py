"""`th init`: interactive workspace creation."""

from __future__ import annotations

import html.parser
import re
import shutil
from pathlib import Path
from typing import Callable

import yaml

from .cache import HTTP, FetchError
from .config import UserConfig
from .lp import LPClient, LPError
from .progress import Progress
from .sources import SourceError, SourceManager
from .versions import vkey
from .workspace import (BUILD_LOCATIONS, PPA_RE, TRANSITION_FILE, Transition, Workspace,
                        WorkspaceError, create_workspace)


class _TextExtractor(html.parser.HTMLParser):
    SKIP = {"script", "style", "head"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "pre", "tr", "section"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(text: str) -> str:
    p = _TextExtractor()
    p.feed(text)
    out = "".join(p.parts)
    return re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"


def doc_name(src: str) -> str:
    base = src.rstrip("/").rsplit("/", 1)[-1] or "doc"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    return base if base.endswith((".txt", ".md")) else base + ".txt"


class Asker:
    def __init__(self, ask: Callable[[str], str], out: Callable[[str], None]):
        self.ask_fn, self.out = ask, out

    def text(self, label: str, default: str | None = None, required: bool = True) -> str:
        while True:
            prompt = f"{label}" + (f" [{default}]" if default else "") + ": "
            value = self.ask_fn(prompt).strip() or (default or "")
            if value or not required:
                return value
            self.out("  a value is required")

    def items(self, label: str, default: list[str] | None = None) -> list[str]:
        d = ", ".join(default or [])
        value = self.ask_fn(f"{label} (comma-separated)" + (f" [{d}]" if d else "") + ": ").strip()
        if not value:
            return list(default or [])
        if value == "-":
            return []
        return [v.strip() for v in value.split(",") if v.strip()]

    def yes(self, label: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        value = self.ask_fn(f"{label} [{hint}] ").strip().lower()
        return default if not value else value in ("y", "yes")


def fetch_toolchain_changelog(lp: LPClient, http: HTTP, sources: SourceManager, t: Transition,
                              docs: Path) -> str | None:
    ppa = t.ppa if t.build_location == "ppa" else None
    pubs = lp.published_sources(t.toolchain_source, t.series, ppa=ppa)
    if not pubs:
        return None
    pub = max(pubs, key=lambda p: vkey(p.version))
    text = None
    raw = getattr(pub, "raw", None)
    if raw is not None:
        try:
            text = http.get_text(raw.changelogUrl())
        except Exception:
            text = None
    if not text:
        tree = sources.get(t.toolchain_source, pub.version)
        text = (tree.root / "debian" / "changelog").read_text(errors="replace")
    name = "toolchain_changelog.txt"
    (docs / name).write_text(f"# {t.toolchain_source} {pub.version} debian/changelog\n{text}")
    return name


def run_wizard(root: Path, cfg: UserConfig, lp: LPClient, http: HTTP, progress: Progress,
               ask: Callable[[str], str] = input, out: Callable[[str], None] = print) -> Workspace:
    if (root / TRANSITION_FILE).exists():
        raise WorkspaceError(f"{root / TRANSITION_FILE} already exists")
    visible = [p for p in root.iterdir() if not p.name.startswith(".")] if root.exists() else []
    a = Asker(ask, out)
    if visible and not a.yes(f"{root} is not empty. Create the workspace here anyway?"):
        raise WorkspaceError("aborted")

    name = a.text("Transition name", root.name)
    try:
        default_series = lp.current_series() if hasattr(lp, "current_series") else None
    except LPError:
        default_series = None
    while True:
        series = a.text("Series", default_series)
        if lp.series_exists(series):
            break
        out(f"  series {series!r} not found on Launchpad")
    while True:
        toolchain = a.text("Toolchain source package (e.g. python3-defaults, gcc-defaults)")
        if lp.source_exists(toolchain, series):
            break
        out(f"  {toolchain!r} is not published in {series}")
    old_version = a.text("Old toolchain version (e.g. 3.14)")
    new_version = a.text("New toolchain version (e.g. 3.15)")
    while True:
        location = a.text(f"Build location ({'/'.join(BUILD_LOCATIONS)})", "archive")
        if location in BUILD_LOCATIONS:
            break
        out("  must be 'ppa' or 'archive'")
    ppa = None
    private = False
    if location == "ppa":
        while True:
            ppa = a.text("PPA (~owner/ubuntu/name)")
            if not PPA_RE.match(ppa):
                out("  must look like ~owner/ubuntu/name")
                continue
            try:
                info = lp.ppa_info(ppa)
                private = info.private
                break
            except LPError as e:
                out(f"  {e}")
    relevant = a.items("Relevant binaries (used as hints)")
    ignored = a.items("Arches to ignore by default", [])
    tags = a.items("LP bug tags", [name])
    if private and not cfg.allow_private_data:
        out(f"WARNING: {ppa} is private. Logs and sources from it would be sent to the configured "
            "LLM provider; transition-helper will refuse to do so unless allow_private_data: true "
            "is set in the user config.")

    t = Transition(name=name, series=series, toolchain_source=toolchain, old_version=old_version,
                   new_version=new_version, build_location=location, ppa=ppa,
                   relevant_binaries=relevant, ignored_arches=ignored, lp_bug_tags=tags, docs=[])
    ws = create_workspace(root, t)
    out(f"created {root / TRANSITION_FILE}")

    sources = SourceManager(ws.cache_dir, lp, http, progress, ppa=ppa)
    try:
        progress.step("init", f"fetching debian/changelog of {toolchain}")
        doc = fetch_toolchain_changelog(lp, http, sources, t, ws.docs_dir)
        if doc:
            t.docs.append(doc)
            out(f"saved docs/{doc}")
    except (LPError, SourceError, FetchError, OSError) as e:
        out(f"warning: could not fetch the toolchain changelog: {e}")

    while True:
        src = a.text("Extra doc to fetch (URL or file path; empty to finish)", required=False)
        if not src:
            break
        try:
            name_ = doc_name(src)
            dest = ws.docs_dir / name_
            if re.match(r"https?://", src):
                body = http.get_text(src)
                if "<html" in body[:2000].lower():
                    body = html_to_text(body)
                dest.write_text(f"# {src}\n{body}")
            else:
                path = Path(src).expanduser()
                shutil.copyfile(path, dest)
            if name_ not in t.docs:
                t.docs.append(name_)
            out(f"saved docs/{name_}")
        except (FetchError, FileNotFoundError, OSError) as e:
            out(f"  failed: {e}")

    (root / TRANSITION_FILE).write_text(
        yaml.safe_dump(t.to_dict(), sort_keys=False, default_flow_style=None))
    return Workspace(root=root, transition=t)
