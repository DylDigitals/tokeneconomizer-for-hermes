"""Per-project segments (docs/plans/per-project-segments.md): a Hermes session that runs commands
in another repository continues there as a new upstream session, each folded into its own
repository. Tests 1-8 fail before the change; 9 is the no-switch regression set."""
import json
import os
import subprocess
import threading

import pytest

from test_adapter import (CATALOG, FAIL, FakeLlm, Log, _ledger, env, make_rt, runtime,  # noqa: F401
                          seams, session, te, terminal_result)
from te_hermes_plugin import host as hostmod
from te_hermes_plugin.host import HermesHost, SessionLog
from tokeneconomizer.plugin import denylist
from tokeneconomizer.plugin.paths import repo_identity


def echoed(out, code, cwd):
    """A terminal result whose command changed directory: Hermes echoes `cwd`."""
    return json.dumps({"output": out, "exit_code": code, "error": None, "cwd": str(cwd)})


def term(command, result, workdir=None):
    args = {"command": command}
    if workdir:
        args["workdir"] = str(workdir)
    return ("terminal", args, result, "ok")


def git_repo(path, commit=False):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if commit:
        subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    return path


@pytest.fixture
def seg(env, tmp_path, monkeypatch):
    """`home`: a plain folder a gateway session starts in. `R` (= env.repo), `S`: repositories."""
    home = tmp_path / "home"
    home.mkdir()
    env.home_dir = home
    env.R = env.repo
    env.S = git_repo(tmp_path / "S")
    monkeypatch.setenv("TERMINAL_CWD", str(home))
    return env


def label(path):
    return repo_identity(str(path)).label


def folds(rt, sid):
    """{upstream session id: [repo label, ...]} of the ledger's session records for `sid`."""
    out = {}
    for r in _ledger(rt):
        if r.get("kind") == "session" and (r["session_id"] == sid
                                           or r["session_id"].startswith(sid + "~")):
            out.setdefault(r["session_id"], []).append(r["repo"])
    return out


def seed_facts(rt, monkeypatch, where, prefix):
    monkeypatch.setenv("TERMINAL_CWD", str(where))       # config is per repository
    assert te(rt, "config", "set", "microskills=envfacts")[0] == 0
    for n in range(4):
        session(rt, f"{prefix}{n}", [("run the tests", FAIL)])


def attached_facts(out):
    """The facts block a transformed JSON terminal result carries, or ''."""
    try:
        data = json.loads(out)
    except (TypeError, ValueError):
        return ""
    block = data.get(runtime.ADVICE_FIELD, "") if isinstance(data, dict) else ""
    return block if runtime.FACTS_MARK in block else ""


def prompts_in(path):
    if not os.path.exists(path):
        return 0
    with open(path) as fh:
        return sum(1 for ln in fh if '"type": "user"' in ln and '"cwd"' in ln)


# ---- 1. home-started session moves into R mid-turn -------------------------------------------
def test_cd_into_repo_switches_and_each_segment_folds_once(seg, monkeypatch):
    rt = make_rt(seg)
    seed_facts(rt, monkeypatch, seg.R, "learn")
    monkeypatch.setenv("TERMINAL_CWD", str(seg.home_dir))
    contexts, _ = session(rt, "g1", [("fix R", [
        term(f"cd {seg.R} && git status", echoed("clean", 0, seg.R))])])
    blocks = [attached_facts(c) for c in contexts if attached_facts(c)]
    assert len(blocks) == 1 and "python3" in blocks[0], contexts
    assert runtime.FACTS_MARK not in contexts[0]          # home has no facts
    assert folds(rt, "g1") == {"g1": [label(seg.home_dir)], "g1~2": [label(seg.R)]}
    rec = next(r for r in _ledger(rt) if r.get("session_id") == "g1~2")
    assert rec["prompts"] == 1 and rec["untool"] is not None
    rt.shutdown()


# ---- 2. where a command ran: echo, then workdir, then the tracked directory -------------------
def test_workdir_switches_without_moving_the_tracked_directory(seg):
    rt = make_rt(seg)
    session(rt, "g2", [("work", [
        term("git log -1", terminal_result("x", 0), workdir=seg.R),       # ran in R
        term("ls", terminal_result("x", 0)),                              # ran in home: no switch
        term(f"cd {seg.S}", echoed("", 0, seg.S), workdir=seg.R),         # ended in S
        term("ls", terminal_result("x", 0)),                              # home again
    ])])
    assert folds(rt, "g2") == {"g2": [label(seg.home_dir)], "g2~2": [label(seg.R)],
                               "g2~3": [label(seg.S)]}
    rt.shutdown()


