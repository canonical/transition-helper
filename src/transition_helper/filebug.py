"""`th file-bug`: the single write action. Shows the full draft and asks y/N."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .llm.tools import write_patch
from .lp import BugInfo, LPError
from .patches import PatchError, PatchSession, Proposal
from .report import load_report, write_files
from .triage import Services
from .versions import default_entry

WORD_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{2,}")


def bug_tags(report: dict) -> list[str]:
    t = report["transition"]
    tags = list(t.get("lp_bug_tags") or [])
    tags.append("ftbfs" if report["classification"]["phase"] == "build" else "update-excuse")
    out = []
    for tag in tags:
        tag = re.sub(r"[^a-z0-9.+-]", "-", tag.lower()).strip("-")
        if tag and tag not in out:
            out.append(tag)
    return out


def draft_bug(report: dict) -> dict:
    s, t = report["summary"], report["transition"]
    fr = report.get("final_report") or {}
    bd = fr.get("bug_draft") or {}
    kind = "FTBFS" if report["classification"]["phase"] == "build" else "autopkgtest/migration"
    title = bd.get("title") or (f"{s['package']} {s['version'] or ''}: {kind} with "
                                f"{t['toolchain_source']} {t['new_version']}")
    lines = []
    if bd.get("description"):
        lines.append(bd["description"].strip())
        lines.append("")
    lines.append(f"Package: {s['package']} {s['version'] or ''} ({s['location']})")
    lines.append(f"Transition: {t['name']} ({t['toolchain_source']} {t['old_version']} -> "
                 f"{t['new_version']}) in {t['series']}")
    lines.append(f"Category: {s['category']}")
    lines.append("")
    lines.append(f"Root cause: {s['root_cause']}")
    lines.append("")
    links = []
    for b in report["locate"].get("builds", []):
        if b.get("log_url") and b["state"] != "Successfully built":
            links.append(f"- build log {b['arch']} ({b['state']}): {b['log_url']}")
    for ft in report["classification"].get("failing_tests", []):
        if ft.get("log_url"):
            links.append(f"- autopkgtest {ft['test_pkg']}/{ft['arch']} ({ft['status']}): {ft['log_url']}")
    if links:
        lines.append("Logs:")
        lines.extend(links[:20])
        lines.append("")
    if fr.get("evidence"):
        lines.append("Evidence:")
        for e in fr["evidence"][:10]:
            lines.append(f"- {e['log_id']} lines {e['lines']}" + (f": {e['note']}" if e.get("note") else ""))
        lines.append("")
    if s["fix_targets"]:
        lines.append("Proposed fix(es):")
        for f in s["fix_targets"]:
            lines.append(f"- {f['target_pkg']} {f.get('target_version') or ''}: {f['rationale']}")
        lines.append("")
    lines.append("(Drafted with transition-helper; reviewed by the reporter.)")
    attachments = []
    for target, rel in (report.get("patches") or {}).items():
        attachments.append({"target": target, "file": rel})
    return {"title": title.strip(), "description": "\n".join(lines).strip() + "\n",
            "tags": bug_tags(report), "attachments": attachments}


def likely_duplicates(bugs: list[BugInfo], report: dict, draft: dict) -> list[BugInfo]:
    t = report["transition"]
    tags = set(t.get("lp_bug_tags") or [])
    title_words = set(WORD_RE.findall(draft["title"].lower())) - {report["package"]}
    hints = {t["new_version"].lower(), t["toolchain_source"].lower(), t["name"].lower(), "ftbfs"}
    out = []
    for b in bugs:
        words = set(WORD_RE.findall(b.title.lower()))
        if set(b.tags) & tags or hints & words or len(title_words & words) >= 3:
            out.append(b)
    return out


def regenerate_patches(svc: Services, report: dict, report_dir: Path, bug: int) -> dict[str, str]:
    """Rebuild every target's combined diff with the bug number (DEP-3 + LP: #N)."""
    proposals = [Proposal.from_dict(p) for p in report.get("proposals") or []]
    if not proposals:
        return {}
    t = svc.ws.transition
    kind = "build" if report["classification"]["phase"] == "build" else "autopkgtest"
    session = PatchSession(svc.sources, svc.ws.cache_dir / "work" / f"{report_dir.name}-bug",
                           svc.cfg.maintainer_or_placeholder(), t.series,
                           default_entry(kind, t.toolchain_source, t.new_version))
    session.load(proposals)
    written = {}
    for result in session.rebuild_all(bug=bug):
        written[result.target_pkg] = write_patch(report_dir, result, report.get("revision", 1))
    report["proposals"] = [p.to_dict() for ps in session.proposals.values() for p in ps]
    return written


def file_bug(svc: Services, report_dir: Path, input_fn: Callable[[str], str] = input,
             out: Callable[[str], None] = print) -> int:
    report = load_report(report_dir)
    if report.get("bug"):
        out(f"already filed: LP: #{report['bug']['id']} {report['bug']['web_link']}")
        return 0
    pkg = report["package"]
    draft = draft_bug(report)
    try:
        bugs = svc.lp.open_bugs(pkg)
    except LPError as e:
        out(f"warning: duplicate check failed: {e}")
        bugs = []
    dupes = likely_duplicates(bugs, report, draft)

    out("=" * 72)
    out(f"Target:  {pkg} (Ubuntu)")
    out(f"Title:   {draft['title']}")
    out(f"Tags:    {' '.join(draft['tags'])}")
    for a in draft["attachments"]:
        warn = "" if a["target"] == pkg else f"  (patches {a['target']}, NOT {pkg})"
        out(f"Attach:  {a['file']} (flagged as patch){warn}")
    out("-" * 72)
    out(draft["description"])
    out("=" * 72)
    if dupes:
        out("Possible duplicates among open bugs:")
        for b in dupes:
            out(f"  LP: #{b.id} [{b.status}] {b.title} ({b.web_link})")
    elif bugs:
        out(f"{len(bugs)} open bug(s) on {pkg}; none look like duplicates.")
    answer = input_fn("File this bug on Launchpad? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        out("not filed")
        return 0

    try:
        bug = svc.lp.create_bug(pkg, draft["title"], draft["description"], draft["tags"], [])
    except LPError as e:
        out(f"error: {e}")
        return 1
    out(f"filed LP: #{bug.id} {bug.web_link}")
    report["bug"] = bug.to_dict()
    try:
        written = regenerate_patches(svc, report, report_dir, bug.id)
        report["patches"] = dict(report.get("patches") or {}) | written
    except PatchError as e:
        out(f"warning: could not regenerate patches with the bug number: {e}")
    for target, rel in (report.get("patches") or {}).items():
        data = (report_dir / rel).read_bytes()
        try:
            svc.lp.attach(bug.id, Path(rel).name, data, f"Proposed fix for {target}")
        except LPError as e:
            out(f"warning: attaching {rel} failed: {e}")
    write_files(report_dir, report)
    out("report and changelog stanza(s) updated with the bug number")
    return 0
