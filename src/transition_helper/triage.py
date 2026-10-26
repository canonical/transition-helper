"""The triage pipeline: locate -> classify -> blockers -> infra -> evidence -> LLM -> render."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .autopkgtest import AutopkgtestClient, retry_url
from .blockers import BlockerChain, blocker_chain
from .cache import HTTP, Cache, FetchError
from .classify import Category, Classification, classify
from .config import UserConfig
from .debian_delta import DebianDelta, debian_delta
from .excuses import EXCUSES_STALE_HOURS, ExcusesError, ExcusesIndex, load_excuses
from .infra import assess_build, assess_test
from .kb import KB
from .llm.agent import PrivateDataRefused, Transcript, check_private, run_agent, system_prompt
from .llm.client import LLMClient, LLMError, LiteLLMClient
from .llm.tools import ToolContext, ToolRegistry
from .locate import LocateResult, State, locate
from .logs import LogError, LogStore, build_log_id, dedupe, failure_signature, failure_windows, test_log_id
from .lp import LaunchpadClient, LPClient, LPError
from .patches import PatchSession
from .progress import Progress
from .sources import SourceManager
from .versions import default_entry
from .workspace import Workspace

MAX_TEST_LOGS = 12


@dataclass
class TriageOptions:
    arch: str | None = None
    interactive: bool = False
    follow_blockers: bool = False
    no_llm: bool = False
    force_llm: bool = False
    refresh: bool = False
    quiet: bool = False


@dataclass
class Services:
    ws: Workspace
    cfg: UserConfig
    progress: Progress
    cache: Cache
    http: HTTP
    lp: LPClient
    store: LogStore
    sources: SourceManager
    kb: KB
    autopkgtest: AutopkgtestClient
    _excuses: ExcusesIndex | None = None
    excuses_error: str | None = None

    def excuses(self) -> ExcusesIndex | None:
        if self._excuses is None and self.excuses_error is None:
            self.progress.step("excuses", f"loading update_excuses for {self.ws.transition.series}")
            try:
                self._excuses = load_excuses(self.cache, self.http, self.ws.transition.series,
                                             warn=self.progress.warn)
                age = self._excuses.age_hours()
                if age is not None:
                    self.progress.step("excuses", f"generated {self._excuses.generated[:16]} UTC "
                                       f"({age:.1f}h ago)")
                    if age > EXCUSES_STALE_HOURS:
                        self.progress.warn(f"excuses are {age:.0f}h old; britney may be stuck, "
                                           "so migration data may be out of date")
                for err in self._excuses.errors[:5]:
                    self.progress.warn(f"excuses: {err}")
            except (ExcusesError, FetchError, FileNotFoundError, OSError) as e:
                self.excuses_error = str(e)
                self.progress.warn(f"cannot load excuses: {e}")
        return self._excuses


def make_services(ws: Workspace, cfg: UserConfig, progress: Progress, refresh: bool = False,
                  lp: LPClient | None = None, http: HTTP | None = None) -> Services:
    cache = Cache(ws.cache_dir, refresh=refresh)
    http = http or HTTP()
    if lp is None:
        lp = LaunchpadClient(ws.cache_dir, credentials_file=cfg.lp_credentials_file)
    t = ws.transition
    kb = KB.load([cfg.global_kb_path, ws.kb_dir])
    for err in kb.errors:
        progress.warn(f"kb: {err}")
    return Services(
        ws=ws, cfg=cfg, progress=progress, cache=cache, http=http, lp=lp,
        store=LogStore(ws.cache_dir / "logs", http, refresh=refresh),
        sources=SourceManager(ws.cache_dir, lp, http, progress,
                              ppa=t.ppa if t.build_location == "ppa" else None),
        kb=kb, autopkgtest=AutopkgtestClient(http, cache),
    )


@dataclass
class LogEvidence:
    log_id: str
    url: str
    kind: str
    arch: str
    pkg: str
    lines: int = 0
    windows: list = field(default_factory=list)
    kb: list = field(default_factory=list)
    signature: str = ""
    infra: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {"log_id": self.log_id, "url": self.url, "kind": self.kind, "arch": self.arch,
                "pkg": self.pkg, "lines": self.lines,
                "windows": [w.to_dict() for w in self.windows],
                "kb": [m.to_dict() for m in self.kb], "signature": self.signature,
                "infra": self.infra, "error": self.error}


@dataclass
class TriageData:
    pkg: str
    opts: TriageOptions
    loc: LocateResult
    cls: Classification
    excuse: dict | None = None
    chain: BlockerChain | None = None
    logs: list[LogEvidence] = field(default_factory=list)
    groups: list = field(default_factory=list)
    test_infra: list[dict] = field(default_factory=list)
    bugs: list = field(default_factory=list)
    debian: DebianDelta | None = None
    queued: list[dict] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    llm: dict = field(default_factory=dict)
    final_report: dict | None = None
    proposals: list = field(default_factory=list)
    patches: dict[str, str] = field(default_factory=dict)
    known_versions: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    excuses_generated: str | None = None

    @property
    def representative_logs(self) -> list[LogEvidence]:
        reps = {g.representative for g in self.groups}
        return [log for log in self.logs if log.log_id in reps]

    def kb_ids(self) -> list[str]:
        return sorted({m.sig_id for log in self.logs for m in log.kb})


def triage(svc: Services, pkg: str, opts: TriageOptions,
           llm_client: LLMClient | None = None, _followed: bool = False) -> tuple[Path, TriageData]:
    p, t = svc.progress, svc.ws.transition
    excuses = svc.excuses()
    try:
        loc = locate(svc.lp, t, pkg, excuses, arch=opts.arch)
    except LPError as e:
        raise SystemExit(f"error: {e}")
    p.step("locate", loc.summary())
    for note in loc.notes:
        p.step("locate", note)
    excuse = excuses.get(pkg) if excuses else None
    ignored = set(t.ignored_arches)
    cls = classify(loc, excuse, ignored_arches=ignored, arch=opts.arch)
    p.step("classify", f"{cls.phase} / {', '.join(cls.categories)}")
    data = TriageData(pkg=pkg, opts=opts, loc=loc, cls=cls,
                      excuse=excuse.to_dict() if excuse else None)
    data.excuses_generated = excuses.generated if excuses else None
    data.known_versions = {ft.test_pkg: ft.test_version for ft in cls.failing_tests
                           if ft.test_version}

    # 3. blocker chain
    if {Category.DEPENDS_ON_UNMIGRATED, Category.UNINSTALLABLE} & set(cls.categories) and excuses:
        def bin_to_src(binary: str, arch: str) -> str | None:
            return svc.lp.binary_source(binary, t.series, arch)

        data.chain = blocker_chain(excuses, pkg, bin_to_src)
        for line in data.chain.render():
            p.step("blockers", line)
        roots = [r for r in data.chain.roots if r != pkg]
        if roots:
            p.step("blockers", f"root blocker(s): {', '.join(roots)} -> th triage {roots[0]}")
            if opts.follow_blockers and not _followed:
                p.step("blockers", f"--follow-blockers: triaging {roots[0]} instead")
                return triage(svc, roots[0], opts, llm_client, _followed=True)

    # 4/5. evidence
    gather_evidence(svc, data)
    data.commands = build_commands(svc, data)
    data.next_steps = deterministic_next_steps(svc, data)

    report_dir = svc.ws.new_report_dir(pkg)
    (report_dir / "evidence").mkdir(exist_ok=True)

    # 6. LLM
    run, why = should_run_llm(data, opts)
    data.llm = {"ran": False, "reason": why, "model": svc.cfg.llm.model or None}
    if run:
        run_llm(svc, data, report_dir, llm_client)
    else:
        p.step("llm", f"skipped ({why})")

    # 7. render
    from .report import write_report

    write_report(svc.ws, data, report_dir, revision=1)
    return report_dir, data


# -- evidence ------------------------------------------------------------------------

def _process_log(svc: Services, ev: LogEvidence) -> None:
    try:
        meta = svc.store.fetch(ev.log_id, ev.url)
        ev.lines = meta.lines
        ev.windows = failure_windows(svc.store, ev.log_id)
        ev.kb = svc.kb.match_log(svc.store, ev.log_id)
        ev.signature = failure_signature(ev.windows)
        for m in ev.kb:
            svc.progress.step("kb", f"matched {m.sig_id} in {ev.log_id}")
    except (LogError, FetchError, FileNotFoundError, OSError) as e:
        ev.error = str(e)
        svc.progress.warn(f"log {ev.log_id}: {e}")


def gather_evidence(svc: Services, data: TriageData) -> None:
    p, t, cls, loc = svc.progress, svc.ws.transition, data.cls, data.loc
    # build logs
    for issue in cls.build_issues:
        if issue.category == Category.FTBFS and not issue.log_url:
            msg = (f"{issue.arch}: failed to build but Launchpad has no build log "
                   f"(builder problem?); see {issue.web_link}")
            data.warnings.append(msg)
            p.warn(msg)
        if issue.category in (Category.FTBFS, Category.BUILD_INFRA) and issue.log_url:
            ev = LogEvidence(build_log_id(data.pkg, issue.arch), issue.log_url, "build",
                             issue.arch, data.pkg)
            p.step("evidence", f"fetching build log {issue.arch}")
            _process_log(svc, ev)
            data.logs.append(ev)
    failing_build_arches = {i.arch for i in cls.build_issues if i.category == Category.FTBFS}
    all_build_arches = {b.arch for b in loc.builds}
    for ev in data.logs:
        if ev.kind == "build" and not ev.error:
            tail = [line for _, line in svc.store.tail(ev.log_id, 150)]
            ev.infra = assess_build(tail, failing_build_arches, all_build_arches).to_dict()

    # test logs + infra/flaky heuristics
    tested_arches: dict[str, set[str]] = {}
    for tr in (data.excuse or {}).get("tests", []):
        tested_arches.setdefault(tr["test_pkg"], set()).add(tr["arch"])
    failing_by_pkg: dict[str, set[str]] = {}
    for ft in cls.failing_tests:
        failing_by_pkg.setdefault(ft.test_pkg, set()).add(ft.arch)
    has_unmigrated = bool(cls.blockers)
    for ft in cls.failing_tests[:MAX_TEST_LOGS]:
        ev = None
        if ft.log_url and ft.run_id:
            ev = LogEvidence(test_log_id(ft.test_pkg, ft.arch, ft.run_id), ft.log_url,
                             "autopkgtest", ft.arch, ft.test_pkg)
            p.step("evidence", f"fetching test log {ft.test_pkg}/{ft.arch} ({ft.kind})")
            _process_log(svc, ev)
            data.logs.append(ev)
        history = svc.autopkgtest.history(t.series, ft.arch, ft.test_pkg, limit=10)
        latest = next((r for r in history if r.run_id == ft.run_id), None)
        lines = None
        if ev and not ev.error:
            lines = [line for _, line in svc.store.tail(ev.log_id, 200)]
        verdict = assess_test(ft.status, ft.arch, lines, history,
                              failing_by_pkg.get(ft.test_pkg, set()),
                              tested_arches.get(ft.test_pkg, set()),
                              trigger_count=len(ft.triggers), has_unmigrated_deps=has_unmigrated,
                              latest_outcome=latest.outcome if latest else None)
        info = {"test_pkg": ft.test_pkg, "arch": ft.arch, "run_id": ft.run_id,
                "log_id": ev.log_id if ev else None, **verdict.to_dict(),
                "history_runs": [r.to_dict() for r in history[:10]]}
        data.test_infra.append(info)
        if ev:
            ev.infra = verdict.to_dict()
        if verdict.flagged:
            p.step("infra", f"{ft.test_pkg}/{ft.arch}: likely "
                   f"{'infra' if verdict.likely_infra else 'flaky'} ({'; '.join(verdict.reasons)})")
    if len(cls.failing_tests) > MAX_TEST_LOGS:
        data.warnings.append(f"only the first {MAX_TEST_LOGS} of {len(cls.failing_tests)} "
                             "failing test logs were downloaded")

    # dedupe across arches
    data.groups = dedupe({ev.log_id: (ev.signature, [m.sig_id for m in ev.kb])
                          for ev in data.logs if not ev.error})
    for g in data.groups:
        if len(g.members) > 1:
            p.step("evidence", f"identical failure on {len(g.members)} logs; "
                   f"representative {g.representative}")

    # pending tests: what is queued/running
    if Category.AUTOPKGTEST_PENDING in cls.categories:
        for test_pkg in sorted({pt.test_pkg for pt in cls.pending_tests})[:20]:
            data.queued.extend({"test_pkg": test_pkg, **q}
                               for q in svc.autopkgtest.queued(t.series, test_pkg))

    # LP bugs
    try:
        data.bugs = [b.to_dict() for b in svc.lp.open_bugs(data.pkg)]
        tagged = [b for b in data.bugs if set(b["tags"]) & set(t.lp_bug_tags)]
        p.step("bugs", f"{len(data.bugs)} open bug(s)"
               + (f", {len(tagged)} with transition tags: "
                  + ", ".join(f"#{b['id']}" for b in tagged) if tagged else ""))
    except LPError as e:
        data.warnings.append(str(e))
        p.warn(str(e))

    # Debian delta
    if loc.target_version or loc.versions.get("release"):
        data.debian = debian_delta(svc.http, svc.cache, data.pkg,
                                   loc.target_version or loc.versions.get("release"),
                                   [t.name, t.toolchain_source, t.new_version, "FTBFS"])
        p.step("debian", data.debian.summary())


# -- decisions ---------------------------------------------------------------------------

def should_run_llm(data: TriageData, opts: TriageOptions) -> tuple[bool, str]:
    if opts.no_llm:
        return False, "--no-llm"
    if opts.force_llm:
        return True, "requested by operator"
    cats = set(data.cls.categories)
    if Category.FTBFS in cats:
        if not any(ev.kind == "build" and not ev.error for ev in data.logs):
            return False, "no build log available to analyse"
        if data.logs and all((ev.infra or {}).get("likely_infra") for ev in data.logs if ev.kind == "build"):
            return False, "build failure looks like infra"
        return True, "FTBFS"
    if Category.AUTOPKGTEST_REGRESSION in cats:
        if data.test_infra and all(i.get("flagged") for i in data.test_infra):
            return False, "all failing tests look like infra/flaky; retry first"
        return True, "autopkgtest regression"
    if Category.UNINSTALLABLE in cats:
        if data.chain and [r for r in data.chain.roots if r != data.pkg]:
            return False, "uninstallability explained by blocker chain"
        return True, "unclear uninstallability"
    return False, f"not needed for {data.cls.primary}"


def deterministic_next_steps(svc: Services, data: TriageData) -> list[str]:
    t, loc, cls = svc.ws.transition, data.loc, data.cls
    out: list[str] = []
    cats = set(cls.categories)
    if loc.state == State.NOT_FOUND:
        out.append(f"{data.pkg} is not published in {t.series}; check the name")
    if loc.state == State.NOT_REBUILT:
        out.append("no rebuild has been uploaded for this transition; decide whether one is needed")
    if loc.state == State.MIGRATED:
        out.append("nothing to do: the package migrated")
    if loc.state == State.BUILT_IN_PPA_NOT_IN_PROPOSED:
        out.append("all arches built in PPA; not in -proposed yet (copy is a human review step)")
    if loc.state == State.IN_PROPOSED_NO_EXCUSE_YET:
        out.append("wait for the next britney run to produce an excuse")
    for issue in cls.build_issues:
        if issue.category == Category.DEPWAIT:
            out.append(f"{issue.arch}: build waits on {issue.missing_dependency or 'a dependency'}; "
                       "make it available (or check it is meant to be)")
    no_log = [i.arch for i in cls.build_issues if i.category == Category.FTBFS and not i.log_url]
    if no_log:
        out.append(f"no build log on {','.join(no_log)}: check the Launchpad build page; "
                   "a retry may be needed")
    if Category.BUILD_INFRA in cats:
        out.append("builder/chroot/upload problem: retry the build from the Launchpad build page")
    if Category.BUILD_PENDING in cats:
        out.append("builds still pending: wait")
    for i in data.test_infra:
        if i.get("flagged"):
            out.append(f"{i['test_pkg']}/{i['arch']} looks {'infra' if i['likely_infra'] else 'flaky'}: "
                       "retry it (URL in commands.sh)")
    if Category.AUTOPKGTEST_PENDING in cats:
        out.append(f"{len(cls.pending_tests)} test(s) still running/queued: wait")
    if data.chain:
        roots = [r for r in data.chain.roots if r != data.pkg]
        if roots:
            out.append(f"blocked by {', '.join(roots)}: run 'th triage {roots[0]}'")
        for n in data.chain.not_in_proposed:
            out.append(f"{n} blocks this but has no excuse (not in -proposed?)")
    if Category.MISSING_BUILD in cats and loc.state != State.BUILD_FAILED:
        out.append("britney reports missing builds; check the Launchpad build records")
    if Category.BLOCKED in cats:
        out.append("blocked by a block request/bug: " + ("; ".join(n for n in cls.notes if "bug" in n) or "see excuse"))
    if data.debian and data.debian.hint:
        out.append(data.debian.hint)
    tagged = [b for b in data.bugs if set(b["tags"]) & set(t.lp_bug_tags)]
    for b in tagged[:3]:
        out.append(f"existing transition bug LP: #{b['id']} ({b['title']})")
    return out


def build_commands(svc: Services, data: TriageData) -> list[str]:
    t = svc.ws.transition
    ppa = t.ppa if t.build_location == "ppa" else None
    out: list[str] = []
    for i in data.test_infra:
        ft = next((f for f in data.cls.failing_tests
                   if f.test_pkg == i["test_pkg"] and f.arch == i["arch"]), None)
        if ft is None:
            continue
        url = retry_url(t.series, ft.arch, ft.test_pkg, ft.triggers, ppa=ppa if data.loc.target_location == "ppa" else None,
                        all_proposed=i.get("needs_all_proposed", False))
        why = "; ".join(i.get("reasons", [])) or "retry"
        prefix = "" if i.get("flagged") else "# (not flagged as infra) "
        out.append(f"# retry {ft.test_pkg}/{ft.arch}: {why}\n{prefix}xdg-open '{url}'")
    if data.chain:
        for r in [r for r in data.chain.roots if r != data.pkg][:3]:
            out.append(f"# root blocker\nth triage {r}")
    return out


# -- LLM -----------------------------------------------------------------------------------

def bundle_summary(svc: Services, data: TriageData, max_chars: int = 30_000) -> str:
    """Initial user message: deterministic findings + representative failure windows."""
    from .llm.tools import untrusted

    t, loc, cls = svc.ws.transition, data.loc, data.cls
    out = [f"# Package {data.pkg}",
           f"State: {loc.state} — {loc.summary()}",
           "Versions: " + ", ".join(f"{k}={v}" for k, v in loc.versions.items()),
           f"Phase/categories: {cls.phase} / {', '.join(cls.categories)}"]
    out += [f"Note: {n}" for n in loc.notes + cls.notes]
    if cls.build_issues:
        out.append("## Build issues")
        out += [f"- {b.arch}: {b.state}" + (f" (needs {b.missing_dependency})" if b.missing_dependency else "")
                for b in cls.build_issues]
    if cls.failing_tests:
        out.append("## Failing autopkgtests")
        for ft in cls.failing_tests:
            inf = next((i for i in data.test_infra if i["test_pkg"] == ft.test_pkg and i["arch"] == ft.arch), {})
            out.append(f"- {ft.test_pkg} {ft.test_version or ''} on {ft.arch}: {ft.status} ({ft.kind}); "
                       f"triggers {' '.join(ft.triggers)}; history {' '.join(inf.get('history', [])[:8]) or '-'}"
                       + (f"; heuristics: {'; '.join(inf.get('reasons', []))}" if inf.get("reasons") else ""))
    if cls.uninstallable:
        out.append("## Uninstallable")
        out += [f"- {u}" for u in cls.uninstallable[:20]]
    if data.chain:
        out.append("## Blocker chain")
        out += [f"- {line}" for line in data.chain.render()]
    kb_ids = data.kb_ids()
    if kb_ids:
        out.append("## Knowledge-base matches (hints, verify them)")
        for sid in kb_ids:
            sig = svc.kb.get(sid)
            if sig:
                out.append(f"- {sid}: {sig.explanation} Fix recipe: {sig.fix_recipe}")
    if data.bugs:
        out.append("## Open Launchpad bugs")
        out += [f"- LP: #{b['id']} [{b['status']}] {b['title']} tags={','.join(b['tags'])}" for b in data.bugs[:10]]
    if data.debian:
        out.append("## Debian")
        out.append(data.debian.summary())
    out.append("## Logs in the store (ids for grep_log/read_log/tail_log)")
    for g in data.groups:
        others = [m for m in g.members if m != g.representative]
        out.append(f"- {g.representative}" + (f" (same failure as {', '.join(others)})" if others else ""))
    for ev in data.logs:
        if ev.error:
            out.append(f"- {ev.log_id}: download failed ({ev.error})")
    head = "\n".join(out)
    budget = max_chars - len(head) - 500
    excerpts = []
    for ev in data.representative_logs:
        for w in ev.windows:
            block = untrusted(f"log {ev.log_id} lines {w.start}-{w.end}", w.render())
            if len(block) > budget:
                excerpts.append(f"[window {ev.log_id} {w.start}-{w.end} omitted for size; use read_log]")
                continue
            budget -= len(block)
            excerpts.append(block)
    docs = ", ".join(t.docs) if t.docs else "none"
    return (head + "\n## Failure windows (representative logs)\n" + "\n".join(excerpts)
            + f"\n\nTransition docs available via search_docs: {docs}\n"
            f"Triaged version: {loc.target_version}. Start the analysis.")


def make_tool_context(svc: Services, data: TriageData, report_dir: Path, revision: int = 1,
                      ) -> ToolContext:
    t = svc.ws.transition
    kind = "build" if data.cls.phase == "build" else "autopkgtest"
    session = PatchSession(
        svc.sources, svc.ws.cache_dir / "work" / report_dir.name,
        svc.cfg.maintainer_or_placeholder(), t.series,
        default_entry(kind, t.toolchain_source, t.new_version))
    return ToolContext(
        workspace=svc.ws, pkg=data.pkg, target_version=data.loc.target_version,
        store=svc.store, sources=svc.sources, kb=svc.kb, progress=svc.progress,
        patches=session, report_dir=report_dir, lp=svc.lp, excuses=svc.excuses(),
        http=svc.http, cache=svc.cache, autopkgtest=svc.autopkgtest,
        known_versions=dict(data.known_versions),
        max_result_chars=svc.cfg.budgets.max_tool_result_chars, revision=revision,
    )


def privacy_check(svc: Services, data: TriageData) -> None:
    t = svc.ws.transition
    private = False
    if t.build_location == "ppa" and t.ppa:
        try:
            private = svc.lp.ppa_info(t.ppa).private
        except LPError:
            private = True  # cannot tell: be conservative
    data.loc.ppa_private = private
    check_private(private, svc.cfg.allow_private_data)


def run_llm(svc: Services, data: TriageData, report_dir: Path,
            client: LLMClient | None) -> None:
    p = svc.progress
    try:
        privacy_check(svc, data)
    except PrivateDataRefused as e:
        p.warn(str(e))
        data.llm.update(reason=str(e))
        return
    if client is None:
        try:
            client = LiteLLMClient(svc.cfg.llm)
        except LLMError as e:
            p.warn(f"LLM unavailable: {e}")
            data.llm.update(reason=f"LLM unavailable: {e}")
            return
    ctx = make_tool_context(svc, data, report_dir)
    registry = ToolRegistry(ctx)
    transcript = Transcript(report_dir / "evidence" / "transcript.jsonl")
    bundle = bundle_summary(svc, data)
    (report_dir / "evidence" / "bundle_summary.md").write_text(bundle)
    messages = [{"role": "system", "content": system_prompt(registry, svc.cfg.budgets)},
                {"role": "user", "content": bundle}]
    transcript.write("system", content=messages[0]["content"])
    transcript.write("user", content=bundle)
    p.step("llm", f"starting analysis with {svc.cfg.llm.model or 'configured model'}")
    result = run_agent(client, registry, svc.cfg.budgets, p, messages, transcript)
    data.llm.update(ran=True, iterations=result.iterations, tokens=result.tokens,
                    forced=result.forced, errors=result.errors, reason=data.llm.get("reason"))
    data.final_report = result.final_report
    data.proposals = [prop.to_dict() for props in ctx.patches.proposals.values() for prop in props]
    data.patches = dict(ctx.written_patches)


def report_version() -> str:
    return __version__
