"""Segment review round 1 (implementation): late duplicate observers, finalize racing a
switch, plugin reload keeping Hermes's terminal directory, and a folder that becomes a repo."""
import subprocess
import threading

from test_segments import seg, env, make_rt, echoed, terminal_result, _request  # noqa: F401


def start(rt, sid):
    rt.pre_llm_call(session_id=sid, turn_id=sid + ":t:0", user_message="go",
                    conversation_history=[], is_first_turn=True, platform="cli")
    _request(rt, sid, sid + ":t:0")


def observe(rt, sid, cid, args, result, turn=0):
    return rt.transform_tool_result(tool_name="terminal", args=args, result=result,
                                    session_id=sid, tool_call_id=cid,
                                    turn_id=f"{sid}:t:{turn}", status="ok")


def test_late_duplicate_observer_after_turn_end_does_not_switch_back(seg):
    rt = make_rt(seg)
    sid = "latepost"
    start(rt, sid)
    args, result = {"command": f"cd {seg.R}"}, echoed("", 0, seg.R)
    observe(rt, sid, "first", args, result)
    rt.on_session_end(session_id=sid, turn_id=sid + ":t:0", completed=True, platform="cli")
    rt.pre_llm_call(session_id=sid, turn_id=sid + ":t:1", user_message="now S",
                    conversation_history=[], is_first_turn=False, platform="cli")
    observe(rt, sid, "second", {"command": f"cd {seg.S}"}, echoed("", 0, seg.S), turn=1)
    # Hermes lets a timed-out observer finish late: turn 0's post_tool_call arrives now.
    rt.post_tool_call(tool_name="terminal", args=args, result=result, session_id=sid,
                      tool_call_id="first", turn_id=sid + ":t:0", status="ok")
    s = rt._existing(sid)
    rt.shutdown()
    assert (s.useg, s.cwd, s.dir) == (sid + "~3", str(seg.S), str(seg.S))


class _GatedLock:
    """s.lock stand-in: the named thread waits at its first acquire until released."""

    def __init__(self, lock, name):
        self.lock, self.name, self.reached, self.release = lock, name, threading.Event(), \
            threading.Event()
        self.gated = False

    def __enter__(self):
        if threading.current_thread().name == self.name and not self.gated:
            self.gated = True
            self.reached.set()
            assert self.release.wait(10)
        return self.lock.__enter__()

    def __exit__(self, *a):
        return self.lock.__exit__(*a)


def test_finalize_racing_a_switch_ends_the_current_segment(seg):
    rt = make_rt(seg)
    sid = "raceend"
    start(rt, sid)
    s = rt._existing(sid)
    gate = s.lock = _GatedLock(s.lock, "finalizer")
    t = threading.Thread(name="finalizer",
                         target=lambda: rt.on_session_finalize(session_id=sid, reason="exit"))
    t.start()
    assert gate.reached.wait(10)
    observe(rt, sid, "cd", {"command": f"cd {seg.R}"}, echoed("", 0, seg.R))
    gate.release.set()
    t.join(10)
    assert not t.is_alive()
    rec = rt._session_record(s)
    rt.shutdown()
    assert rec.get("session_id") == sid + "~2" and rec.get("end_reason") == "exit"


def test_no_switch_after_finalize(seg):
    rt = make_rt(seg)
    sid = "closed"
    start(rt, sid)
    s = rt._existing(sid)
    rt.on_session_finalize(session_id=sid, reason="exit")
    # A caller that already held the session when it was finalized.
    with s.lock:
        facts = rt._maybe_switch(s, "terminal", {"command": f"cd {seg.R}"},
                                 echoed("", 0, seg.R), "late", sid + ":t:0")
    rt.shutdown()
    assert (facts, s.useg) == ("", sid)


def test_call_id_reused_in_a_later_turn_is_a_new_call(seg):
    # Hermes accepts an id again once compaction removed the earlier call from history.
    rt = make_rt(seg)
    sid = "idreuse"
    start(rt, sid)
    observe(rt, sid, "call_0", {"command": f"cd {seg.R}"}, echoed("", 0, seg.R))
    rt.on_session_end(session_id=sid, turn_id=sid + ":t:0", completed=True, platform="cli")
    rt.pre_llm_call(session_id=sid, turn_id=sid + ":t:1", user_message="now S",
                    conversation_history=[], is_first_turn=False, platform="cli")
    observe(rt, sid, "call_0", {"command": f"cd {seg.S}"}, echoed("", 0, seg.S), turn=1)
    s = rt._existing(sid)
    rt.shutdown()
    assert (s.useg, s.cwd) == (sid + "~3", str(seg.S))


def test_late_observer_from_an_ended_turn_never_switches(seg):
    # Even once its record has aged out of the duplicate memory.
    rt = make_rt(seg)
    sid = "eviction"
    start(rt, sid)
    args, result = {"command": f"cd {seg.R}"}, echoed("", 0, seg.R)
    observe(rt, sid, "first", args, result)
    for i in range(300):
        observe(rt, sid, f"n{i}", {"command": "true"}, terminal_result("", 0))
    rt.on_session_end(session_id=sid, turn_id=sid + ":t:0", completed=True, platform="cli")
    rt.pre_llm_call(session_id=sid, turn_id=sid + ":t:1", user_message="now S",
                    conversation_history=[], is_first_turn=False, platform="cli")
    observe(rt, sid, "second", {"command": f"cd {seg.S}"}, echoed("", 0, seg.S), turn=1)
    rt.post_tool_call(tool_name="terminal", args=args, result=result, session_id=sid,
                      tool_call_id="first", turn_id=sid + ":t:0", status="ok")
    s = rt._existing(sid)
    rt.shutdown()
    assert (s.useg, s.cwd, s.dir) == (sid + "~3", str(seg.S), str(seg.S))