# ---- 3. several moves, back to an earlier repository ----------------------------------------
def test_home_r_s_r_makes_four_segments(seg):
    rt = make_rt(seg)
    session(rt, "g3", [("hop", [term(f"cd {seg.R}", echoed("", 0, seg.R)),
                                term(f"cd {seg.S}", echoed("", 0, seg.S))]),
                       ("back", [term(f"cd {seg.R}", echoed("", 0, seg.R))])], first=True)
    assert folds(rt, "g3") == {"g3": [label(seg.home_dir)], "g3~2": [label(seg.R)],
                               "g3~3": [label(seg.S)], "g3~4": [label(seg.R)]}
    rt.shutdown()


def _deny(path, tools):
    f = os.path.join(str(path), seams.DENY_FILE)
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f, "w") as fh:
        json.dump({seams.DENY_KEY: tools}, fh)


def _request(rt, sid, tid):
    out = rt.llm_request(request={"tools": [{"type": "function", "function": {"name": n}}
                                            for n in CATALOG]},
                         session_id=sid, turn_id=tid, api_mode="chat_completions",
                         platform="cli")
    tools = (out or {}).get("request", {}).get("tools") or [{"function": {"name": n}}
                                                            for n in CATALOG]
    return {t["function"]["name"] for t in tools}


# ---- 4. gateway restart mid-segment ---------------------------------------------------------
def test_restart_resumes_the_current_segment_with_its_frozen_deny_list(seg):
    _deny(seg.home_dir, ["image_generate"])
    _deny(seg.R, ["text_to_speech"])
    rt = make_rt(seg)
    session(rt, "g4", [("go", [term(f"cd {seg.R}", echoed("", 0, seg.R))])], finalize=False)
    assert rt.drain(60)
    rt.shutdown()
    home_log, seg_log = rt._log_path("g4"), rt._log_path("g4~2")
    before_home, before_seg = prompts_in(home_log), prompts_in(seg_log)

    rt2 = make_rt(seg)
    rt2.pre_llm_call(session_id="g4", turn_id="g4:t:0001", user_message="more",
                     conversation_history=[{"role": "user", "content": "go"}],
                     is_first_turn=False, platform="cli")
    s = rt2._existing("g4")
    assert s.useg == "g4~2" and os.path.realpath(s.cwd) == os.path.realpath(str(seg.R))
    names = _request(rt2, "g4", "g4:t:0001")
    assert "image_generate" not in names and "text_to_speech" in names
    assert prompts_in(home_log) == before_home and prompts_in(seg_log) == before_seg + 1
    rt2.shutdown()


# ---- 5. compaction child seen first after a restart -----------------------------------------
def test_compaction_child_after_restart_continues_the_root_segment(seg):
    rt = make_rt(seg)
    session(rt, "root5", [("go", [term(f"cd {seg.R}", echoed("", 0, seg.R))])], finalize=False)
    rt.pre_llm_call(session_id="child5", turn_id="root5:t:0001", user_message="x",
                    conversation_history=[], is_first_turn=False, platform="cli")
    assert rt._existing("child5").sid == "root5"
    assert rt.drain(60)
    rt.shutdown()

    rt2 = make_rt(seg)
    rt2.pre_llm_call(session_id="child5", turn_id="child5:t:0002", user_message="y",
                     conversation_history=[], is_first_turn=False, platform="cli")
    s = rt2._existing("child5")
    assert s.sid == "root5" and s.useg == "root5~2"
    rt2.shutdown()


