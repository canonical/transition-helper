import pytest
import yaml

from transition_helper.cli import main
from transition_helper.config import (EXAMPLE_CONFIG, KEYS, ConfigError, UserConfig, apply_overrides,
                                      coerce, config_path, edit_config, edit_template,
                                      effective_dict, load_config, parse_config, set_raw,
                                      unset_raw, update_config, validate_raw)


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return config_path()


def test_coerce():
    assert coerce("budgets.max_tokens", "400_000") == 400_000
    assert coerce("allow_private_data", "Yes") is True
    assert coerce("allow_private_data", "off") is False
    assert coerce("llm.model", "openai/gpt-5") == "openai/gpt-5"
    for key, bad in (("budgets.max_tokens", "lots"), ("budgets.max_tokens", "0"),
                     ("allow_private_data", "maybe"), ("llm.model", "  ")):
        with pytest.raises(ConfigError):
            coerce(key, bad)
    with pytest.raises(ConfigError, match="did you mean 'llm.model'"):
        coerce("llm.modle", "x")


def test_set_unset_raw():
    data: dict = {"llm": None}
    set_raw(data, "llm.model", "m")
    set_raw(data, "debemail", "a@b")
    assert data == {"llm": {"model": "m"}, "debemail": "a@b"}
    assert unset_raw(data, "llm.model")
    assert data == {"debemail": "a@b"}  # empty section dropped
    assert not unset_raw(data, "llm.model")
    assert not unset_raw(data, "budgets.max_tokens")


def test_validate_raw():
    validate_raw({"llm": {"model": "m", "base_url": None}, "budgets": {"max_tokens": 5},
                  "allow_private_data": False})
    validate_raw(None)
    for bad in ({"llm": {"model": "m", "temperature": 1}}, {"llm": "m"},
                {"allow_private_data": "false"}, {"budgets": {"max_tokens": True}},
                {"budgets": {"max_tokens": None}}, {"debfullname": 3}, ["x"]):
        with pytest.raises(ConfigError):
            validate_raw(bad)
    with pytest.raises(ConfigError, match="positive"):
        parse_config({"budgets": {"max_iterations": 0}})


def test_keys_match_userconfig_and_example():
    eff = effective_dict(UserConfig())  # raises if a key has no UserConfig attribute
    assert set(eff) == {k.split(".")[0] for k in KEYS}
    example = yaml.safe_load(EXAMPLE_CONFIG)
    validate_raw(example)  # the example passes the strict checks...
    assert effective_dict(parse_config(example)).keys() == eff.keys()
    assert {f"{s}.{k}" for s in ("llm", "budgets") for k in example[s]} | \
        {k for k in example if k not in ("llm", "budgets")} == set(KEYS)  # ...and covers every key


def test_overrides(cfg_file):
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text("llm:\n  model: m\n  api_key_env: K\n")
    cfg = load_config(overrides=["llm.model=other", "budgets.max_iterations=3"])
    assert cfg.llm.model == "other" and cfg.llm.api_key_env == "K"
    assert cfg.budgets.max_iterations == 3 and cfg.path == cfg_file
    assert load_config(overrides=["llm.model="]).llm.model == ""
    data = {"llm": {"model": "m"}}
    apply_overrides(data, ["llm.model=x"])
    assert data == {"llm": {"model": "m"}}  # input not mutated
    for bad in ("llm.model", "nope=1", "budgets.max_tokens=x", "nope="):
        with pytest.raises(ConfigError):
            load_config(overrides=[bad])


def test_update_config(cfg_file):
    _, backup = update_config(cfg_file, lambda d: set_raw(d, "llm.model", "m"))
    assert backup is None
    assert yaml.safe_load(cfg_file.read_text()) == {"llm": {"model": "m"}}
    with pytest.raises(ConfigError):
        update_config(cfg_file, lambda d: set_raw(d, "budgets.max_tokens", "x"))
    assert yaml.safe_load(cfg_file.read_text()) == {"llm": {"model": "m"}}  # untouched

    cfg_file.write_text("# mine\nllm:\n  model: m  # the model\n")
    _, backup = update_config(cfg_file, lambda d: set_raw(d, "debemail", "a@b"))
    assert backup.read_text() == "# mine\nllm:\n  model: m  # the model\n"
    assert yaml.safe_load(cfg_file.read_text()) == {"llm": {"model": "m"}, "debemail": "a@b"}
    removed, _ = update_config(cfg_file, lambda d: unset_raw(d, "debemail"))
    assert removed
    # an unknown key left in the file can still be removed
    cfg_file.write_text("old_key: 1\n")
    removed, _ = update_config(cfg_file, lambda d: unset_raw(d, "old_key"))
    assert removed and cfg_file.read_text() == ""


