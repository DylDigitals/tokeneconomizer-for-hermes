"""End-to-end fold contracts that must survive an upstream engine replacement."""
import json

from test_adapter import env, make_rt, session, terminal_result, _ledger, CATALOG  # noqa: F401
from te_hermes_plugin.host import HermesHost, SessionLog
from tokeneconomizer.plugin import fold


def test_refolding_a_resumed_session_charges_new_usage_once_without_relearning(env):
    rt = make_rt(env)
    session(rt, "resume-cost", [("work", [])])
    ctx = rt.ctx_class().build(HermesHost(), str(env.repo), rt.env(),
                               session_id="resume-cost", settle=False)
    before = ctx.store.read("untool.json")
    SessionLog(rt._log_path("resume-cost")).usage("second-request", "anthropic/claude-haiku-5.5",
        {"input_tokens": 70, "cache_read_tokens": 500, "cache_write_tokens": 5,
         "output_tokens": 20})
    fold.fold_session(ctx, "resume-cost")
    fold.fold_session(ctx, "resume-cost")
    records = _ledger(rt)
    assert [r["kind"] for r in records] == ["session", "usage_delta"]
    delta = records[1]
    assert [delta[k] for k in ("requests", "input_tokens", "cache_read_input_tokens",
                               "cache_creation_input_tokens", "output_tokens")] == [1, 70, 500, 5, 20]
    assert ctx.store.read("untool.json") == before
    rt.shutdown()


def test_fold_uses_session_catalog_and_hermes_tool_names_not_profile_union(env):
    rt = make_rt(env)
    session(rt, "catalog-fold", [("read files", [
        ("terminal", {"command": "pwd"}, terminal_result(str(env.repo), 0), "ok")
    ])], finalize=False)
    # A concurrent session has different tools; its latest profile snapshot is not ours.
    rt._record_catalog(["other_session_tool"])
    rt.on_session_finalize(session_id="catalog-fold", reason="exit")
    assert rt.drain(60)
    ctx = rt.ctx_class().build(HermesHost(), str(env.repo), rt.env(),
                               session_id="catalog-fold", settle=False)
    state = ctx.store.read("untool.json")
    assert set(state["catalog_tools"]) == set(CATALOG)
    assert "terminal" in state["used_tools"]
    assert "Bash" not in state["used_tools"]
    assert "other_session_tool" not in state["streaks"]
    rt.shutdown()
