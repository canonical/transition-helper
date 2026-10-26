"""Report rendering: report.md, report.json, commands.sh, evidence/.

Everything renders from the report dict (report.json), so follow-up revisions and
file-bug can re-render without re-running the pipeline."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from debian.debian_support import Version

from . import __version__
from .workspace import Workspace

SCHEMA_VERSION = 1


class ReportError(Exception):
    pass


def suffix(revision: int) -> str:
    return "" if revision <= 1 else f".v{revision}"


# -- building the dict -------------------------------------------------------------------

def build_report(ws: Workspace, data) -> dict:
    loc, cls = data.loc, data.cls
    report = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revision": 1,
        "transition": ws.transition.to_dict(),
        "package": data.pkg,
        "version": loc.target_version,
        "options": {k: v for k, v in data.opts.__dict__.items()},
        "locate": loc.to_dict(),
        "classification": cls.to_dict(),
        "excuse": data.excuse,
        "blockers": data.chain.to_dict() if data.chain else None,
        "evidence": {
            "logs": [ev.to_dict() for ev in data.logs],
            "groups": [g.to_dict() for g in data.groups],
            "tests": data.test_infra,
            "bugs": data.bugs,
            "debian": data.debian.to_dict() if data.debian else None,
            "queued": data.queued,
        },
        "kb_matches": data.kb_ids(),
        "deterministic_next_steps": data.next_steps,
        "commands": data.commands,
        "llm": data.llm,
        "final_report": data.final_report,
        "proposals": data.proposals,
        "patches": data.patches,
        "bug": None,
        "warnings": data.warnings,
        "excuses_generated": data.excuses_generated,
    }
    report["summary"] = compute_summary(report)
    return report


def describe_deterministic(report: dict) -> str:
    loc, cls = report["locate"], report["classification"]
    primary = cls["primary"]
    parts = [f"{loc['state']}"]
    if cls["build_issues"]:
        parts.append("; ".join(sorted({f"{b['state']} on {b['arch']}" for b in cls["build_issues"]})))
    if cls["failing_tests"]:
        tests = sorted({f"{t['test_pkg']}/{t['arch']}" for t in cls["failing_tests"]})
        parts.append(f"{len(tests)} failing test(s): {', '.join(tests[:8])}"
                     + (" …" if len(tests) > 8 else ""))
    if report.get("blockers") and report["blockers"].get("roots"):
        parts.append("blocked by " + ", ".join(report["blockers"]["roots"]))
    if cls["uninstallable"]:
        parts.append("uninstallable: " + "; ".join(cls["uninstallable"][:3]))
    if report.get("kb_matches"):
        parts.append("KB: " + ", ".join(report["kb_matches"]))
    return f"{primary}: " + " — ".join(parts)


def compute_summary(report: dict) -> dict:
    fr = report.get("final_report") or {}
    cls = report["classification"]
    patches = report.get("patches") or {}
    proposals = report.get("proposals") or []
    fixes = []
    for fix in fr.get("fixes", []):
        target = fix.get("target_pkg")
        fixes.append({
            "target_pkg": target,
            "target_version": fix.get("target_version"),
            "rationale": fix.get("rationale"),
            "patch_ref": fix.get("patch_ref"),
            "patch_file": patches.get(target),
            "is_triaged_package": target == report["package"],
        })
    # patches that the final report did not mention still have to be visible
    for target, rel in patches.items():
        if not any(f["target_pkg"] == target for f in fixes):
            names = [p["patch_name"] for p in proposals if p["target_pkg"] == target]
            fixes.append({"target_pkg": target, "target_version": None,
                          "rationale": "(not referenced by the final report)",
                          "patch_ref": ", ".join(names), "patch_file": rel,
                          "is_triaged_package": target == report["package"]})
    steps = list(fr.get("suggested_actions") or []) + [
        s for s in report.get("deterministic_next_steps", []) if s not in (fr.get("suggested_actions") or [])]
    return {
        "package": report["package"],
        "version": report.get("version"),
        "location": report["locate"].get("target_location"),
        "state": report["locate"]["state"],
        "phase": cls["phase"],
        "category": fr.get("category") or cls["primary"],
        "deterministic_categories": cls["categories"],
        "root_cause": fr.get("root_cause") or describe_deterministic(report),
        "is_transition_related": fr.get("is_transition_related"),
        "fix_targets": fixes,
        "confidence": fr.get("confidence") or ("deterministic only" if not report["llm"].get("ran")
                                               else "n/a (no final report)"),
        "next_steps": steps,
    }


# -- markdown ---------------------------------------------------------------------------

def _yn(v) -> str:
    return {True: "yes", False: "no"}.get(v, "unknown")


def _cell(text) -> str:
    return str(text if text is not None else "-").replace("|", "\\|").replace("\n", " ")


def render_fix_targets(summary: dict) -> list[str]:
    out = []
    fixes = summary["fix_targets"]
    if not fixes:
        return ["- none proposed"]
    for f in fixes:
        mark = "" if f["is_triaged_package"] else " ⚠ **different package than the one triaged**"
        patch = f" — patch: `{f['patch_file']}`" if f.get("patch_file") else " — no patch"
        out.append(f"- **{f['target_pkg']}** {f.get('target_version') or ''}{mark}{patch}")
        out.append(f"  - why: {f['rationale']}")
    return out


def render_markdown(report: dict) -> str:
    s = report["summary"]
    loc, cls, ev = report["locate"], report["classification"], report["evidence"]
    t = report["transition"]
    rev = report.get("revision", 1)
    L: list[str] = []
    title = f"# Triage: {s['package']} {s['version'] or ''}".rstrip()
    L.append(title + (f" (revision {rev})" if rev > 1 else ""))
    L.append("")
    L.append(f"Transition **{t['name']}** ({t['toolchain_source']} {t['old_version']} → "
             f"{t['new_version']}, {t['series']}). Generated {report['created']} by "
             f"transition-helper {report['tool_version']}.")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| Package | {_cell(s['package'])} {_cell(s['version'])} ({_cell(s['location'])}) |")
    L.append(f"| Phase / category | {_cell(s['phase'])} / **{_cell(s['category'])}** "
             f"(deterministic: {_cell(', '.join(s['deterministic_categories']))}) |")
    L.append(f"| State | {_cell(s['state'])} |")
    L.append(f"| Root cause | {_cell(s['root_cause'])} |")
    L.append(f"| Transition-related | {_yn(s['is_transition_related'])} |")
    L.append(f"| Confidence | {_cell(s['confidence'])} |")
    L.append("")
    L.append("### Fix target(s)")
    L.append("")
    L.extend(render_fix_targets(s))
    L.append("")
    L.append("### Next steps")
    L.append("")
    L.extend(f"{i}. {step}" for i, step in enumerate(s["next_steps"], 1))
    if not s["next_steps"]:
        L.append("- none")
    L.append("")
    if report.get("bug"):
        L.append(f"Filed as LP: #{report['bug']['id']} — {report['bug']['web_link']}")
        L.append("")

    L.append("## State")
    L.append("")
    L.append("| Location | Version |")
    L.append("|---|---|")
    for k, v in loc["versions"].items():
        L.append(f"| {k} | {_cell(v)} |")
    L.append("")
    if loc["builds"]:
        L.append("| Arch | Build state | Log |")
        L.append("|---|---|---|")
        for b in sorted(loc["builds"], key=lambda b: b["arch"]):
            log = f"[log]({b['log_url']})" if b.get("log_url") else "-"
            L.append(f"| {b['arch']} | {_cell(b['state'])} | {log} |")
        L.append("")
    for n in loc["notes"]:
        L.append(f"- {n}")
    if report.get("excuses_generated"):
        L.append(f"- excuses generated {report['excuses_generated'][:16]} UTC")
    if loc["notes"] or report.get("excuses_generated"):
        L.append("")

    L.append("## Classification")
    L.append("")
    L.append(f"Categories: {', '.join(cls['categories'])}")
    L.append("")
    for n in cls["notes"]:
        L.append(f"- {n}")
    for b in cls["build_issues"]:
        dep = f" — needs `{b['missing_dependency']}`" if b.get("missing_dependency") else ""
        L.append(f"- build {b['arch']}: {b['category']} ({b['state']}){dep}")
    if cls["failing_tests"]:
        tests_info = {(i["test_pkg"], i["arch"]): i for i in ev.get("tests", [])}
        L.append("")
        L.append("| Test package | Arch | Status | Kind | Triggers | Heuristics | Log |")
        L.append("|---|---|---|---|---|---|---|")
        for ft in cls["failing_tests"]:
            info = tests_info.get((ft["test_pkg"], ft["arch"]), {})
            flags = []
            if info.get("likely_infra"):
                flags.append("likely infra")
            if info.get("likely_flaky"):
                flags.append("likely flaky")
            if info.get("history"):
                flags.append("history (newest first): " + " ".join(info["history"][:6]))
            log = f"[log.gz]({ft['log_url']})" if ft.get("log_url") else "-"
            L.append(f"| {ft['test_pkg']} {ft.get('test_version') or ''} | {ft['arch']} | {ft['status']} | "
                     f"{ft['kind']} | {_cell(' '.join(ft['triggers']))} | {_cell('; '.join(flags))} | {log} |")
    if cls["pending_tests"]:
        L.append("")
        L.append(f"Pending tests: {len(cls['pending_tests'])} "
                 f"({', '.join(sorted({p['test_pkg'] for p in cls['pending_tests']})[:15])})")
    if cls["uninstallable"]:
        L.append("")
        L.append("Uninstallable:")
        L.extend(f"- {u}" for u in cls["uninstallable"][:30])
    L.append("")

    if report.get("blockers"):
        b = report["blockers"]
        L.append("## Blocker chain")
        L.append("")
        for e in b["edges"]:
            L.append(f"- {e['pkg']} → {e['blocker']} ({e['reason']})")
        for c in b["cycles"]:
            L.append(f"- cycle: {' → '.join(c)}")
        if b["roots"]:
            L.append("")
            L.append("Root(s): " + ", ".join(f"`th triage {r}`" for r in b["roots"]))
        if b["not_in_proposed"]:
            L.append("")
            L.append("Without an excuse: " + ", ".join(b["not_in_proposed"]))
        L.append("")

    L.append("## Evidence")
    L.append("")
    if report.get("kb_matches"):
        L.append("### Knowledge-base matches")
        L.append("")
        for log in ev["logs"]:
            for m in log.get("kb", []):
                first = m["lines"][0] if m["lines"] else [0, ""]
                L.append(f"- `{m['sig_id']}` in `{m['log_id']}` line {first[0]}: `{first[1][:200]}`")
        L.append("")
    if ev["logs"]:
        L.append("### Logs")
        L.append("")
        groups = {g["representative"]: g for g in ev.get("groups", [])}
        for log in ev["logs"]:
            g = groups.get(log["log_id"])
            same = f" (representative of {len(g['members'])})" if g and len(g["members"]) > 1 else ""
            err = f" — download failed: {log['error']}" if log.get("error") else ""
            L.append(f"- `{log['log_id']}` [{log['lines']} lines]({log['url']}){same}{err}")
        L.append("")
        L.append("Failure excerpts are in `evidence/excerpts/`.")
        L.append("")
    bugs = ev.get("bugs") or []
    tags = set(t.get("lp_bug_tags") or [])
    if bugs:
        L.append("### Open Launchpad bugs")
        L.append("")
        for b in bugs[:20]:
            hl = " **[transition]**" if set(b["tags"]) & tags else ""
            L.append(f"- [LP: #{b['id']}]({b['web_link']}) [{b['status']}] {b['title']}{hl}")
        L.append("")
    deb = ev.get("debian")
    if deb:
        L.append("### Debian")
        L.append("")
        if deb.get("error"):
            L.append(f"- check failed: {deb['error']}")
        for suite, v in (deb.get("versions") or {}).items():
            L.append(f"- {suite}: {v}")
        if deb.get("hint"):
            L.append(f"- **{deb['hint']}**")
        for e in deb.get("entries", [])[:10]:
            mark = " (mentions the transition)" if e.get("relevant") else ""
            L.append(f"  - {e['version']}{mark}")
        L.append("")
    if ev.get("queued"):
        L.append("### Queued / running tests")
        L.append("")
        for q in ev["queued"][:30]:
            L.append(f"- {q['test_pkg']}/{q['arch']}: {q['state']} ({' '.join(q.get('triggers', []))})")
        L.append("")

    fr = report.get("final_report")
    llm = report.get("llm") or {}
    L.append("## AI analysis")
    L.append("")
    if not llm.get("ran"):
        L.append(f"Not run: {llm.get('reason', 'n/a')}.")
        L.append("")
    elif not fr:
        L.append("The model did not produce a valid final report"
                 + (f" ({'; '.join(llm.get('errors') or [])})" if llm.get("errors") else "") + ".")
    else:
        L.append(f"Model `{llm.get('model')}`, {llm.get('iterations')} turn(s), "
                 f"{llm.get('tokens')} tokens{' (budget forced the report)' if llm.get('forced') else ''}. "
                 "**Review everything below; the operator decides.**")
        L.append("")
        L.append(f"**Root cause:** {fr['root_cause']}")
        L.append("")
        if fr.get("evidence"):
            L.append("Evidence:")
            for e in fr["evidence"]:
                L.append(f"- `{e['log_id']}` lines {e['lines']}" + (f": {e['note']}" if e.get("note") else ""))
            L.append("")
        if fr.get("kb_matches"):
            L.append("KB matches cited: " + ", ".join(fr["kb_matches"]))
            L.append("")
        bd = fr.get("bug_draft")
        if bd:
            L.append("### Bug draft")
            L.append("")
            L.append(f"**{bd['title']}**")
            L.append("")
            L.append("```")
            L.append(bd["description"])
            L.append("```")
            L.append("")
    if report.get("proposals"):
        L.append("### Proposed patches")
        L.append("")
        for p in report["proposals"]:
            kind = f"DEP-3 quilt patch `debian/patches/{p['quilt_patch']}`" if p.get("quilt_patch") \
                else "direct edit"
            L.append(f"- **{p['target_pkg']}** {p['target_version']}: `{p['patch_name']}` ({kind}); "
                     f"files: {', '.join(p['files'])}")
            L.append(f"  - {p['description'].splitlines()[0]}")
        L.append("")
        L.append("Apply with `patch -p1 < <file>` inside the unpacked source "
                 "(`dpkg-source -x`). Nothing has been built or uploaded.")
        L.append("")
    if report.get("warnings"):
        L.append("## Warnings")
        L.append("")
        L.extend(f"- {w}" for w in report["warnings"])
        L.append("")
    L.append("## Files")
    L.append("")
    sfx = suffix(rev)
    L.append(f"- `report{sfx}.json`, `commands{sfx}.sh` (suggestions only)")
    for target, rel in (report.get("patches") or {}).items():
        L.append(f"- `{rel}` (patches **{target}**)")
    L.append("- `evidence/` (bundle, excerpts, transcript.jsonl)")
    return "\n".join(L) + "\n"


def render_commands(report: dict, report_dir: Path | None = None) -> str:
    lines = ["#!/bin/sh",
             "# transition-helper suggestions. NOTHING here has been run.",
             "# Review each command and uncomment what you decide to do.",
             "exit 0", ""]
    for block in report.get("commands", []):
        for line in block.splitlines():
            lines.append(line if line.startswith("#") else f"# {line}")
        lines.append("")
    pkg = report["package"]
    for target, rel in (report.get("patches") or {}).items():
        props = [p for p in report.get("proposals", []) if p["target_pkg"] == target]
        ver = props[0]["target_version"] if props else "<version>"
        lines.append(f"# apply the proposed changes to {target}"
                     + ("" if target == pkg else f" (NOT the triaged package {pkg})"))
        upstream = Version(ver).upstream_version if props else "<upstream-version>"
        lines.append(f"# pull-lp-source {target} {ver} && (cd {target}-{upstream} "
                     f"&& patch -p1 < '{(report_dir.resolve() / rel) if report_dir else rel}')")
        lines.append("")
    return "\n".join(lines)


# -- writing -----------------------------------------------------------------------------

def write_excerpts(report_dir: Path, data) -> None:
    exc = report_dir / "evidence" / "excerpts"
    exc.mkdir(parents=True, exist_ok=True)
    for ev in data.logs:
        if ev.windows:
            name = re.sub(r"[^A-Za-z0-9._@%+-]", "_", ev.log_id)
            body = "\n\n".join(f"--- {ev.log_id} lines {w.start}-{w.end} ---\n{w.render()}"
                               for w in ev.windows)
            (exc / f"{name}.txt").write_text(f"# {ev.url}\n{body}\n")


def write_files(report_dir: Path, report: dict) -> Path:
    rev = report.get("revision", 1)
    sfx = suffix(rev)
    report["summary"] = compute_summary(report)
    (report_dir / f"report{sfx}.json").write_text(json.dumps(report, indent=1, default=str))
    md = report_dir / f"report{sfx}.md"
    md.write_text(render_markdown(report))
    cmd = report_dir / f"commands{sfx}.sh"
    cmd.write_text(render_commands(report, report_dir))
    cmd.chmod(0o755)
    return md


def write_report(ws: Workspace, data, report_dir: Path, revision: int = 1) -> dict:
    report = build_report(ws, data)
    report["revision"] = revision
    (report_dir / "evidence").mkdir(parents=True, exist_ok=True)
    write_excerpts(report_dir, data)
    (report_dir / "evidence" / "bundle.json").write_text(json.dumps({
        "locate": report["locate"], "classification": report["classification"],
        "excuse": report["excuse"], "blockers": report["blockers"],
        "evidence": report["evidence"], "kb_matches": report["kb_matches"],
    }, indent=1, default=str))
    write_files(report_dir, report)
    return report


def latest_revision(report_dir: Path) -> int:
    revs = [1] if (report_dir / "report.json").exists() else []
    for p in report_dir.glob("report.v*.json"):
        m = re.match(r"report\.v(\d+)\.json$", p.name)
        if m:
            revs.append(int(m.group(1)))
    if not revs:
        raise ReportError(f"{report_dir} contains no report.json")
    return max(revs)


def load_report(report_dir: Path, revision: int | None = None) -> dict:
    rev = revision or latest_revision(report_dir)
    path = report_dir / f"report{suffix(rev)}.json"
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ReportError(f"cannot read {path}: {e}") from e
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ReportError(f"{path}: unsupported schema_version {report.get('schema_version')}")
    return report


# -- terminal summary --------------------------------------------------------------------

def terminal_summary(report: dict, report_dir: Path) -> str:
    s = report["summary"]
    L = [f"== {s['package']} {s['version'] or ''} ==",
         f"phase/category : {s['phase']} / {s['category']}  [{s['state']}]",
         f"root cause     : {s['root_cause']}",
         f"transition     : {_yn(s['is_transition_related'])}",
         f"confidence     : {s['confidence']}"]
    if s["fix_targets"]:
        L.append("FIX TARGET(S)  :")
        for f in s["fix_targets"]:
            warn = "" if f["is_triaged_package"] else "   <-- NOT the triaged package"
            L.append(f"  * {f['target_pkg']} {f.get('target_version') or ''}"
                     f"{' -> ' + f['patch_file'] if f.get('patch_file') else ''}{warn}")
            L.append(f"    why: {f['rationale']}")
    else:
        L.append("fix target(s) : none proposed")
    if s["next_steps"]:
        L.append("next steps     :")
        L.extend(f"  {i}. {step}" for i, step in enumerate(s["next_steps"], 1))
    L.append(f"report         : {report_dir / ('report' + suffix(report.get('revision', 1)) + '.md')}")
    return "\n".join(L)