def test_edit_config(cfg_file):
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text("# keep me\nllm:\n  model: m\n")
    edits = iter([
        lambda t: t.replace("model: m", "model: m\n  temprature: 1"),
        lambda t: t.replace("  temprature: 1\n", "") + "budgets:\n  max_iterations: 9\n",
    ])
    seen, out = [], []

    def editor(text):
        seen.append(text)
        return next(edits)(text)

    assert edit_config(cfg_file, editor, input_fn=lambda _: "", out=out.append)
    assert seen[1].startswith("# ERROR: unknown config key 'llm.temprature'")
    text = cfg_file.read_text()
    assert text.startswith("# keep me\n") and "ERROR" not in text  # comments kept
    assert load_config().budgets.max_iterations == 9

    # reverting to the original after an error is "no changes", not a write
    edits = iter([lambda t: t + "nope: 1\n", lambda t: t.replace("nope: 1\n", "")])
    out.clear()
    assert not edit_config(cfg_file, lambda t: next(edits)(t), input_fn=lambda _: "",
                           out=out.append)
    assert out[-1] == "no changes" and cfg_file.read_text() == text


def test_edit_config_abort_and_noop(cfg_file):
    out = []
    assert not edit_config(cfg_file, lambda t: t, out=out.append)
    assert out == ["no changes"] and not cfg_file.exists()
    out.clear()
    assert not edit_config(cfg_file, lambda t: "budgets: [1]\n", input_fn=lambda _: "n",
                           out=out.append)
    assert out[-1] == "aborted; not saved" and not cfg_file.exists()
    # the template for a new file is entirely commented out, i.e. all defaults
    assert yaml.safe_load(edit_template()) is None


def test_cli_config(cfg_file, capsys, monkeypatch):
    assert main(["config", "get", "llm.model"]) == 1
    assert main(["config", "set", "llm.model", "anthropic/claude-opus-5-5"]) == 0
    assert main(["config", "set", "budgets.max_iterations", "lots"]) == 1
    assert main(["config", "set", "allow_private_data", "yes"]) == 0
    capsys.readouterr()

    assert main(["config", "get", "llm.model"]) == 0
    assert capsys.readouterr().out == "anthropic/claude-opus-5-5\n"
    assert main(["config", "get", "allow_private_data"]) == 0
    assert capsys.readouterr().out == "true\n"
    assert main(["config", "get", "budgets"]) == 0
    assert "max_iterations: 20" in capsys.readouterr().out
    assert main(["config", "get", "nope"]) == 1

    assert main(["-c", "llm.model=openai/gpt-5", "config", "show"]) == 0
    shown = capsys.readouterr().out
    assert "(with -c overrides)" in shown
    assert yaml.safe_load(shown)["llm"]["model"] == "openai/gpt-5"
    assert yaml.safe_load(cfg_file.read_text())["llm"]["model"] == "anthropic/claude-opus-5-5"

    assert main(["config", "unset", "allow_private_data"]) == 0
    assert main(["config", "unset", "allow_private_data"]) == 1
    assert main(["config", "path"]) == 0
    assert main(["config", "keys"]) == 0
    out = capsys.readouterr().out
    assert str(cfg_file) in out and "budgets.max_tokens" in out

    monkeypatch.setenv("EDITOR", "false")
    assert main(["config", "edit"]) == 1
    assert "editor 'false' failed" in capsys.readouterr().err


def _wizard(path, answers, ping=None, env=None):
    from transition_helper.config_wizard import run_config_wizard

    answers = iter(answers)
    prompts, out = [], []

    def ask(prompt):
        prompts.append(prompt)
        return next(answers)

    written = run_config_wizard(path, ask=ask, out=out.append, ping=ping, env=env or {})
    assert next(answers, None) is None, "unused answers"
    return written, prompts, out


