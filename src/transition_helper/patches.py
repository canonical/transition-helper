"""propose_patch validation and combined-diff generation.

For each target package we keep a list of accepted proposals. The combined diff is
rebuilt from scratch by replaying every proposal into a git worktree of the cached
source tree, so it is always consistent and re-generatable (e.g. by `file-bug`
after a bug number is known)."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .sources import SourceError, SourceManager, SourceTree, run, tree_name
from .versions import changelog_stanza, next_ubuntu_version

PATCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
FORBIDDEN_PATHS = ("debian/changelog", "debian/patches/series")


class PatchError(Exception):
    pass


@dataclass
class FileDiff:
    path: str
    text: str

    @property
    def is_debian(self) -> bool:
        return self.path == "debian" or self.path.startswith("debian/")


@dataclass
class Proposal:
    target_pkg: str
    target_version: str
    patch_name: str
    description: str
    unified_diff: str
    bug: int | None = None
    files: list[str] = field(default_factory=list)
    quilt_patch: str | None = None  # name in debian/patches, when one was created

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Proposal":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


def _path_of(header_path: str) -> str | None:
    header_path = header_path.split("\t")[0].strip()
    if header_path == "/dev/null":
        return None
    if header_path.startswith(("a/", "b/")):
        return header_path[2:]
    raise PatchError(f"diff path {header_path!r} must use a/ and b/ prefixes (patch -p1 style)")


def split_diff(diff: str) -> list[FileDiff]:
    """Split a unified diff into per-file sections (git or plain ---/+++ style)."""
    if not diff.strip():
        raise PatchError("empty diff")
    lines = diff.splitlines(keepends=True)
    starts: list[int] = []
    git_style = any(line.startswith("diff --git ") for line in lines)
    for i, line in enumerate(lines):
        if git_style and line.startswith("diff --git "):
            starts.append(i)
        elif not git_style and line.startswith("--- ") and i + 1 < len(lines) \
                and lines[i + 1].startswith("+++ "):
            starts.append(i)
    if not starts:
        raise PatchError("no file headers found; send a unified diff with ---/+++ headers")
    out: list[FileDiff] = []
    for n, s in enumerate(starts):
        e = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunk = lines[s:e]
        path = None
        minus = plus = None
        for line in chunk:
            if line.startswith("--- "):
                minus = line[4:].rstrip("\n")
            elif line.startswith("+++ "):
                plus = line[4:].rstrip("\n")
                break
        if plus is not None:
            path = _path_of(plus) or (_path_of(minus) if minus else None)
        elif chunk[0].startswith("diff --git "):
            m = re.match(r"diff --git a/(\S+) b/(\S+)", chunk[0])
            path = m.group(2) if m else None
        if not path:
            raise PatchError(f"cannot determine file path for section starting {chunk[0]!r:.80}")
        text = "".join(chunk)
        if not text.endswith("\n"):
            text += "\n"
        out.append(FileDiff(path=path, text=text))
    for fd in out:
        parts = Path(fd.path).parts
        if fd.path.startswith("/") or ".." in parts:
            raise PatchError(f"path {fd.path!r} escapes the source tree")
        if parts and parts[0] in (".pc", ".git"):
            raise PatchError(f"path {fd.path!r} is not editable")
        if fd.path in FORBIDDEN_PATHS:
            raise PatchError(f"do not edit {fd.path}; the tool generates it")
    return out


def dep3_header(description: str, author: tuple[str, str], bug: int | None,
                when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    desc_lines = [line.rstrip() for line in description.strip().splitlines()] or ["(no description)"]
    out = [f"Description: {desc_lines[0]}"]
    for line in desc_lines[1:]:
        out.append(" ." if not line.strip() else f" {line}")
    out.append(f"Author: {author[0]} <{author[1]}>")
    if bug:
        out.append(f"Bug-Ubuntu: https://bugs.launchpad.net/bugs/{bug}")
    out.append("Forwarded: no")
    out.append(f"Last-Update: {when.strftime('%Y-%m-%d')}")
    out.append("---")
    return "\n".join(out) + "\n"


def sanitize_patch_name(name: str) -> str:
    name = name.strip().replace(" ", "-")
    if name.startswith("debian/patches/"):
        name = name[len("debian/patches/"):]
    if not PATCH_NAME_RE.match(name):
        raise PatchError(f"invalid patch_name {name!r}; use letters, digits, . _ + -")
    if not name.endswith((".patch", ".diff")):
        name += ".patch"
    return name


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def _pad_hunk(hunk: list[str], file_lines: list[str]) -> list[str]:
    """Make leading/trailing context the same length using the real file.

    git apply and patch -F0 treat a hunk with less trailing than leading context as
    anchored to end of file (and vice versa), which model-written diffs often get
    wrong. The hunk's old lines must match the file exactly; nothing is fuzzed."""
    m = HUNK_RE.match(hunk[0].rstrip("\n"))
    body = hunk[1:]
    if not m or not body:
        return hunk
    lead = next((i for i, line in enumerate(body) if line[:1] in "+-"), len(body))
    trail = next((i for i, line in enumerate(reversed(body)) if line[:1] in "+-"), len(body))
    if lead == trail or lead == len(body):
        return hunk
    old = [line[1:].rstrip("\n") for line in body if line[:1] in (" ", "-")]
    want = int(m.group(1)) - 1
    starts = [i for i in range(len(file_lines) - len(old) + 1)
              if file_lines[i:i + len(old)] == old]
    if not starts:
        return hunk
    start = min(starts, key=lambda i: abs(i - want))
    end = start + len(old)
    if trail < lead:
        extra = file_lines[end:end + lead - trail]
        body = body + [f" {line}\n" for line in extra]
    else:
        extra = file_lines[max(0, start - (trail - lead)):start]
        body = [f" {line}\n" for line in extra] + body
        start -= len(extra)
    n_old = sum(1 for line in body if line[:1] in (" ", "-"))
    n_new = sum(1 for line in body if line[:1] in (" ", "+"))
    new_start = int(m.group(3)) - (int(m.group(1)) - 1 - start)
    return [f"@@ -{start + 1},{n_old} +{max(new_start, 0)},{n_new} @@{m.group(5)}\n"] + body


