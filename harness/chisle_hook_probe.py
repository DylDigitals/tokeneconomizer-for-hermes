"""Real Hermes hook dispatch with the real Chisle and TokenEconomizer plugins loaded: which
`transform_tool_result` string reaches the model when both want to change the same result?

    HX_ROOT=<root with both plugins> HX_PATH_PREFIX=$W/sandbox/bin-node harness/hxpy harness/chisle_hook_probe.py
"""
import json
import os
import sys

sys.path.insert(0, os.environ["HERMES_SRC"])     # set by harness/hxpy
from hermes_cli.plugins import discover_plugins, get_plugin_manager  # noqa: E402
import model_tools  # noqa: E402

discover_plugins(force=True)
cbs = get_plugin_manager()._hooks.get("transform_tool_result", [])
print("hook order:", [getattr(cb, "__module__", "?") for cb in cbs])
rt = next(cb.__self__ for cb in cbs if hasattr(cb, "__self__") and "tokeneconomizer" in cb.__module__)

sid, turn, call = "probe-s1", "probe-s1:t:0", "call-1"
rt.pre_llm_call(session_id=sid, turn_id=turn, user_message="x", conversation_history=[],
                is_first_turn=True, platform="cli")
s = rt._tracked(sid, turn)
ADVICE = "PROBE-REDIRECT: stop retrying the same command"
s.calls[(turn, call, "terminal")] = {"advice": [ADVICE]}          # the guard decided on this call

big = "\n".join(f"remote-check: step {i} fetching build-cache shard {i} ... ok" for i in range(300))
result = json.dumps({"output": big + "\nerror: transient upstream timeout", "exit_code": 3,
                     "error": None})
ids = model_tools._CallIds(None, sid, call, turn, None)
out = model_tools._apply_transform_tool_result_hook("terminal", {"command": "./scripts/remote-check.sh"},
                                                    result, 0, ids)
print(json.dumps({"compressed_by_chisle": len(out) < len(result) and "chisle" in out.lower(),
                  "te_advice_delivered": ADVICE in out,
                  "chars_in": len(result), "chars_out": len(out)}))

# The model-bound request (Hermes's own llm_request middleware chain) for that stored result.
from hermes_cli.middleware import apply_llm_request_middleware  # noqa: E402
req = {"model": "m", "tools": [{"type": "function", "function": {"name": "terminal"}}],
       "messages": [{"role": "user", "content": "x"},
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": call, "type": "function",
                         "function": {"name": "terminal", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": call, "content": out}]}
mw = apply_llm_request_middleware(req, session_id=sid, turn_id=turn, api_mode="chat_completions",
                                  platform="cli", task_id=None, api_request_id="r1")
sent = mw.payload["messages"][-1]["content"]
print(json.dumps({"model_sees_compressed": "chisle" in sent.lower(),
                  "model_sees_advice": sent.count(ADVICE)}))
