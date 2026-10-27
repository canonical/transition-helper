# transition-helper (`th`)

AI-assisted triage of source packages that are stuck in an Ubuntu transition
(python3.x → 3.y, a gcc/llvm bump, …). Give it one stuck source package. It works out
*why* the package is stuck, writes a report and, where that makes sense, proposes
patches and next steps.

The operator stays responsible for every decision and every upload. `th`:

- only reads Launchpad, britney's excuses, autopkgtest.ubuntu.com and Debian;
- never uploads, copies, retries or test-builds anything;
- has a single write action, `th file-bug`. It shows the full draft and asks y/N, and it is
  never available to the LLM.

Rules classify the problem first, from structured data. The LLM is used only for the
parts that need reasoning: reading logs, finding the root cause and proposing a fix. It
runs in a bounded, read-only tool loop.

## Install

Requires Python ≥ 3.12 and, on the host, `dpkg-dev`, `quilt`, `git` and `patch`.

```sh
sudo apt install dpkg-dev quilt git patch
pipx install .          # or: uv tool install .
th doctor
```

For development:

```sh
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest
```

## User config

`~/.config/transition-helper/config.yaml` (`th doctor --example-config` prints this):

```yaml
llm:
  model: anthropic/claude-opus-5-5   # any LiteLLM model string
  base_url: null                     # optional (proxy / self-hosted endpoint)
  api_key_env: ANTHROPIC_API_KEY     # name of the env var holding the key
budgets:
  max_iterations: 20                 # LLM turns per triage
  max_tokens: 400000                 # total tokens per triage
  max_tool_result_chars: 12000       # longer tool results are cut ("truncated; refine")
global_kb_path: ~/src/transition-kb  # team-shared signature repo (a git checkout)
debfullname: Your Name               # fallback: $DEBFULLNAME
debemail: you@ubuntu.com             # fallback: $DEBEMAIL
lp_auth: anonymous                   # or a path to a launchpadlib credentials file
allow_private_data: false            # allow data from a private PPA to reach the LLM
```

The LLM backend is provider-agnostic. LiteLLM sits behind a thin `LLMClient` interface.
Without a model configured, everything except the AI analysis still works
(`--no-llm` is implied).

The easiest way to create it is the wizard, which walks through every setting:

```sh
th config init
```

It asks whether to use an LLM, then for the model, the API key's env var (guessed from the
provider prefix, e.g. `openai/…` → `OPENAI_API_KEY`), an optional endpoint, budgets and
private-data handling. It can send a test request to check that the model works. Then it
asks for your maintainer identity (prefilled from `$DEBFULLNAME`/`$DEBEMAIL`), Launchpad
credentials and the global KB. It shows the resulting file and asks before writing.
Re-running it updates an existing config: each current value is the default (Enter keeps
it, `-` clears it), and the previous file is saved as `config.yaml.bak`. The file it writes
is commented with each key's meaning.

To change single keys use `th config` (keys are dotted, like `llm.model`; `th config keys`
lists them all with their types and defaults):

```sh
th config set llm.model anthropic/claude-opus-5-5
th config set llm.api_key_env ANTHROPIC_API_KEY
th config set budgets.max_iterations 30
th config get llm.model        # effective value; exit 1 if unset
th config get llm              # a whole section
th config unset llm.base_url   # back to the default
th config show                 # effective config: file + defaults (+ -c overrides)
th config edit                 # $VISUAL/$EDITOR, validated before saving; keeps comments
th config path
```

Values are type-checked and unknown keys are rejected (with a "did you mean"), so nothing
invalid is ever written. `set`/`unset` rewrite the file with PyYAML, which drops comments;
when the file had any, the previous version is saved as `config.yaml.bak` first. Use
`th config edit` to keep them.

To override a key for a single run without touching the file, use the global `-c` option
(repeatable; `KEY=` resets a key to its default):

```sh
th -c llm.model=openai/gpt-5 -c budgets.max_tokens=200000 triage foo
th -c llm.model= triage foo    # as if no model were configured
```

## Workspace

Run `th init` in an empty directory, one per transition. The wizard asks for every field
of `transition.yaml` and validates the series, the PPA and the toolchain source against
Launchpad. It saves the new toolchain's `debian/changelog` to `docs/` and can fetch extra
porting notes (URLs or files) as text. If the PPA is private, it warns that logs would go
to the LLM provider: `th` refuses to send them unless `allow_private_data` is set.

```
~/transitions/python3.15/
  transition.yaml
  docs/                       # toolchain changelog, porting notes (text; search_docs)
  kb/                         # transition-scoped KB signatures
  reports/<pkg>/<UTC-timestamp>/
  .cache/                     # logs (gzipped), sources, excuses, LP/Debian responses
```

