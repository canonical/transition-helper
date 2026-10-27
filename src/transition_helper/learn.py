"""`th learn`: turn a report into a new KB signature."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import yaml

from .config import UserConfig
from .editor import EditorError, edit
from .kb import KB, make_signature, validate_signature
from .logs import LogError, LogStore, normalise
from .report import load_report
from .workspace import Workspace

ELAPSED_RE = re.compile(r"^\s*\d+s\s+")
MAX_CANDIDATES = 5


class LearnError(Exception):
    pass


def candidate_patterns(report: dict) -> list[str]:
    """Escaped failure lines from the representative logs, most specific first."""
    reps = {g["representative"] for g in report["evidence"].get("groups", [])}
    seen: set[str] = set()
    out: list[str] = []
    for log in report["evidence"]["logs"]:
        if reps and log["log_id"] not in reps:
            continue
        for w in log.get("windows", []):
            for _, line in w.get("trigger_lines", []):
                text = ELAPSED_RE.sub("", line).strip()
                if len(text) < 12 or normalise(text) in seen:
                    continue
                seen.add(normalise(text))
                rx = re.escape(text)
                # keep it portable across versions/paths: numbers -> \d+
                rx = re.sub(r"(?<!\\)\d+", r"\\d+", rx)
                out.append(rx)
    # prefer lines that name an error type over generic make/dpkg noise
    out.sort(key=lambda p: (not re.search(r"Error|error:|No module|undefined|FAIL", p), len(p)))
    return out[:MAX_CANDIDATES]


def prefill(report: dict, scope: str) -> dict:
    fr = report.get("final_report") or {}
    t = report["transition"]
    kinds = sorted({"build" if log["kind"] == "build" else "autopkgtest"
                    for log in report["evidence"]["logs"]}) or ["build"]
    slug = re.sub(r"[^a-z0-9]+", "-", f"{t['name']}-{report['package']}".lower()).strip("-")
    link = (report.get("bug") or {}).get("web_link") or next(
        (log["url"] for log in report["evidence"]["logs"]), "")
    recipe = "; ".join(f"{f['target_pkg']}: {f['rationale']}" for f in fr.get("fixes", []))
    return {
        "id": slug,
        "scope": scope,
        "applies_to": kinds,
        "patterns": candidate_patterns(report) or ["REPLACE-ME"],
        "explanation": fr.get("root_cause") or report["summary"]["root_cause"],
        "fix_recipe": recipe or "TODO",
        "examples": [{"package": report["package"], "version": report.get("version"), "link": link}],
    }


def rematch(sig_data: dict, report: dict, store: LogStore) -> list[str]:
    sig = make_signature(sig_data, "<edited>")
    kb = KB([sig])
    hits = []
    for log in report["evidence"]["logs"]:
        try:
            for m in kb.match_log(store, log["log_id"]):
                n, line = m.lines[0]
                hits.append(f"{log['log_id']}:{n}: {line[:160]}")
        except LogError:
            continue
    return hits


HEADER = """\
# New KB signature drafted from {report}.
# Edit, save and quit. Patterns are Python regexes matched per log line.
# Delete everything to abort.
"""


def learn(ws: Workspace, cfg: UserConfig, report_dir: Path, global_kb: bool = False,
          editor: str | None = None, input_fn: Callable[[str], str] = input,
          out: Callable[[str], None] = print) -> Path | None:
    report = load_report(report_dir)
    if global_kb and not cfg.global_kb_path:
        raise LearnError("global_kb_path is not set in the user config")
    dest_dir = cfg.global_kb_path if global_kb else ws.kb_dir
    scope = "global" if global_kb else ws.transition.name
    store = LogStore(ws.cache_dir / "logs")
    text = HEADER.format(report=report_dir) + yaml.safe_dump(prefill(report, scope),
                                                             sort_keys=False, width=100)
    while True:
        try:
            text = edit(text, editor)
        except EditorError as e:
            raise LearnError(str(e)) from e
        body = "\n".join(line for line in text.splitlines() if not line.startswith("#")).strip()
        if not body:
            out("aborted (empty signature)")
            return None
        problem = None
        try:
            data = yaml.safe_load(body)
            errors = validate_signature(data)
            if errors:
                problem = "; ".join(errors)
            else:
                hits = rematch(data, report, store)
                if not hits:
                    problem = "the signature matches none of this report's logs"
        except yaml.YAMLError as e:
            problem = f"invalid YAML: {e}"
        if problem is None:
            break
        out(f"invalid: {problem}")
        if input_fn("Edit again? [Y/n] ").strip().lower() in ("n", "no"):
            out("aborted")
            return None
        text = f"# ERROR: {problem}\n" + text
    out("matches:")
    for h in hits:
        out(f"  {h}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{data['id']}.yaml"
    if dest.exists() and input_fn(f"{dest} exists. Overwrite? [y/N] ").strip().lower() not in ("y", "yes"):
        out("not saved")
        return None
    dest.write_text(yaml.safe_dump(data, sort_keys=False, width=100))
    out(f"saved {dest}")
    if global_kb:
        out(f"remember to commit it in {dest_dir}")
    return dest