def test_config_wizard_fresh(cfg_file, tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    pinged = []
    written, prompts, out = _wizard(cfg_file, [
        "",                         # use an LLM? (default yes)
        "",                         # model -> default
        "",                         # key env -> guessed ANTHROPIC_API_KEY
        "ftp://x", "",              # base url: invalid, then none
        "",                         # test request? (default yes)
        "y", "0", "30", "", "",     # budgets: change; turns invalid then 30; rest default
        "",                         # private data -> no
        "A B", "nope", "a@b.c",     # maintainer (bad email retried)
        "/missing/creds", "n", "",  # LP creds: missing file, don't use it, then anonymous
        str(kb),                    # global KB
        "",                         # write? (default yes)
    ], ping=lambda llm: pinged.append(llm.model) or "pong", env={"ANTHROPIC_API_KEY": "k"})
    assert written and pinged == ["anthropic/claude-opus-5-5"]
    assert "[ANTHROPIC_API_KEY]" in prompts[2]
    assert "  must start with http:// or https://" in out and "  not an email address" in out
    assert "  ok: anthropic/claude-opus-5-5 replied 'pong'" in out
    text = cfg_file.read_text()
    assert "# LLM turns per triage" in text  # rendered with help comments
    assert yaml.safe_load(text) == {
        "llm": {"model": "anthropic/claude-opus-5-5", "api_key_env": "ANTHROPIC_API_KEY"},
        "budgets": {"max_iterations": 30},  # only the non-default budget is stored
        "global_kb_path": str(kb), "debfullname": "A B", "debemail": "a@b.c",
    }
    assert load_config().budgets.max_iterations == 30


def test_config_wizard_update(cfg_file):
    cfg_file.parent.mkdir(parents=True)
    original = ("# my comment\nllm:\n  model: anthropic/claude-opus-5-5\n"
                "  api_key_env: ANTHROPIC_API_KEY\ndebemail: a@b.c\nallow_private_data: true\n")
    cfg_file.write_text(original)
    # Enter everywhere keeps every current value...
    written, prompts, out = _wizard(cfg_file, [""] * 10 + ["n"])
    assert not written and "not saved" in out
    assert any("[a@b.c]" in p for p in prompts)
    assert any("private PPAs" in p and "[Y/n]" in p for p in prompts)  # current value is default
    assert cfg_file.read_text() == original

    # ...a new model re-guesses the key env; '-' clears a value
    written, _, out = _wizard(cfg_file, [
        "", "openai/gpt-5", "", "",  # LLM: keep using one, new model, guessed env, no base url
        "", "",                      # budgets unchanged, private data stays allowed
        "", "-",                     # no name; clear the email
        "", "", "",                  # anonymous LP, no KB, write
    ], env={"OPENAI_API_KEY": "k"})
    assert written
    assert yaml.safe_load(cfg_file.read_text()) == {
        "llm": {"model": "openai/gpt-5", "api_key_env": "OPENAI_API_KEY"},
        "allow_private_data": True,
    }
    assert cfg_file.with_name("config.yaml.bak").read_text() == original

    # rendering is stable: a second pass with no changes doesn't rewrite
    written, _, out = _wizard(cfg_file, [""] * 10, env={"OPENAI_API_KEY": "k"})
    assert not written and "no changes" in out


def test_config_wizard_no_llm_and_failed_ping(cfg_file):
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text("llm:\n  model: m\n")
    written, _, _ = _wizard(cfg_file, ["n", "", "", "", "", ""])
    assert written and "llm" not in (yaml.safe_load(cfg_file.read_text()) or {})

    def ping(llm):
        if llm.model == "bad":
            raise RuntimeError("model not found\nlong traceback")
        return "pong"

    written, _, out = _wizard(cfg_file, [
        "", "bad", "", "", "",      # model 'bad' (no key env guess), ping fails
        "", "good", "", "", "",     # change settings: 'good', ping ok
        "", "", "", "", "", "", "",
    ], ping=ping)
    assert "  failed: model not found" in out and "  ok: good replied 'pong'" in out
    assert yaml.safe_load(cfg_file.read_text()) == {"llm": {"model": "good"}}


def test_config_wizard_invalid_existing(cfg_file):
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text("budgets: [1]\n")
    written, _, out = _wizard(cfg_file, ["n", "", "", "", "", ""])
    assert written and out[0].startswith("warning: ignoring the current config")
    assert cfg_file.with_name("config.yaml.bak").read_text() == "budgets: [1]\n"
    assert cfg_file.read_text() == ""


def test_render_config_roundtrip():
    from transition_helper.config import render_config

    data = {"debfullname": "A: B #1", "llm": {"base_url": None, "model": "m"},
            "allow_private_data": True, "budgets": {"max_tokens": 5}}
    text = render_config(data)
    assert yaml.safe_load(text) == data
    assert text.index("llm:") < text.index("budgets:") < text.index("debfullname")
    assert render_config({}) == ""