# ---- 6. usage and subagent records follow the current segment, each once ---------------------
def test_usage_and_subagents_after_a_switch_land_in_the_current_segment(seg):
    rt = make_rt(seg)
    sid, tid = "g6", "g6:t:0000"
    rt.pre_llm_call(session_id=sid, turn_id=tid, user_message="go", conversation_history=[],
                    is_first_turn=True, platform="cli")
    _request(rt, sid, tid)
    posted = []

    def usage(n):
        rid = f"{tid}:r{n}"
        posted.append(rid)
        rt.post_api_request(session_id=sid, turn_id=tid, api_request_id=rid, model="m",
                            platform="cli", usage={"input_tokens": 10, "output_tokens": 1})

    usage(0)
    racer = threading.Thread(target=lambda: [usage(n) for n in range(1, 41)])
    racer.start()
    rt.transform_tool_result(tool_name="terminal", args={"command": f"cd {seg.R}"},
                             result=echoed("", 0, seg.R), session_id=sid,
                             tool_call_id="c6", turn_id=tid, status="ok")
    racer.join()
    rt.subagent_start(parent_session_id=sid, child_session_id="sub6")
    rt.post_api_request(session_id="sub6", api_request_id="sub6:r", model="m",
                        platform="subagent", usage={"input_tokens": 5, "output_tokens": 1})
    rt.on_session_end(session_id=sid, turn_id=tid, completed=True, platform="cli")
    rt.on_session_finalize(session_id=sid, reason="exit")
    assert rt.drain(60)

    def ids(path):
        with open(path) as fh:
            return [json.loads(ln)["message"]["id"] for ln in fh
                    if '"usage"' in ln and '"assistant"' in ln]

    home_ids, seg_ids = ids(rt._log_path(sid)), ids(rt._log_path(f"{sid}~2"))
    assert sorted(home_ids + seg_ids) == sorted(posted)          # every request exactly once
    assert os.path.exists(rt._child_log_path(f"{sid}~2", "sub6"))
    assert not os.path.exists(rt._child_log_path(sid, "sub6"))
    total = sum(int(r.get("requests") or 0) for r in _ledger(rt)
                if r.get("kind") == "session" and r["session_id"] in (sid, f"{sid}~2"))
    assert total == len(posted) + 1                              # + the subagent's request
    rt.shutdown()


# ---- 7. P15: a tool the session could not see is no evidence in the destination ---------------
def _untool(rt, where):
    ctx = rt.ctx_class().build(HermesHost(), str(where), rt.env(), session_id=None, settle=False)
    return ctx, ctx.store.read("untool.json") or {}


@pytest.mark.parametrize("r_applied", [False, True])
def test_masked_tool_streak_is_not_advanced_in_the_destination(seg, monkeypatch, r_applied):
    rt = make_rt(seg)
    monkeypatch.setenv("TERMINAL_CWD", str(seg.R))
    session(rt, "r7", [("work", [term("ls", terminal_result("x", 0))])])
    ctx, st = _untool(rt, seg.R)
    assert st["streaks"]["image_generate"] == 1 and st["streaks"]["text_to_speech"] == 1
    if r_applied:
        _deny(seg.R, ["browser_navigate"])
        denylist._record(ctx.store, ["browser_navigate"],
                         denylist.settings_path(str(seg.R), "hermes"))
    _deny(seg.home_dir, ["image_generate"])
    monkeypatch.setenv("TERMINAL_CWD", str(seg.home_dir))
    session(rt, "g7", [("go", [term(f"cd {seg.R}", echoed("", 0, seg.R))])])
    assert hostmod.session_mask(rt._log_path("g7~2")) == {"image_generate"}
    _, st = _untool(rt, seg.R)
    assert st["streaks"]["image_generate"] == 1                 # hidden: not counted
    assert st["streaks"]["text_to_speech"] == 2                 # visible and unused: counted
    assert "image_generate" not in st.get("proposal", [])
    rt.shutdown()


def test_session_without_mask_record_scores_as_before(tmp_path):
    log = tmp_path / "s.jsonl"
    SessionLog(str(log)).catalog(["a"])
    assert hostmod.session_mask(str(log)) is None
    from tokeneconomizer.config import ToolingConfig
    from tokeneconomizer.toolprune import ToolingStage
    assert ToolingStage(ToolingConfig(evidence=1), "hermes", "agent").session_mask == set()