```yaml
name: python3.15
series: tenacious
toolchain_source: python3-defaults
old_version: "3.14"
new_version: "3.15"
build_location: ppa               # ppa | archive
ppa: ~owner/ubuntu/name           # only if build_location == ppa
relevant_binaries: [python3-all-dev, python3-dev, dh-python]
ignored_arches: [riscv64]         # skipped unless --arch is given
lp_bug_tags: [python3.15]
docs: [toolchain_changelog.txt, porting_notes.md]
```

Every other command finds the workspace by walking up from the current directory to the
nearest `transition.yaml`, like git does.

## Commands

```
th [-c KEY=VALUE ...] <command>             # -c: per-run user config override
th init
th doctor [--no-ping]                       # tools, LP access, LLM ping, workspace, KB
th config init                              # wizard for the whole user config
th config path|show|keys|edit
th config get|unset <key>
th config set <key> <value>
th triage <srcpkg> [--arch A] [--interactive] [--follow-blockers]
                   [--no-llm] [--llm] [--refresh] [--quiet]
th followup <report-dir>
th file-bug <report-dir>
th learn <report-dir> [--global]
```

- `--arch A` focuses on one architecture, including one listed in `ignored_arches`.
- `--follow-blockers` triages the root of a blocker chain instead of the package itself.
- `--llm` runs the AI analysis even when the rules would skip it. `--no-llm` never runs it.
- `--refresh` bypasses the caches (excuses, logs, Debian data).
- `--quiet` suppresses the progress lines on stderr.

### What triage does

1. **Locate.** Reads versions and builds in the PPA, `<series>-proposed` and `<series>`.
   The newest version is the target; `~ppaN` is ignored when comparing. The result is one
   of these states: `BUILD_FAILED`, `BUILD_PENDING`, `PARTIAL_BUILD`,
   `BUILT_IN_PPA_NOT_IN_PROPOSED`, `IN_PROPOSED_NO_EXCUSE_YET`, `IN_PROPOSED_WITH_EXCUSE`,
   `MIGRATED` or `NOT_REBUILT`. Copying from the PPA to the archive is a human review
   step, so `th` only reports it as a state.
2. **Classify.** Rules turn Launchpad build states and the britney excuse into a category:
   - build: `FTBFS`, `DEPWAIT`, `BUILD_INFRA`, `BUILD_PENDING`, `SUPERSEDED`
   - migration: `AUTOPKGTEST_REGRESSION`, `AUTOPKGTEST_PENDING`, `DEPENDS_ON_UNMIGRATED`,
     `UNINSTALLABLE`, `MISSING_BUILD`, `BLOCKED`, `OTHER`

   Each failing test is recorded with its arch, triggers and log, and as either the
   package's own test or a reverse dependency's.
3. **Blocker chain.** Walks blocked-by, migrate-after and uninstallable binaries through
   the excuses, with a depth limit and cycle detection. It then suggests
   `th triage <root>`.
4. **Infra/flaky heuristics.** Flags a failing test as likely infra or flaky when the
   result is tmpfail, the log has infra markers (timeout, network, OOM, testbed), the
   release-pocket baseline also fails, the pass/fail history is intermittent, or the
   failure is a single-arch one that looks like infra. Retry URLs (`request.cgi`, with
   `all-proposed=1` when the trigger set looks incomplete) go into `commands.sh`. Nothing
   is ever retried automatically.
5. **Evidence.** Stores logs gzipped under stable ids (`build:<pkg>:<arch>`,
   `test:<pkg>:<arch>:<run>`). It then:
   - extracts failure windows;
   - matches the KB;
   - dedupes identical failures across arches, analysing one representative;
   - lists open LP bugs, highlighting those with transition tags;
   - compares against Debian unstable/experimental ("a sync or merge may fix this").
6. **LLM.** Runs for `FTBFS`, for autopkgtest regressions that don't look like infra, for
   unclear `UNINSTALLABLE`, or on request. It starts from the evidence bundle and works in
   a read-only tool loop:
   - logs: grep, line ranges and tails only, never whole logs;
   - docs and KB lookup;
   - sources: `get_source` of *any* package plus file listing, reading and grep;
   - package state: LP versions, excuses, Debian info;
   - `propose_patch`.

   It ends with a schema-validated `final_report`.
7. **Render** `reports/<pkg>/<ts>/`:
   - `report.md`, starting with a summary block
   - `report.json`
   - `patches/<target>_<version>.patch`
   - `commands.sh` (suggestions only; everything in it is commented out)
   - `evidence/` (bundle, excerpts, `transcript.jsonl`)

Progress lines like `locate: …`, `kb: matched …` and `llm[3/20]: grep_log build:amd64 /…/`
stream to stderr, and the terminal gets the summary block at the end.

### Fix targets and patches

The fix can belong to a different package than the one triaged: the owner of the failing
test, or a library. The model must name `target_pkg` for each fix and give its reasoning.
The terminal summary and the report call out every fix target. One that is not the
triaged package is flagged `⚠ different package than the one triaged`.