def repair_context(root: Path, text: str) -> str:
    out: list[str] = []
    for section in split_diff(text):
        lines = section.text.splitlines(keepends=True)
        target = root / section.path
        try:
            file_lines = target.read_text(errors="replace").splitlines()
        except OSError:
            out.append(section.text)
            continue
        i = 0
        while i < len(lines) and not lines[i].startswith("@@"):
            out.append(lines[i])
            i += 1
        while i < len(lines):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("@@"):
                j += 1
            out.extend(_pad_hunk(lines[i:j], file_lines))
            i = j
    return "".join(out)


def apply_diff(root: Path, text: str, check_only: bool = False) -> tuple[bool, str]:
    """Apply (or check) a diff one file section at a time.

    Per section because --recount (needed for model-written hunk headers) would
    otherwise swallow a following plain `--- a/...` header as a removed line. A
    section that fails is retried once with context-repaired hunks (_pad_hunk).
    No fuzz either way."""
    args = ["git", "apply", "--recount", "-p1"] + (["--check"] if check_only else []) + ["-"]
    for section in split_diff(text):
        p = run(args, cwd=root, check=False, input=section.text)
        if p.returncode == 0:
            continue
        err = (p.stderr or p.stdout).strip()
        repaired = repair_context(root, section.text)
        if repaired == section.text or run(args, cwd=root, check=False, input=repaired).returncode != 0:
            return False, err[-3000:]
    return True, ""


def quilt_env() -> dict:
    env = dict(os.environ)
    env.update({"QUILT_PATCHES": "debian/patches", "QUILT_PC": ".pc",
                "QUILT_PATCH_OPTS": "--reject-format=unified", "QUILT_NO_DIFF_INDEX": "1"})
    return env


def quilt(args: list[str], cwd: Path, check: bool = True):
    return run(["quilt", "--quiltrc", "-", *args], cwd=cwd, check=check, env=quilt_env())


@dataclass
class TargetResult:
    target_pkg: str
    base_version: str
    new_version: str
    combined_diff: str
    proposals: list[Proposal]


