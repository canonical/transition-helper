"""Offline fakes for Launchpad, HTTP and the LLM."""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path

from transition_helper.lp import BugInfo, BuildRec, LPError, PPAInfo, SourcePub
from transition_helper.llm.client import LLMResponse, ToolCall

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class FakeLP:
    pubs: list[SourcePub] = field(default_factory=list)
    builds_by_version: dict[str, list[BuildRec]] = field(default_factory=dict)
    proposed_history: set[tuple[str, str]] = field(default_factory=set)
    bugs: dict[str, list[BugInfo]] = field(default_factory=dict)
    private_ppa: bool = False
    series: tuple[str, ...] = ("tenacious",)
    binaries: dict[str, str] = field(default_factory=dict)
    source_files: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    created: list[dict] = field(default_factory=list)
    attached: list[tuple] = field(default_factory=list)

    def add(self, name, version, location, status="Published", builds=None):
        pocket = {"proposed": "Proposed", "release": "Release", "ppa": "Release"}[location]
        self.pubs.append(SourcePub(name, version, location, pocket, status))
        if builds is not None:
            self.builds_by_version[f"{name}/{version}/{location}"] = [
                BuildRec(arch=a, state=s, log_url=f"https://lp.example/{name}/{version}/{a}/log.txt.gz",
                         dependencies=dep)
                for a, s, dep in [(b + (None,))[:3] for b in builds]]
        return self

    def series_exists(self, series):
        return series in self.series

    def current_series(self):
        return self.series[0]

    def ppa_info(self, ppa):
        return PPAInfo(reference=ppa, private=self.private_ppa)

    def source_exists(self, pkg, series):
        return any(p.name == pkg for p in self.pubs)

    def published_sources(self, pkg, series, ppa=None):
        from transition_helper.versions import vkey

        out = [p for p in self.pubs if p.name == pkg and (ppa or p.location != "ppa")]
        return sorted(out, key=lambda p: vkey(p.version), reverse=True)

    def builds(self, pub):
        return list(self.builds_by_version.get(f"{pub.name}/{pub.version}/{pub.location}", []))

    def went_through_proposed(self, pkg, series, version):
        return (pkg, version) in self.proposed_history

    def source_file_urls(self, pkg, version, ppa=None):
        if (pkg, version) not in self.source_files:
            raise LPError(f"no publication of {pkg} {version} found")
        return self.source_files[(pkg, version)]

    def binary_source(self, binary, series, arch):
        return self.binaries.get(binary)

    def open_bugs(self, pkg):
        return list(self.bugs.get(pkg, []))

    def create_bug(self, pkg, title, description, tags, attachments):
        self.created.append({"pkg": pkg, "title": title, "description": description, "tags": tags})
        return BugInfo(id=4242, title=title, status="New", tags=tags,
                       web_link="https://bugs.launchpad.net/bugs/4242")

    def attach(self, bug_id, filename, data, comment):
        self.attached.append((bug_id, filename, data, comment))


class FakeHTTP:
    """Serves canned responses; records requests. Unknown URLs -> FileNotFoundError."""

    def __init__(self, routes: dict[str, object] | None = None):
        self.routes = dict(routes or {})
        self.requests: list[str] = []

    def _lookup(self, url, params=None):
        self.requests.append(url)
        for key in (url + ("?" + json.dumps(params, sort_keys=True) if params else ""), url):
            if key in self.routes:
                return self.routes[key]
        raise FileNotFoundError(url)

    def head(self, url):
        key = "HEAD " + url
        self.requests.append(key)
        if key not in self.routes:
            raise FileNotFoundError(url)
        return self.routes[key]

    def get_text(self, url, params=None):
        v = self._lookup(url, params)
        return v if isinstance(v, str) else json.dumps(v)

    def get_json(self, url, params=None):
        v = self._lookup(url, params)
        return json.loads(v) if isinstance(v, str) else v

    def get_bytes(self, url):
        v = self._lookup(url)
        return v if isinstance(v, bytes) else str(v).encode()

    def download(self, url, dest: Path, gzip_output=False):
        v = self._lookup(url)
        data = v if isinstance(v, bytes) else str(v).encode()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if gzip_output and data[:2] != b"\x1f\x8b":
            data = gzip.compress(data)
        dest.write_bytes(data)
        return dest


def call(name, _id=None, **arguments):
    return ToolCall(id=_id or f"call_{name}_{id(arguments)}", name=name, arguments=arguments)


class ScriptedLLM:
    """Returns pre-scripted responses; each step is a list of ToolCalls, a string
    (plain text), or a callable(messages) -> either."""

    def __init__(self, steps, tokens_per_call=100):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.tokens_per_call = tokens_per_call

    def chat(self, messages, tools, tool_choice=None):
        self.calls.append({"messages": [dict(m) for m in messages],
                           "tools": [t["function"]["name"] for t in tools],
                           "tool_choice": tool_choice})
        if not self.steps:
            return LLMResponse(content="(script exhausted)", tokens=self.tokens_per_call)
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, str):
            return LLMResponse(content=step, tokens=self.tokens_per_call)
        return LLMResponse(content=None, tool_calls=list(step), tokens=self.tokens_per_call)


def minimal_final(**over):
    report = {
        "category": "FTBFS",
        "root_cause": "The build uses the removed imp module.",
        "is_transition_related": True,
        "confidence": "medium",
        "evidence": [{"log_id": "build:foo:amd64", "lines": "10-12"}],
        "fixes": [],
        "kb_matches": [],
        "suggested_actions": ["upload the fix"],
    }
    report.update(over)
    return report
