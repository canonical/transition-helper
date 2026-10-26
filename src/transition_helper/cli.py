"""`th` command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import EXAMPLE_CONFIG, ConfigError, config_path, load_config
from .progress import Progress
from .workspace import WorkspaceError, find_workspace


def _config():
    try:
        return load_config()
    except ConfigError as e:
        raise SystemExit(f"error: {e}")


def _services(args, quiet: bool = False, refresh: bool = False):
    from .triage import make_services

    try:
        ws = find_workspace()
    except WorkspaceError as e:
        raise SystemExit(f"error: {e}")
    return make_services(ws, _config(), Progress(quiet=quiet), refresh=refresh)


def _report_dir(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if p.is_file():
        p = p.parent
    if not (p / "report.json").exists():
        raise SystemExit(f"error: {p} is not a report directory (no report.json)")
    return p


def cmd_init(args) -> int:
    from .cache import HTTP
    from .init_wizard import run_wizard
    from .lp import LaunchpadClient

    cfg = _config()
    root = Path.cwd()
    lp = LaunchpadClient(root / ".cache", credentials_file=cfg.lp_credentials_file)
    try:
        ws = run_wizard(root, cfg, lp, HTTP(), Progress())
    except (WorkspaceError, KeyboardInterrupt, EOFError) as e:
        print(f"error: {e or 'aborted'}", file=sys.stderr)
        return 1
    print(f"workspace ready: {ws.root}")
    return 0


def cmd_doctor(args) -> int:
    from .doctor import format_checks, run_checks

    cfg, err = None, None
    try:
        cfg = load_config()
    except ConfigError as e:
        err = str(e)
    if args.example_config:
        print(f"# {config_path()}\n{EXAMPLE_CONFIG}")
        return 0
    checks = run_checks(cfg, err, ping_llm=not args.no_ping)
    print(format_checks(checks))
    return 1 if any(c.ok is False for c in checks) else 0


def cmd_triage(args) -> int:
    from .report import load_report, terminal_summary
    from .triage import TriageOptions, triage

    opts = TriageOptions(arch=args.arch, interactive=args.interactive,
                         follow_blockers=args.follow_blockers, no_llm=args.no_llm,
                         force_llm=args.llm, refresh=args.refresh, quiet=args.quiet)
    svc = _services(args, quiet=args.quiet, refresh=args.refresh)
    report_dir, _ = triage(svc, args.srcpkg, opts)
    report = load_report(report_dir)
    print(terminal_summary(report, report_dir))
    if args.interactive:
        from .followup import followup

        return followup(svc, report_dir)
    return 0


def cmd_followup(args) -> int:
    from .followup import followup

    svc = _services(args)
    return followup(svc, _report_dir(args.report_dir))


def cmd_file_bug(args) -> int:
    from .filebug import file_bug

    svc = _services(args)
    return file_bug(svc, _report_dir(args.report_dir))


def cmd_learn(args) -> int:
    from .learn import LearnError, learn

    try:
        ws = find_workspace()
    except WorkspaceError as e:
        raise SystemExit(f"error: {e}")
    try:
        dest = learn(ws, _config(), _report_dir(args.report_dir), global_kb=args.global_kb)
    except LearnError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0 if dest else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="th", description="AI-assisted triage of packages stuck in a transition")
    p.add_argument("--version", action="version", version=f"th {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create a transition workspace in the current (empty) directory")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("doctor", help="check tools, Launchpad access, LLM config and workspace")
    s.add_argument("--no-ping", action="store_true", help="do not send a test request to the LLM")
    s.add_argument("--example-config", action="store_true", help="print an example user config")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("triage", help="work out why a source package is stuck")
    s.add_argument("srcpkg")
    s.add_argument("--arch", help="focus on one architecture (includes ignored_arches)")
    s.add_argument("--interactive", action="store_true", help="enter follow-up after the report")
    s.add_argument("--follow-blockers", action="store_true",
                   help="triage the root of a blocker chain instead")
    s.add_argument("--no-llm", action="store_true", help="deterministic analysis only")
    s.add_argument("--llm", action="store_true", help="run the LLM even when rules would skip it")
    s.add_argument("--refresh", action="store_true", help="bypass caches (excuses, logs, Debian)")
    s.add_argument("--quiet", action="store_true", help="no progress lines")
    s.set_defaults(func=cmd_triage)

    for name, func, helptext in (
        ("followup", cmd_followup, "continue a report interactively"),
        ("file-bug", cmd_file_bug, "file a Launchpad bug from a report (asks y/N)"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("report_dir")
        s.set_defaults(func=func)

    s = sub.add_parser("learn", help="create a KB signature from a report")
    s.add_argument("report_dir")
    s.add_argument("--global", dest="global_kb", action="store_true",
                   help="save to the shared global KB repo instead of <workspace>/kb")
    s.set_defaults(func=cmd_learn)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