class PatchSession:
    def __init__(self, sources: SourceManager, work_dir: Path, maintainer: tuple[str, str],
                 series: str, entry: str):
        self.sources = sources
        self.work_dir = work_dir
        self.maintainer = maintainer
        self.series = series
        self.entry = entry  # first changelog bullet, e.g. "Fix FTBFS with python3-defaults 3.15"
        self.proposals: dict[str, list[Proposal]] = {}
        self.versions: dict[str, str] = {}

    # -- public -----------------------------------------------------------------
    def propose(self, target_pkg: str, unified_diff: str, description: str, patch_name: str,
                bug: int | None = None, target_version: str | None = None) -> TargetResult:
        if not description or not description.strip():
            raise PatchError("description is required (it becomes the DEP-3 Description)")
        version = target_version or self.versions.get(target_pkg)
        tree = self._tree(target_pkg, version)
        name = sanitize_patch_name(patch_name)
        sections = split_diff(unified_diff)
        diff_text = "".join(s.text for s in sections)
        ok, err = apply_diff(tree.root, diff_text, check_only=True)
        if not ok:
            raise PatchError("git apply --check failed against "
                             f"{tree_name(tree.pkg, tree.version)}:\n{err}")
        proposal = Proposal(target_pkg=tree.pkg, target_version=tree.version, patch_name=name,
                            description=description.strip(), unified_diff=diff_text, bug=bug,
                            files=[s.path for s in sections])
        existing = [q for q in self.proposals.get(tree.pkg, []) if q.patch_name != name]
        result = self.build(tree, existing + [proposal], bug=bug)
        self.proposals[tree.pkg] = result.proposals
        self.versions[tree.pkg] = tree.version
        return result

    def load(self, proposals: list[Proposal]) -> None:
        for p in proposals:
            self.proposals.setdefault(p.target_pkg, []).append(p)
            self.versions[p.target_pkg] = p.target_version

    def targets(self) -> list[str]:
        return [t for t, ps in self.proposals.items() if ps]

    def rebuild_all(self, bug: int | None = None) -> list[TargetResult]:
        out = []
        for target in self.targets():
            tree = self._tree(target, self.versions[target])
            out.append(self.build(tree, self.proposals[target], bug=bug))
        return out

    # -- internals ----------------------------------------------------------------
    def _tree(self, pkg: str, version: str | None) -> SourceTree:
        if version:
            tree = self.sources.existing(pkg, version)
            if tree is None:
                try:
                    tree = self.sources.get(pkg, version)
                except SourceError as e:
                    raise PatchError(f"source of {pkg} {version} unavailable: {e}") from e
            return tree
        cands = sorted(self.sources.root.glob(f"{pkg}_*"))
        cands = [c for c in cands if (c / ".git").is_dir()]
        if len(cands) == 1:
            ver = cands[0].name[len(pkg) + 1:]
            tree = self.sources.existing(pkg, ver)
            if tree:
                return tree
        raise PatchError(f"no fetched source tree for {pkg}; call get_source({pkg!r}) first"
                         if not cands else f"several trees for {pkg}; pass target_version")

    def build(self, tree: SourceTree, proposals: list[Proposal], bug: int | None = None,
              ) -> TargetResult:
        wt = self.work_dir / tree_name(tree.pkg, tree.version)
        self._fresh_worktree(tree, wt)
        try:
            base = run(["git", "rev-parse", "HEAD"], cwd=tree.root).stdout.strip()
            applied: list[Proposal] = []
            for prop in proposals:
                prop.bug = bug or prop.bug
                self._apply(tree, wt, prop)
                applied.append(prop)
            if any(p.quilt_patch for p in applied):
                self._check_series(wt)
            new_version = next_ubuntu_version(tree.version)
            subs = []
            for p in applied:
                first = p.description.splitlines()[0].rstrip(".")
                if p.quilt_patch:
                    subs.append(f"d/p/{p.quilt_patch}: {first}.")
                    direct = [f for f in p.files if f.startswith("debian/")]
                else:
                    direct = p.files
                if direct:
                    subs.append(f"{', '.join(_short(f) for f in direct)}: {first}.")
            entry_text = self.entry + "".join("\n- " + s for s in subs)
            stanza = changelog_stanza(tree.pkg, new_version, self.series, [entry_text],
                                      self.maintainer, bug=bug or next((p.bug for p in applied if p.bug), None))
            cl = wt / "debian" / "changelog"
            old = cl.read_text(errors="replace") if cl.exists() else ""
            cl.write_text(stanza + "\n" + old)
            run(["git", "add", "-A", "-f", "."], cwd=wt)
            diff = run(["git", "diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames",
                        base, "--", ".", ":(exclude).pc"], cwd=wt).stdout
            return TargetResult(target_pkg=tree.pkg, base_version=tree.version,
                                new_version=new_version, combined_diff=diff, proposals=applied)
        finally:
            self._remove_worktree(tree, wt)

    def _fresh_worktree(self, tree: SourceTree, wt: Path) -> None:
        self._remove_worktree(tree, wt)
        wt.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "worktree", "add", "-q", "--detach", str(wt), "HEAD"], cwd=tree.root)

    def _remove_worktree(self, tree: SourceTree, wt: Path) -> None:
        if wt.exists():
            run(["git", "worktree", "remove", "--force", str(wt)], cwd=tree.root, check=False)
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
        run(["git", "worktree", "prune"], cwd=tree.root, check=False)

    def _apply(self, tree: SourceTree, wt: Path, prop: Proposal) -> None:
        sections = split_diff(prop.unified_diff)
        upstream = [s for s in sections if not s.is_debian]
        direct = [s for s in sections if s.is_debian]
        prop.quilt_patch = None
        if tree.is_quilt and upstream:
            text = "".join(s.text for s in upstream)
            self._git_apply(wt, text, prop)
            # regenerate a clean diff (correct hunk counts) from git, then revert
            run(["git", "add", "-A", "-f", "--", *[s.path for s in upstream]], cwd=wt)
            clean = run(["git", "diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames",
                         "--", *[s.path for s in upstream]], cwd=wt).stdout
            run(["git", "reset", "-q", "--hard", "HEAD"], cwd=wt)
            run(["git", "clean", "-fdq", "--", *[s.path for s in upstream]], cwd=wt, check=False)
            patches_dir = wt / "debian" / "patches"
            patches_dir.mkdir(parents=True, exist_ok=True)
            if (patches_dir / prop.patch_name).exists():
                raise PatchError(f"debian/patches/{prop.patch_name} already exists; choose another patch_name")
            (patches_dir / prop.patch_name).write_text(
                dep3_header(prop.description, self.maintainer, prop.bug) + clean)
            series = patches_dir / "series"
            existing = series.read_text() if series.exists() else ""
            if existing and not existing.endswith("\n"):
                existing += "\n"
            series.write_text(existing + prop.patch_name + "\n")
            prop.quilt_patch = prop.patch_name
        elif upstream:
            direct = sections
        if direct:
            self._git_apply(wt, "".join(s.text for s in direct), prop)
        run(["git", "add", "-A", "-f", "."], cwd=wt)
        run(["git", "commit", "-q", "--no-verify", "--allow-empty", "-m", prop.patch_name], cwd=wt)

    def _git_apply(self, wt: Path, text: str, prop: Proposal) -> None:
        ok, err = apply_diff(wt, text)
        if not ok:
            raise PatchError(f"{prop.patch_name} does not apply on top of the other proposals for "
                             f"{prop.target_pkg}:\n{err}")

    def _check_series(self, wt: Path) -> None:
        """The whole series (existing + new patches) must apply from pristine, fuzz 0."""
        try:
            if (wt / ".pc" / "applied-patches").exists():
                # -f: restore from .pc backups even if a patch file was edited
                p = quilt(["pop", "-a", "-q", "-f"], wt, check=False)
                if p.returncode not in (0, 2):
                    raise PatchError(f"quilt pop -a failed:\n{(p.stdout + p.stderr)[-3000:]}")
            p = quilt(["push", "-a", "-q", "--fuzz=0"], wt, check=False)
            if p.returncode not in (0, 2):
                raise PatchError("the quilt series no longer applies (quilt push -a):\n"
                                 f"{(p.stdout + p.stderr).strip()[-3000:]}")
        finally:
            run(["git", "reset", "-q", "--hard", "HEAD"], cwd=wt, check=False)
            run(["git", "clean", "-fdqx"], cwd=wt, check=False)


def _short(path: str) -> str:
    return "d/" + path[len("debian/"):] if path.startswith("debian/") else path

