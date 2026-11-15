"""Exact-parity behaviours: shared state root, launcher, tool-search filtering, subagent
accounting, signed-out pause, multimodal advice, measure, fold --verbose, streak view."""
import json
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from test_adapter import (CATALOG, FakeLlm, Log, controls, env, make_rt, runtime, seams,  # noqa: F401
                          session, te, terminal_result)

from te_hermes_plugin import drivers, runner
from te_hermes_plugin.host import HermesHost

DEFERRED_DESC = ("Search 3 additional tools.\n\nDeferred tool catalog:\nweb tools (3):\n"
                 "- image_generate: make images\n- browser_navigate: open a page\n"
                 "- text_to_speech: speak")


def _deny(env, *names):
    os.makedirs(env.repo / ".tokeneconomizer", exist_ok=True)
    with open(env.repo / seams.DENY_FILE, "w") as fh:
        json.dump({seams.DENY_KEY: list(names)}, fh)


def _start(rt, sid, tools=None):
    rt.pre_llm_call(session_id=sid, turn_id=f"{sid}:t:0", user_message="x",
                    conversation_history=[], is_first_turn=True, platform="cli")
    req = {"tools": tools or [{"type": "function", "function": {"name": n}} for n in CATALOG]}
    return rt.llm_request(request=req, session_id=sid, turn_id=f"{sid}:t:0",
                          api_mode="chat_completions", platform="cli")


def test_default_state_root_is_upstreams_shared_home(env, monkeypatch, tmp_path):
    home = tmp_path / "person"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TOKENECONOMIZER_HOME")
    rt = make_rt(env)
    assert rt.root == str(home / ".tokeneconomizer")
    assert not (home / ".tokeneconomizer" / "config.json").exists()   # nothing seeded


def test_session_start_writes_upstream_launcher_to_this_plugin(env):
    rt = make_rt(env)
    session(rt, "l1", [("x", [])])
    launcher = env.root / "bin" / "te"
    body = launcher.read_text()
    script = env.root / "hosts" / "hermes" / "plugin" / "scripts" / "te"
    assert str(script) in body
    assert seams.ENTRY in script.read_text()
    p = subprocess.run([str(launcher), "status", "--host", "hermes", "--cwd", str(env.repo)],
                       capture_output=True, text=True,
                       env=dict(os.environ, TOKENECONOMIZER_HOME=str(env.root)))
    assert p.returncode == 0 and "TokenEconomizer" in p.stdout, p.stderr


@pytest.mark.parametrize("argv", [["toolprune", "measure"], ["untool", "measure"],
                                  ["toolprune", "refresh-catalog"], ["probe"],
                                  ["--host", "hermes", "toolprune", "measure"],
                                  ["--cwd=.", "probe"],
                                  ["toolprune", "--host", "hermes", "measure", "x"],
                                  ["untool", "--cwd", ".", "refresh-catalog"]])
def test_launcher_refuses_commands_that_must_run_a_hermes_session(env, argv):
    # Outside Hermes, upstream runs these on another agent's CLI (or prints nothing), so the
    # launcher would measure the wrong host; `hermes te` routes them to drivers.py.
    p = subprocess.run([sys.executable, seams.ENTRY, *argv, "--host", "hermes",
                        "--cwd", str(env.repo)], capture_output=True, text=True,
                       env=dict(os.environ, TOKENECONOMIZER_HOME=str(env.root),
                                PATH="/usr/bin:/bin"), timeout=60)   # no agent CLI to reach
    assert p.returncode == 2 and "hermes te" in p.stderr, (p.stdout, p.stderr)