def test_plugin_reload_follows_hermes_terminal_directory(seg, monkeypatch):
    from tools.terminal_tool import clear_session_cwd, record_session_cwd
    monkeypatch.setenv("HERMES_SESSION_KEY", "k-reload")
    rt = make_rt(seg)
    sid = "reload"
    start(rt, sid)
    observe(rt, sid, "r", {"command": f"cd {seg.R}"}, echoed("", 0, seg.R))
    observe(rt, sid, "s", {"command": "ls", "workdir": str(seg.S)}, terminal_result("x", 0))
    record_session_cwd("k-reload", str(seg.R))      # Hermes's record: the cd, not the workdir
    rt.shutdown()                                   # plugin reload; Hermes keeps running
    try:
        rt2 = make_rt(seg)
        start(rt2, sid)
        observe(rt2, sid, "r-again", {"command": "git status"}, terminal_result("clean", 0))
        s = rt2._existing(sid)
        rt2.shutdown()
    finally:
        clear_session_cwd("k-reload")
    assert (s.useg, s.cwd) == (sid + "~4", str(seg.R))


def test_folder_that_becomes_a_repository_switches(seg, tmp_path):
    new = tmp_path / "new-project"
    new.mkdir()
    rt = make_rt(seg)
    sid = "newrepo"
    start(rt, sid)
    observe(rt, sid, "c1", {"command": "ls", "workdir": str(new)}, terminal_result("x", 0))
    subprocess.run(["git", "init", "-q", str(new)], check=True)
    observe(rt, sid, "c2", {"command": "git status", "workdir": str(new)},
            terminal_result("clean", 0))
    s = rt._existing(sid)
    rt.shutdown()
    assert s.useg == sid + "~2"


def _end(rt, sid, turn):
    rt.on_session_end(session_id=sid, turn_id=f"{sid}:t:{turn}", completed=True, platform="cli")


def _begin(rt, sid, turn):
    rt.pre_llm_call(session_id=sid, turn_id=f"{sid}:t:{turn}", user_message="continue",
                    conversation_history=[], is_first_turn=False, platform="cli")


def test_late_duplicate_after_many_later_turns_never_switches(seg):
    rt = make_rt(seg)
    sid = "old-ended"
    start(rt, sid)
    args, result = {"command": f"cd {seg.R}"}, echoed("", 0, seg.R)
    observe(rt, sid, "first", args, result)
    _end(rt, sid, 0)
    for turn in range(1, 300):
        _begin(rt, sid, turn)
        observe(rt, sid, f"c{turn}", {"command": "true"}, terminal_result("", 0), turn=turn)
        _end(rt, sid, turn)
    _begin(rt, sid, 300)
    observe(rt, sid, "current", {"command": f"cd {seg.S}"}, echoed("", 0, seg.S), turn=300)
    rt.post_tool_call(tool_name="terminal", args=args, result=result, session_id=sid,
                      tool_call_id="first", turn_id=f"{sid}:t:0", status="ok")
    s = rt._existing(sid)
    rt.shutdown()
    assert (s.useg, s.cwd, s.dir) == (sid + "~3", str(seg.S), str(seg.S))


def test_late_old_call_does_not_block_its_reused_id_in_the_current_turn(seg):
    rt = make_rt(seg)
    sid = "poison"
    start(rt, sid)
    args, result = {"command": f"cd {seg.R}"}, echoed("", 0, seg.R)
    observe(rt, sid, "call_0", args, result)
    for i in range(300):
        rt.transform_tool_result(tool_name="read_file", args={"path": "a"}, result="x",
                                 session_id=sid, tool_call_id=f"read-{i}",
                                 turn_id=f"{sid}:t:0", status="ok")
    _end(rt, sid, 0)
    _begin(rt, sid, 1)
    rt.post_tool_call(tool_name="terminal", args=args, result=result, session_id=sid,
                      tool_call_id="call_0", turn_id=f"{sid}:t:0", status="ok")
    observe(rt, sid, "call_0", {"command": f"cd {seg.S}"}, echoed("", 0, seg.S), turn=1)
    s = rt._existing(sid)
    rt.shutdown()
    assert (s.useg, s.cwd, s.dir) == (sid + "~3", str(seg.S), str(seg.S))


def test_late_old_transform_does_not_take_the_new_calls_facts(seg, monkeypatch):
    from test_segments import attached_facts, seed_facts
    rt = make_rt(seg)
    seed_facts(rt, monkeypatch, seg.S, "seed")
    monkeypatch.setenv("TERMINAL_CWD", str(seg.home_dir))
    sid = "facts-theft"
    start(rt, sid)
    old_args, old_result = {"command": f"cd {seg.R}"}, echoed("", 0, seg.R)
    observe(rt, sid, "call_0", old_args, old_result)
    _end(rt, sid, 0)
    _begin(rt, sid, 1)
    args, result = {"command": f"cd {seg.S}"}, echoed("", 0, seg.S)
    rt.post_tool_call(tool_name="terminal", args=args, result=result, session_id=sid,
                      tool_call_id="call_0", turn_id=f"{sid}:t:1", status="ok")
    old_out = observe(rt, sid, "call_0", old_args, old_result)
    new_out = observe(rt, sid, "call_0", args, result, turn=1)
    rt.shutdown()
    assert not attached_facts(old_out)
    assert "python3" in attached_facts(new_out)