`propose_patch` validates the diff with `git apply --check` against an unpacked
(`dpkg-source -x`), git-committed tree of the exact version. Model-written hunks with
uneven context are repaired exactly, without fuzz. Errors go back to the model. An
accepted change becomes:

- a DEP-3 quilt patch plus a `debian/patches/series` entry, for `3.0 (quilt)` upstream
  changes. The whole series is checked with `quilt pop -a && quilt push -a --fuzz=0`.
- direct `debian/` edits, for packaging-only fixes and native/1.0 packages.
- a `debian/changelog` stanza: `X-Y`/`X-YbuildN` → `X-Yubuntu1`,
  `X-YubuntuN` → `X-Yubuntu(N+1)`, native `X` → `Xubuntu1`. Its entry reads
  `Fix FTBFS/autopkgtest with <toolchain> <new_version>`, with `(LP: #N)` once a bug
  exists.

There is one combined diff per target. Apply it with `patch -p1` inside the unpacked
source. No package is built.

## Follow-up, bugs and the knowledge base

- **`th followup <report-dir>`** reloads the report and a transcript summary, then starts
  a REPL with the same tools.
  - Ask questions in plain text.
  - `/report` writes a new revision: `report.v2.md`, `report.v2.json`,
    `patches/…v2.patch`.
  - `/patches`, `/summary` and `/quit` are also available.

  `th triage --interactive` goes straight into follow-up.
- **`th file-bug <report-dir>`** files the bug:
  1. Checks the package's open bugs for likely duplicates.
  2. Drafts title, description (root cause, evidence, log links) and tags (transition
     tags plus `ftbfs` or `update-excuse`), and shows the whole draft.
  3. Asks y/N. Only on yes does it log in to Launchpad with authenticated launchpadlib and
     file the bug against the source package.
  4. Regenerates the patches and changelog stanzas with the bug number, attaches them as
     patches, and updates the report.
- **KB signatures** are YAML files:

  ```yaml
  id: py315-removed-asyncore
  scope: python3.15          # or global
  applies_to: [build, autopkgtest]
  patterns: ["ModuleNotFoundError: No module named 'asyncore'"]
  explanation: ...
  fix_recipe: ...
  examples: [{package: foo, version: 1.2-3ubuntu1, link: https://...}]
  ```

  They load in this order, later entries overriding earlier ones by id:
  1. the seed signatures shipped with `th`;
  2. `global_kb_path`;
  3. `<workspace>/kb/`.

  Matches are hints for both the report and the LLM.
- **`th learn <report-dir>`** creates a new signature:
  1. Pre-fills it from the report; the escaped failure lines become pattern candidates.
  2. Opens `$EDITOR`.
  3. Checks that the result matches the report's logs.
  4. Saves to `<workspace>/kb/`, or with `--global` to the shared repo (commit it
     yourself).

## Data sources

| What | Where |
|---|---|
| Versions, builds, build logs, source files, bugs | Launchpad API (launchpadlib, `devel`) |
| Excuses | `https://ubuntu-archive-team.ubuntu.com/proposed-migration/<series>/update_excuses.yaml.xz`; re-downloaded only when its `Last-Modified` is newer than the cached copy's |
| Test history | swift listing `autopkgtest.ubuntu.com/results/autopkgtest-<series>/?format=json&prefix=…` + `result.tar` (exitcode, version, triggers) |
| Queues | `autopkgtest.ubuntu.com/queues.json`, `/static/running.json` |
| Debian versions | `api.ftp-master.debian.org/madison?f=json` |
| Debian changelog | `metadata.ftp-master.debian.org/changelogs/<comp>/<p>/<pkg>/<suite>_changelog` |

Excuses are regenerated on a schedule whose exact time varies. Each run sends one HEAD
request (the server ignores conditional GETs) and downloads only when a newer file was
published. If the server can't be reached, the cached copy is used with a warning. The
excuses' generation time is printed and recorded in the report, with a warning when it
is more than 30 hours old.

The parsers are tolerant: they skip unknown fields and malformed entries with a warning.
autopkgtest.ubuntu.com answers clients other than curl with a cookie challenge (HTTP
429), which `th` answers. Transient 5xx responses from Launchpad are retried.

## Safety notes

- File tools only read inside `.cache/sources/<pkg>_<ver>/` and reject path traversal,
  symlink escapes, `.git` and `.pc`.
- The model has no shell. All log and source content reaches it wrapped in
  `<untrusted-data>` blocks.
- Iteration and token budgets are enforced. When they run out, `th` forces a final
  report.
- Data from a private PPA is never sent to the LLM unless `allow_private_data: true` is
  set. If the PPA's privacy cannot be determined, `th` treats it as private.

## Tests

`pytest` runs offline. The tests use recorded fixtures (`tests/fixtures/`: real excuses,
a real autopkgtest log and madison output) and a scripted fake `LLMClient`. Tools that
need `dpkg-source`, `quilt` and `git` are exercised against tiny source packages built
during the tests.