def test_tool_search_results_leave_denied_tools_out(env):
    _deny(env, "image_generate")
    rt = make_rt(env)
    tools = [{"type": "function", "function": {"name": "terminal"}},
             {"type": "function", "function": {"name": "tool_search",
                                               "description": DEFERRED_DESC}}]
    _start(rt, "ts", tools)
    raw = json.dumps({"queries": ["image"], "total_available": 3,
                      "results": [{"query": "image",
                                   "matches": ["image_generate", "browser_navigate"]}],
                      "tools": {"image_generate": {"d": 1}, "browser_navigate": {"d": 2}}})
    # Hermes answers tool_search inline, without `transform_tool_result`: the filter runs on
    # the model-bound request, in every API shape's copy of the result.
    shapes = {
        "chat_completions": ("messages", [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "tool_search", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": raw}],
            lambda r: r["messages"][1]["content"]),
        "anthropic_messages": ("messages", [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "c1", "name": "tool_search", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": raw}]}],
            lambda r: r["messages"][1]["content"][0]["content"]),
        "codex_responses": ("input", [
            {"type": "function_call", "call_id": "c1", "name": "tool_search", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": raw}],
            lambda r: r["input"][1]["output"]),
    }
    for mode, (key, rows, get) in shapes.items():
        res = rt.llm_request(request={"tools": tools, key: rows}, session_id="ts",
                             turn_id="ts:t:0", api_mode=mode, platform="cli")
        data = json.loads(get(res["request"]))
        assert data["results"][0]["matches"] == ["browser_navigate"], mode
        assert set(data["tools"]) == {"browser_navigate"} and data["total_available"] == 2
    # Only the results of the request's own tool_search calls: a user message or another
    # tool's output that merely looks like one is left as it is.
    rows = [{"role": "user", "content": raw},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c2", "content": raw}]
    res = rt.llm_request(request={"tools": tools, "messages": rows}, session_id="ts",
                         turn_id="ts:t:0", api_mode="chat_completions", platform="cli")
    assert [m["content"] for m in res["request"]["messages"]] == [raw, "", raw]


def test_subagent_tool_use_and_cost_belong_to_the_parent(env):
    _deny(env, "image_generate")
    rt = make_rt(env)
    _start(rt, "par")
    rt.subagent_start(parent_session_id="par", child_session_id="kid")
    res = rt.llm_request(request={"tools": [{"type": "function", "function": {"name": n}}
                                            for n in CATALOG]},
                         session_id="kid", turn_id="kid:t:0", api_mode="chat_completions",
                         platform="subagent")
    assert "image_generate" not in [t["function"]["name"] for t in res["request"]["tools"]]
    assert rt.pre_tool_call(tool_name="image_generate", args={}, session_id="kid")
    rt.post_api_request(session_id="kid", turn_id="kid:t:0", api_request_id="kr1",
                        model="m", platform="subagent",
                        usage={"input_tokens": 7, "cache_read_tokens": 0,
                               "cache_write_tokens": 0, "output_tokens": 3})
    for hook in (rt.transform_tool_result, rt.post_tool_call):
        hook(tool_name="browser_navigate", args={"url": "x"}, result="ok",
             session_id="kid", tool_call_id="k1", turn_id="kid:t:0", status="ok")
    sub = env.root / "hermes-sessions" / "par" / "subagents" / "kid.jsonl"
    lines = [json.loads(l) for l in sub.read_text().splitlines()]
    assert sum(1 for l in lines for b in (l.get("message") or {}).get("content") or []
               if isinstance(b, dict) and b.get("type") == "tool_use") == 1   # once per call
    host = HermesHost()
    log = rt._log_path("par")
    assert host.session_usage(log)["output_tokens"] == 3
    trace = host.mechanism_trace(log, str(env.home))
    assert host.tool_usage(trace).get("browser_navigate") == 1
    assert not os.path.exists(rt._log_path("kid"))     # no session of its own


def test_signed_out_optimizer_pauses_side_calls(env):
    class Denied(Exception):
        status_code = 401

    class NoLogin:
        def complete(self, *a, **k):
            raise Denied("Error code: 401 - invalid x-api-key")
    from tokeneconomizer.plugin import sidecall
    r = runner.OptimizerRunner(runner.Binding(NoLogin(), str(env.root), lambda: True))
    with pytest.raises(Denied):
        r("prompt")
    assert sidecall.paused("hermes", str(env.root))
    assert "not signed in" in (env.root / "errors.log").read_text()
    assert not runner.signed_out(TimeoutError("read timed out"))


def test_signed_out_pause_is_told_once_at_session_start(env, monkeypatch):
    from tokeneconomizer.plugin import sidecall
    monkeypatch.setenv("TE_MICROSKILLS", "on")
    rt = make_rt(env)
    path = sidecall._pause_path("hermes", str(env.root))
    os.makedirs(os.path.dirname(path))
    json.dump({"at": 1.0, "reason": "401"}, open(path, "w"))
    first = "".join(session(rt, "pz1", [("x", [])])[0])
    second = "".join(session(rt, "pz2", [("x", [])])[0])
    assert "paused" in first and "paused" not in second


def test_multimodal_result_carries_its_advice(env, monkeypatch):
    rt = make_rt(env)
    _start(rt, "mm")
    monkeypatch.setattr(rt, "_observe", lambda *a, **k: "try another way")
    env_ = {"_multimodal": True, "content": [{"type": "image_url", "image_url": {"url": "x"}}],
            "text_summary": "an image"}
    out = rt.tool_execution(tool_name="vision", args={}, next_call=lambda a: env_,
                            session_id="mm", tool_call_id="v1", turn_id="mm:t:0")
    assert out["content"][-1]["type"] == "text" and "try another way" in out["content"][-1]["text"]
    assert "try another way" in out["text_summary"]
    assert rt.transform_tool_result(tool_name="vision", args={}, result=out, session_id="mm",
                                    tool_call_id="v1", turn_id="mm:t:0", status="ok") is None
    assert rt._existing("mm").undelivered == 0
    plain = rt.tool_execution(tool_name="terminal", args={}, next_call=lambda a: "text",
                              session_id="mm", tool_call_id="t1", turn_id="mm:t:0")
    assert plain == "text"


def test_measure_reports_provider_prompt_tokens_for_named_tools(env, monkeypatch):
    rt = make_rt(env)
    monkeypatch.setattr(drivers, "_middleware_route", lambda: True)
    seen = []

    def fake(runtime_, deny, timeout=300):
        seen.append(tuple(deny))
        return (5000 if not deny else 4200), None
    monkeypatch.setattr(drivers, "_prompt_tokens", fake)
    code, out = te(rt, "toolprune", "measure", "image_generate")
    data = json.loads(out)
    assert code == 0 and seen == [(), ("image_generate",)]
    assert data == {"tools": ["image_generate"], "prompt_tokens_full": 5000,
                    "prompt_tokens_lean": 4200, "saved_per_request": 800,
                    "probe_cost_usd": None}
    assert te(rt, "toolprune", "measure")[1] == "no proposal to measure"


@pytest.mark.parametrize("argv", [
    ["--host", "hermes", "toolprune", "measure", "image_generate"],
    ["--host=hermes", "untool", "measure", "image_generate"],
    ["toolprune", "--host", "hermes", "measure", "image_generate"],
    ["toolprune", "measure", "--host", "hermes", "image_generate"],
    ["toolprune", "measure", "image_generate", "--cwd", "."],
])
def test_measure_routes_to_hermes_driver_whatever_the_option_order(env, monkeypatch, argv):
    # Upstream accepts --host/--cwd before or after the subcommand; every form must reach the
    # Hermes driver (never upstream's, which measures another agent), and option values are
    # never taken as tool names.
    rt = make_rt(env)
    monkeypatch.setattr(drivers, "_middleware_route", lambda: True)
    seen = []
    monkeypatch.setattr(drivers, "_prompt_tokens",
                        lambda runtime_, deny, timeout=300: (seen.append(tuple(deny)), (10, None))[1])
    code, out = te(rt, *argv)
    assert code == 0 and seen == [(), ("image_generate",)], out


def test_measure_child_request_leaves_out_exactly_the_named_tools(env, monkeypatch):
    monkeypatch.setenv(runtime.MEASURE_ENV, "image_generate,text_to_speech")
    rt = make_rt(env)
    res = _start(rt, "ms")
    names = [t["function"]["name"] for t in res["request"]["tools"]]
    assert "image_generate" not in names and "text_to_speech" not in names
    assert "browser_navigate" in names


def test_fold_verbose_prints_records(env):
    rt = make_rt(env)
    session(rt, "fv", [("x", [])], finalize=False)
    rt.on_session_end(session_id="fv", turn_id="fv:t:0", completed=True, platform="cli")
    code, out = te(rt, "fold", "--session", "fv", "--verbose")
    lines = out.splitlines()
    assert code == 0 and lines[-1] == "folded 1 session(s)", out
    assert json.loads(lines[0])["kind"] == "session"


def test_toolprune_shows_unused_streaks_for_hermes(env):
    rt = make_rt(env)
    work = [("terminal", {"command": "ls"}, terminal_result("a", 0), "ok")]
    for n in range(3):
        session(rt, f"st{n}", [("work", work)])
    out = te(rt, "toolprune")[1]
    assert "how long each tool has gone unused" in out, out


def test_judge_never_waits_behind_a_fold(env, monkeypatch):
    """Upstream runs each job as its own detached process: a slow learning pass must not hold
    the loop guard's judge until the looping turn is over."""
    rt = make_rt(env)
    release, ran = threading.Event(), []

    def fake(gen, argv, log_name, reply=None):
        if argv[0] == "fold":
            release.wait(10)
        ran.append(argv[0])
        if reply:
            reply[0].set()
    monkeypatch.setattr(rt, "_run_job", fake)
    ctx = types.SimpleNamespace(config=types.SimpleNamespace(enabled=True),
                                repo=types.SimpleNamespace(root=str(env.repo)))
    assert rt.spawn(ctx, ["fold"], "fold.log")
    assert rt.spawn(ctx, ["judge", "--session", "s", "--job", "j"], "judge.log")
    deadline = time.time() + 5
    while "judge" not in ran and time.time() < deadline:
        time.sleep(0.05)
    release.set()
    assert ran and ran[0] == "judge", ran
    assert rt.drain(10) and ran == ["judge", "fold"]


def test_profile_setting_gives_hermes_its_own_state_root(env, tmp_path):
    """`plugins.entries.tokeneconomizer.settings.state_root` puts Hermes's state (and its
    settings file) in a root of its own, away from other hosts' shared `~/.tokeneconomizer`;
    it is profile config, so a running gateway reads it without a process restart."""
    own = tmp_path / "te-hermes"
    rt = runtime.Runtime(str(env.home), FakeLlm(), Log(), state_root=str(own))
    assert rt.root == str(own)
    session(rt, "own1", [("x", [])])
    assert (own / "bin" / "te").exists() and not (env.root / "bin" / "te").exists()


def test_advice_survives_another_plugins_winning_transform(env, monkeypatch):
    """Hermes keeps the first `transform_tool_result` string; Chisle loads first and compresses
    long outputs, so the redirect TokenEconomizer appended can be dropped. The model-bound
    request then gets it back on that call's own result, once, in every API shape."""
    rt = make_rt(env)
    _start(rt, "cz")
    monkeypatch.setattr(rt, "_observe", lambda *a, **k: "try another way")
    raw = terminal_result("x" * 50, 3)
    ours = rt.transform_tool_result(tool_name="terminal", args={}, result=raw, session_id="cz",
                                    tool_call_id="k1", turn_id="cz:t:0", status="ok")
    assert "try another way" in ours
    won = terminal_result("[chisle: compressed]", 3)     # what Hermes stored instead
    tools = [{"type": "function", "function": {"name": n}} for n in CATALOG]
    shapes = {
        "chat_completions": ("messages", [{"role": "tool", "tool_call_id": "k1", "content": won}],
                             lambda r: r["messages"][0]["content"]),
        "anthropic_messages": ("messages", [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "k1", "content": won}]}],
            lambda r: r["messages"][0]["content"][0]["content"]),
        "codex_responses": ("input", [{"type": "function_call_output", "call_id": "k1",
                                       "output": won}], lambda r: r["input"][0]["output"]),
    }
    for mode, (key, rows, get) in shapes.items():
        res = rt.llm_request(request={"tools": tools, key: rows}, session_id="cz",
                             turn_id="cz:t:0", api_mode=mode, platform="cli")
        text = get(res["request"])
        assert text.startswith(won) and text.count("try another way") == 1, mode
    # Delivered by our own transform: left as it is.
    res = rt.llm_request(request={"tools": tools, "messages": [
        {"role": "tool", "tool_call_id": "k1", "content": ours}]}, session_id="cz",
        turn_id="cz:t:0", api_mode="chat_completions", platform="cli")
    assert (res or {}).get("request", {}).get("messages", [{}])[0].get("content", ours) == ours


def test_own_root_launcher_points_te_at_that_root(env, tmp_path):
    """`<state_root>/bin/te` run on its own (no TOKENECONOMIZER_HOME) reads that root."""
    own = tmp_path / "te-own"
    rt = runtime.Runtime(str(env.home), FakeLlm(), Log(), state_root=str(own))
    session(rt, "own2", [("x", [])])
    clean = {k: v for k, v in os.environ.items() if k != "TOKENECONOMIZER_HOME"}
    out = subprocess.run([str(own / "bin" / "te"), "status"], capture_output=True, text=True,
                         env=clean, cwd=str(env.repo)).stdout
    assert str(own) in out, out


def test_advice_restored_without_tools_and_bounded(env, monkeypatch):
    rt = make_rt(env)
    _start(rt, "cb")
    monkeypatch.setattr(rt, "_observe", lambda *a, **k: "try another way")
    raw = terminal_result("x", 3)
    rt.transform_tool_result(tool_name="terminal", args={}, result=raw, session_id="cb",
                             tool_call_id="k1", turn_id="cb:t:0", status="ok")
    res = rt.llm_request(request={"messages": [{"role": "tool", "tool_call_id": "k1",
                                                "content": raw}]},
                         session_id="cb", turn_id="cb:t:0", api_mode="chat_completions",
                         platform="cli")
    assert "try another way" in res["request"]["messages"][0]["content"]
    s = rt._existing("cb")
    for i in range(runtime.ATTACHED_MAX + 50):
        rt.transform_tool_result(tool_name="terminal", args={}, result=raw, session_id="cb",
                                 tool_call_id=f"n{i}", turn_id="cb:t:0", status="ok")
    assert len(s.attached) == runtime.ATTACHED_MAX and f"n{runtime.ATTACHED_MAX + 49}" in s.attached


def test_advice_keeps_a_failed_result_a_failure(env, monkeypatch):
    """Hermes reads a tool's failure from its JSON result (`exit_code`, `error`). Advice text
    appended after that JSON made it unparseable, so Hermes counted a failed command as a
    success (`agent.display._detect_tool_failure`). The advice now rides inside the object:
    every original byte first, one last key."""
    rt = make_rt(env)
    _start(rt, "fj")
    monkeypatch.setattr(rt, "_observe", lambda *a, **k: 'try "another" way')
    kw = dict(session_id="fj", turn_id="fj:t:0", status="ok")
    raw = terminal_result("FAILED tests/test_x.py::test_a", 1)
    out = rt.transform_tool_result(tool_name="terminal", args={}, result=raw,
                                   tool_call_id="j1", **kw)
    assert out.startswith(raw[:-1])
    data = json.loads(out)
    assert data["exit_code"] == 1 and 'try "another" way' in data[runtime.ADVICE_FIELD]
    assert json.loads(rt.transform_tool_result(tool_name="terminal", args={}, result="{}",
                                               tool_call_id="j2", **kw))[runtime.ADVICE_FIELD]
    # Not a JSON object: the block follows the text, as before.
    for i, plain in enumerate(("plain text", "[1, 2]")):
        out = rt.transform_tool_result(tool_name="terminal", args={}, result=plain,
                                       tool_call_id=f"p{i}", **kw)
        assert out == plain + "\n\n" + runtime._wrap('try "another" way', "tool")
    # A result with a key of that name keeps its own value.
    own = json.dumps({runtime.ADVICE_FIELD: {"original": True}, "exit_code": 2})
    data = json.loads(rt.transform_tool_result(tool_name="terminal", args={}, result=own,
                                               tool_call_id="j4", **kw))
    assert data[runtime.ADVICE_FIELD] == {"original": True} and data["exit_code"] == 2
    assert 'try "another" way' in data[runtime.ADVICE_FIELD + "_"]
    # Delivered inside the object: the request-level repair leaves it alone.
    out = rt.transform_tool_result(tool_name="terminal", args={}, result=raw,
                                   tool_call_id="j3", **kw)
    res = rt.llm_request(request={"messages": [{"role": "tool", "tool_call_id": "j3",
                                                "content": out}]},
                         session_id="fj", turn_id="fj:t:0", api_mode="chat_completions",
                         platform="cli")
    assert (res or {}).get("request", {}).get("messages", [{}])[0].get("content", out) == out


def test_advice_on_an_untrusted_result_waits_for_the_next_trusted_one(env, monkeypatch):
    """Hermes wraps web, browser and MCP results in a block the model is told not to obey
    (`agent.tool_dispatch_helpers._maybe_wrap_untrusted`). Advice decided on such a call waits
    for the turn's next result Hermes does not wrap, as upstream's Hermes plugin holds it. It
    is then part of a stored result: nothing depends on this process's memory, or on the call
    id a provider sees, to reach the model again."""
    from agent.tool_dispatch_helpers import _is_untrusted_tool
    for name in ("web_extract", "web_search", "browser_click", "mcp__probe__echo", "mcp_x",
                 "terminal", "read_file", "tool_call", "tool_search", "delegate_task"):
        assert runtime._untrusted(name) == _is_untrusted_tool(name), name
    rt = make_rt(env)
    _start(rt, "ut")
    advice = {"u1": "first way", "u4": "image way", "t1": "second way", "u5": "late way"}
    monkeypatch.setattr(rt, "_observe", lambda *a, **k: advice.get(a[4], ""))
    kw = dict(args={}, session_id="ut", turn_id="ut:t:0")
    raw = json.dumps({"results": [{"content": "page text " * 20}]})
    for cid, tool in (("u1", "web_extract"), ("u2", "mcp__probe__echo"), ("u3", "browser_click")):
        assert rt.transform_tool_result(tool_name=tool, result=raw, tool_call_id=cid,
                                        status="ok", **kw) is None
    shot = {"_multimodal": True, "content": [{"type": "text", "text": "shot"}],
            "text_summary": "shot"}
    assert rt.tool_execution(tool_name="browser_vision", next_call=lambda a: shot,
                             tool_call_id="u4", **kw) == shot
    s = rt._existing("ut")
    assert not s.attached                           # nothing for the request path to re-add
    out = json.loads(rt.transform_tool_result(tool_name="terminal", tool_call_id="t1",
                                              result=terminal_result("ok", 0), status="ok",
                                              **kw))[runtime.ADVICE_FIELD]
    assert [w in out for w in ("first way", "image way", "second way")] == [True] * 3
    assert not s.held and s.undelivered == 0
    # Held advice the turn never found a result for is counted, and does not reach a later turn.
    rt.transform_tool_result(tool_name="web_search", result=raw, tool_call_id="u5", status="ok",
                             **kw)
    rt.on_session_end(completed=True, platform="cli", session_id="ut", turn_id="ut:t:0")
    assert not s.held and s.undelivered == 1
    # A call Hermes abandoned can return after its turn ended: its advice is that turn's, and
    # the next turn's results do not carry it.
    advice.update({"u6": "stale way", "t2": ""})
    rt.tool_execution(tool_name="browser_vision", next_call=lambda a: shot, tool_call_id="u6",
                      **kw)
    _start(rt, "ut")
    nxt = dict(kw, turn_id="ut:t:1")
    rt.pre_llm_call(session_id="ut", turn_id="ut:t:1", user_message="y",
                    conversation_history=[], platform="cli")
    assert rt.transform_tool_result(tool_name="terminal", tool_call_id="t2", result="ok",
                                    status="ok", **nxt) is None
    rt.on_session_end(completed=True, platform="cli", session_id="ut", turn_id="ut:t:1")
    assert not s.held and s.undelivered == 2
