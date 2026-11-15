"""Several Hermes profiles in one process: each prunes from its own file, and `te` commands that
run Hermes sessions check their options before spending anything."""
import importlib.util
import json
import os
import sys

import pytest

from test_adapter import (CATALOG, PLUGIN, FakeLlm, Log, controls, env, make_rt,  # noqa: F401
                          runtime, seams, session, te)
from te_hermes_plugin import drivers


def _second_copy():
    """The plugin loaded again under another package name, as Hermes loads one copy per
    profile; both share the one vendored `tokeneconomizer` module."""
    name = "te_hermes_plugin_b"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(PLUGIN, "__init__.py"), submodule_search_locations=[PLUGIN])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    from te_hermes_plugin_b import runtime as rt_b, seams as seams_b
    seams_b.install()
    return rt_b, seams_b


def _profiles(env, tmp_path):
    rt_b_mod, seams_b = _second_copy()
    homes = {}
    for p in ("a", "b"):
        # Under a `.hermes` boundary, as real homes are, so the scratch dir's own ancestry
        # (it may sit inside a real profile) never names the profile.
        homes[p] = tmp_path / ".hermes" / "profiles" / p
        homes[p].mkdir(parents=True)
    a = runtime.Runtime(str(homes["a"]), FakeLlm(), Log(), state_root=str(tmp_path / "st-a"))
    b = rt_b_mod.Runtime(str(homes["b"]), FakeLlm(), Log(), state_root=str(tmp_path / "st-b"))
    return a, b, seams_b


def _first_request(rt, sid):
    rt.pre_llm_call(session_id=sid, turn_id=f"{sid}:t:0", user_message="x",
                    conversation_history=[], is_first_turn=True, platform="cli")
    res = rt.llm_request(request={"tools": [{"type": "function", "function": {"name": n}}
                                            for n in CATALOG]},
                         session_id=sid, turn_id=f"{sid}:t:0", api_mode="chat_completions",
                         platform="cli")
    tools = (res or {}).get("request", {}).get("tools")
    return [t["function"]["name"] for t in tools] if tools else list(CATALOG)


def test_each_profile_prunes_from_its_own_file(env, tmp_path):
    a, b, _ = _profiles(env, tmp_path)
    assert a.deny_rel != b.deny_rel != seams.DENY_FILE
    code, out = te(a, "toolprune", "apply", "image_generate")
    assert code == 0, out
    assert (env.repo / a.deny_rel).exists()
    assert not (env.repo / b.deny_rel).exists() and not (env.repo / seams.DENY_FILE).exists()
    assert "image_generate" not in _first_request(a, "sa")
    assert "image_generate" in _first_request(b, "sb")


def test_both_copies_share_one_context_variable_and_scope_upstream_calls(env, tmp_path,
                                                                        monkeypatch):
    a, b, seams_b = _profiles(env, tmp_path)
    assert seams.deny_var() is seams_b.deny_var()
    from tokeneconomizer.plugin import denylist, hook
    seen = []
    real = hook.run
    monkeypatch.setattr(hook, "run", lambda *x, **k: (
        seen.append(os.path.basename(denylist.settings_path("/r", "hermes"))), real(*x, **k))[1])
    _first_request(b, "sb")
    _first_request(a, "sa")
    assert {os.path.basename(a.deny_rel), os.path.basename(b.deny_rel)} <= set(seen)
    assert seen.index(os.path.basename(b.deny_rel)) < seen.index(os.path.basename(a.deny_rel))
    assert denylist.settings_path("/r", "hermes") == "/r/" + seams.DENY_FILE   # reset after


def test_folds_queued_at_session_end_run_in_their_profile(env, tmp_path, monkeypatch):
    a, b, _ = _profiles(env, tmp_path)
    from tokeneconomizer.plugin import denylist
    seen = []
    for rt in (a, b):
        real = rt._run_job_scoped
        monkeypatch.setattr(rt, "_run_job_scoped", lambda *x, _r=real, **k: (
            seen.append(denylist.settings_path("", "hermes")), _r(*x, **k))[1])
    session(a, "fa", [("x", [])])
    session(b, "fb", [("x", [])])
    assert a.drain(30) and b.drain(30)
    assert a.deny_rel in seen and b.deny_rel in seen


def test_root_profile_keeps_the_original_file_name():
    from hermes_constants import get_default_hermes_root
    assert seams.deny_file(str(get_default_hermes_root())) == seams.DENY_FILE


def test_subprocesses_and_launcher_carry_the_profile_file(env, tmp_path):
    a, _, _ = _profiles(env, tmp_path)
    assert a.env()[seams.DENY_ENV] == a.deny_rel
    base = a.launcher_root()
    body = open(os.path.join(base, "scripts", "te")).read()
    assert f"{seams.DENY_ENV}={a.deny_rel}" in body
    assert seams.valid_deny_file(a.deny_rel) == a.deny_rel
    for bad in ("../x-tools.local.json", "/etc/hermes-tools.local.json", "a/b/c",
                "./.tokeneconomizer/hermes-tools.local.json", ".tokeneconomizer/hermesjunk-tools.local.json",
                ".tokeneconomizer/hermes-..-tools.local.json"):
        assert seams.valid_deny_file(bad) is None


# ---- options of the commands that run Hermes sessions ----------------------------------------
@pytest.fixture
def probe_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(drivers, "probe", lambda runtime_, sessions=6, keep=False, out=None,
                        model=None: (calls.append((sessions, keep, model)), 0)[1])
    return calls


def test_help_prints_usage_and_runs_nothing(env, probe_calls):
    rt = make_rt(env)
    for argv in (["probe", "--help"], ["--host", "hermes", "probe", "-h"],
                 ["toolprune", "measure", "--help"], ["probe", "--cwd", "--help"]):
        code, out = te(rt, *argv)
        assert code in (0, 2) and "usage" in out.lower(), (argv, out)   # upstream's parser
    assert probe_calls == []


@pytest.mark.parametrize("argv,want", [
    (["probe", "--sessions=1"], (1, False, None)),
    (["probe", "--sessions", "2", "--keep"], (2, True, None)),
    (["probe", "--model=m1"], (6, False, "m1")),
    (["--host", "hermes", "probe", "--model", "m2"], (6, False, "m2")),
])
def test_probe_option_forms(env, probe_calls, argv, want):
    assert te(make_rt(env), *argv)[0] == 0
    assert probe_calls == [want]


@pytest.mark.parametrize("argv", [["probe", "--bogus"], ["probe", "--sessions"],
                                  ["probe", "--sessions=x"], ["probe", "--model"],
                                  ["toolprune", "measure", "--json"],
                                  ["probe", "--cwd"], ["probe", "--cwd="], ["--host", "probe"],
                                  ["probe", "--host", "claude"], ["probe", "extra"],
                                  ["toolprune", "refresh-catalog", "x"]])
def test_bad_options_are_refused_before_any_session(env, probe_calls, monkeypatch, argv):
    monkeypatch.setattr(drivers, "_prompt_tokens", lambda *a, **k: pytest.fail("ran a session"))
    code, out = te(make_rt(env), *argv)
    assert code == 2 and probe_calls == [], out