# ---- 8. facts repair is block-specific and does not double advice restoration ----------------
def test_facts_repair_targets_the_current_segment_block(seg, monkeypatch):
    rt = make_rt(seg)
    seed_facts(rt, monkeypatch, seg.R, "learn8")
    monkeypatch.setenv("TERMINAL_CWD", str(seg.home_dir))
    sid, tid = "g8", "g8:t:0000"
    rt.pre_llm_call(session_id=sid, turn_id=tid, user_message="go", conversation_history=[],
                    is_first_turn=True, platform="cli")
    _request(rt, sid, tid)
    res = echoed("", 0, seg.R)
    out = rt.transform_tool_result(tool_name="terminal", args={"command": f"cd {seg.R}"},
                                   result=res, session_id=sid, tool_call_id="c8",
                                   turn_id=tid, status="ok")
    block = rt._existing(sid).facts_block
    assert block and attached_facts(out) == block
    esc = runtime._in_json(block)[1:-1]

    def req(tool_content):
        return {"messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go\n\n" + runtime.FACTS_MARK + " home facts"},
            {"role": "assistant", "tool_calls": [{"id": "c8", "type": "function",
                                                  "function": {"name": "terminal"}}]},
            {"role": "tool", "tool_call_id": "c8", "content": tool_content}]}

    def count(request):
        text = json.dumps(request["messages"], ensure_ascii=False)
        return text.count(runtime._in_json(block)[1:-1]) + text.count(
            runtime._in_json(esc)[1:-1])

    # Intact escaped attachment: nothing added.
    assert rt.llm_request(request=req(out), session_id=sid, turn_id=tid,
                          api_mode="chat_completions", platform="cli") is None
    # Another plugin rewrote the result: advice restoration puts it back once, no facts repair.
    r = rt.llm_request(request=req("compressed"), session_id=sid, turn_id=tid,
                       api_mode="chat_completions", platform="cli")["request"]
    assert count(r) == 1 and block in r["messages"][3]["content"]
    # Compaction dropped the tool row; the home block survives: the project block is repaired.
    compacted = req(out)
    compacted["messages"] = compacted["messages"][:2]
    rt._existing(sid).attached.clear()
    r = rt.llm_request(request=compacted, session_id=sid, turn_id=tid,
                       api_mode="chat_completions", platform="cli")["request"]
    assert count(r) == 1 and block in r["messages"][1]["content"]
    rt.shutdown()


# ---- 9. regressions: no switch, no segment file ---------------------------------------------
def _no_segments(rt, sid):
    d = os.path.join(rt.root, "hermes-sessions")
    names = os.listdir(d) if os.path.isdir(d) else []
    assert not any(n.startswith(sid + "~") or n == f"{sid}.segment.json" for n in names), names


def test_plain_directory_does_not_switch(seg, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    rt = make_rt(seg)
    session(rt, "n1", [("go", [term(f"cd {plain}", echoed("", 0, plain))])])
    _no_segments(rt, "n1")
    rt.shutdown()


def test_linked_worktree_of_the_current_repository_does_not_switch(seg, monkeypatch, tmp_path):
    wt = tmp_path / "R-wt"
    subprocess.run(["git", "-C", str(seg.R), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    subprocess.run(["git", "-C", str(seg.R), "worktree", "add", "-q", str(wt)], check=True)
    monkeypatch.setenv("TERMINAL_CWD", str(seg.R))
    rt = make_rt(seg)
    session(rt, "n2", [("go", [term(f"cd {wt}", echoed("", 0, wt))])])
    _no_segments(rt, "n2")
    rt.shutdown()


def test_untrusted_result_naming_a_repository_does_not_switch(seg):
    rt = make_rt(seg)
    session(rt, "n3", [("go", [("web_extract", {"urls": ["x"]}, echoed("", 0, seg.R), "ok"),
                               ("mcp_x_tool", {"workdir": str(seg.R)}, echoed("", 0, seg.R),
                                "ok")])])
    _no_segments(rt, "n3")
    rt.shutdown()


def test_non_local_backend_does_not_switch(seg, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    rt = make_rt(seg)
    session(rt, "n4", [("go", [term(f"cd {seg.R}", echoed("", 0, seg.R))])])
    _no_segments(rt, "n4")
    rt.shutdown()


def test_disabled_plugin_does_not_switch(seg, monkeypatch):
    rt = make_rt(seg)
    assert te(rt, "config", "set", "enabled=false")[0] == 0
    session(rt, "n5", [("go", [term(f"cd {seg.R}", echoed("", 0, seg.R))])])
    _no_segments(rt, "n5")
    rt.shutdown()
