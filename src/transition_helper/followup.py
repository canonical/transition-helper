"""`th followup`: reload a report and continue the conversation with the same tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .llm.agent import (PrivateDataRefused, Transcript, check_private, run_agent,
                        system_prompt)
from .llm.client import LLMClient, LLMError, LiteLLMClient
from .llm.tools import ToolContext, ToolRegistry
from .lp import LPError
from .patches import PatchSession, Proposal
from .report import latest_revision, load_report, terminal_summary, write_files
from .triage import Services
from .versions import default_entry

HELP = """\
Ask a question, or use:
  /report   ask the model for a revised final report (written as a new revision)
  /patches  list accepted patches
  /summary  show the current summary
  /quit     leave (also Ctrl-D)"""

REVISE_PROMPT = ("Produce a revised final_report now that reflects everything discussed, "
                 "including any new or replaced patches.")


def transcript_summary(path: Path, max_items: int = 60) -> str:
    if not path.exists():
        return "(no transcript)"
    items = []
    for line in path.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for c in ev.get("tool_calls") or []:
                args = {k: (v if not isinstance(v, str) or len(v) < 120 else v[:120] + "…")
                        for k, v in (c.get("arguments") or {}).items()}
                items.append(f"- {c['name']}({json.dumps(args)})")
            if ev.get("content") and not ev.get("tool_calls"):
                items.append(f"- said: {ev['content'][:300]}")
    if len(items) > max_items:
        items = items[:max_items // 2] + ["- …"] + items[-max_items // 2:]
    return "\n".join(items) or "(empty)"


def context_message(report: dict, report_dir: Path) -> str:
    trimmed = {k: report.get(k) for k in ("package", "version", "summary", "classification",
                                          "blockers", "kb_matches", "final_report", "proposals")}
    for p in trimmed.get("proposals") or []:
        p.pop("unified_diff", None)
    logs = [f"- {log['log_id']} ({log['lines']} lines)" for log in report["evidence"]["logs"]]
    return ("This is a follow-up session on an earlier triage. The operator will ask questions. "
            "Answer in plain text (that ends your turn), or use tools first. Only call "
            "final_report when the operator asks for a revised report.\n\n"
            f"## Earlier report (JSON)\n{json.dumps(trimmed, indent=1, default=str)[:20000]}\n\n"
            f"## Logs in the store\n" + "\n".join(logs) + "\n\n"
            f"## Earlier tool calls\n{transcript_summary(report_dir / 'evidence' / 'transcript.jsonl')}")


def make_context(svc: Services, report: dict, report_dir: Path, revision: int) -> ToolContext:
    t = svc.ws.transition
    kind = "build" if report["classification"]["phase"] == "build" else "autopkgtest"
    session = PatchSession(svc.sources, svc.ws.cache_dir / "work" / f"{report_dir.name}-v{revision}",
                           svc.cfg.maintainer_or_placeholder(), t.series,
                           default_entry(kind, t.toolchain_source, t.new_version))
    session.load([Proposal.from_dict(p) for p in report.get("proposals") or []])
    known = {ft["test_pkg"]: ft["test_version"] for ft in report["classification"]["failing_tests"]
             if ft.get("test_version")}
    ctx = ToolContext(
        workspace=svc.ws, pkg=report["package"], target_version=report.get("version"),
        store=svc.store, sources=svc.sources, kb=svc.kb, progress=svc.progress, patches=session,
        report_dir=report_dir, lp=svc.lp, excuses=svc.excuses(), http=svc.http, cache=svc.cache,
        autopkgtest=svc.autopkgtest, known_versions=known,
        max_result_chars=svc.cfg.budgets.max_tool_result_chars, revision=revision,
    )
    for p in session.proposals:
        ctx.fetched_trees[p] = session.versions[p]
    return ctx


def new_revision(report: dict, ctx: ToolContext, final: dict | None, llm_stats: dict) -> dict:
    rev = dict(report)
    rev["revision"] = ctx.revision
    if final is not None:
        rev["final_report"] = final
    rev["proposals"] = [p.to_dict() for ps in ctx.patches.proposals.values() for p in ps]
    patches = dict(report.get("patches") or {})
    patches.update(ctx.written_patches)
    rev["patches"] = patches
    rev["llm"] = dict(report.get("llm") or {}) | {"followup": llm_stats, "ran": True}
    return rev


def followup(svc: Services, report_dir: Path, client: LLMClient | None = None,
             input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print) -> int:
    report = load_report(report_dir)
    t = svc.ws.transition
    private = False
    if t.build_location == "ppa" and t.ppa:
        try:
            private = svc.lp.ppa_info(t.ppa).private
        except LPError:
            private = True
    try:
        check_private(private, svc.cfg.allow_private_data)
    except PrivateDataRefused as e:
        out(f"error: {e}")
        return 1
    if client is None:
        try:
            client = LiteLLMClient(svc.cfg.llm)
        except LLMError as e:
            out(f"error: {e}")
            return 1
    revision = latest_revision(report_dir) + 1
    ctx = make_context(svc, report, report_dir, revision)
    registry = ToolRegistry(ctx)
    transcript = Transcript(report_dir / "evidence" / "transcript.jsonl")
    transcript.write("followup_start", revision=revision)
    messages = [{"role": "system", "content": system_prompt(registry, svc.cfg.budgets)},
                {"role": "user", "content": context_message(report, report_dir)},
                {"role": "assistant", "content": "Understood. What would you like to know?"}]
    out(f"follow-up on {report['package']} {report.get('version') or ''} "
        f"(new outputs will be revision {revision}). /help for commands.")
    written = False
    while True:
        try:
            line = input_fn("th> ").strip()
        except (EOFError, KeyboardInterrupt):
            out("")
            break
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            break
        if line == "/help":
            out(HELP)
            continue
        if line == "/patches":
            props = [p for ps in ctx.patches.proposals.values() for p in ps]
            out("\n".join(f"{p.target_pkg}: {p.patch_name} ({', '.join(p.files)})" for p in props)
                or "no patches")
            continue
        if line == "/summary":
            out(terminal_summary(report, report_dir))
            continue
        revise = line == "/report"
        messages.append({"role": "user", "content": REVISE_PROMPT if revise else line})
        transcript.write("user", content=messages[-1]["content"])
        result = run_agent(client, registry, svc.cfg.budgets, svc.progress, messages, transcript,
                           followup=not revise)
        stats = {"iterations": result.iterations, "tokens": result.tokens, "errors": result.errors}
        if result.final_report is not None or (ctx.written_patches and revise):
            report = new_revision(report, ctx, result.final_report, stats)
            md = write_files(report_dir, report)
            written = True
            out(terminal_summary(report, report_dir))
            out(f"wrote {md.name}")
            # further revisions in this session get a new number
            ctx.revision += 1
            ctx.written_patches = {}
        elif result.answer is not None:
            out(result.answer)
        elif result.errors:
            out("error: " + "; ".join(result.errors))
        if ctx.written_patches and not revise:
            out(f"patches updated: {', '.join(ctx.written_patches.values())} "
                "(use /report to write a matching report revision)")
    if ctx.written_patches and not written:
        report = new_revision(report, ctx, None, {})
        md = write_files(report_dir, report)
        out(f"wrote {md.name} with the new patches")
    return 0
