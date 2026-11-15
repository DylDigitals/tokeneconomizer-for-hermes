"""TokenEconomizer Hermes plugin: registration only (see runtime.py for the event map)."""
import logging
from pathlib import Path

logger = logging.getLogger("hermes_plugins.tokeneconomizer")

SKILL_MD = Path(__file__).parent / "skills" / "tokeneconomizer" / "SKILL.md"
SKILL_DESCRIPTION = "What TokenEconomizer does in Hermes and how to control it (/te)."

def register(ctx):
    from hermes_constants import get_hermes_home

    from . import controls, runner, seams
    from .runtime import Runtime

    seams.install()
    root = ctx.get_config("state_root")
    rt = Runtime(str(get_hermes_home()), ctx.llm, logger,
                 state_root=root if isinstance(root, str) and root.strip() else None)

    ctx.register_hook("pre_llm_call", rt.pre_llm_call)
    ctx.register_hook("pre_tool_call", rt.pre_tool_call)
    ctx.register_hook("post_tool_call", rt.post_tool_call)
    ctx.register_hook("transform_tool_result", rt.transform_tool_result)
    ctx.register_hook("transform_llm_output", rt.transform_llm_output)
    ctx.register_hook("post_api_request", rt.post_api_request)
    ctx.register_hook("on_session_end", rt.on_session_end)
    ctx.register_hook("on_session_finalize", rt.on_session_finalize)
    ctx.register_hook("on_session_reset", lambda old_session_id="", reason="", **_:
                      rt.on_session_finalize(session_id=old_session_id, reason=reason))
    ctx.register_middleware("llm_request", rt.llm_request)
    ctx.register_middleware("tool_execution", rt.tool_execution)
    ctx.register_hook("subagent_start", rt.subagent_start)

    ctx.register_auxiliary_task(
        runner.TASK, display_name="TokenEconomizer optimizer",
        description="Micro-fact proposer and loop-guard judge (no tools, short prompts).",
        defaults={"timeout": runner.TIMEOUT_S})
    ctx.register_command("te", lambda raw: controls.slash(rt, raw),
                         description="TokenEconomizer: status, config, facts, toolprune, repos",
                         args_hint="status|config|facts|toolprune|repos|repomap|doctor|fold|probe")

    def setup_cli(parser):
        import argparse
        parser.add_argument("te_args", nargs=argparse.REMAINDER,
                            help="te subcommand and arguments")

    def run_cli(args):
        # `hermes te` acts on the shell's directory; `/te` on the session's working directory.
        code, out = controls.cli(rt, list(getattr(args, "te_args", []) or []))
        print(out)
        return code

    ctx.register_cli_command("te", help="TokenEconomizer controls (same as /te)",
                             setup_fn=setup_cli, handler_fn=run_cli)
    ctx.register_skill("tokeneconomizer", SKILL_MD, description=SKILL_DESCRIPTION)
    # Claude Code lists a plugin's skill with the others; Hermes lists plugin skills nowhere,
    # so the plugin names its own skill in the session prompt.
    ctx.register_system_prompt_section(
        "tokeneconomizer.skill",
        "Plugin skill available (load with skill_view when relevant):\n"
        f"- tokeneconomizer:tokeneconomizer: {SKILL_DESCRIPTION}", max_chars=400)

    import atexit
    atexit.register(rt.drain_at_exit)

    ctx.on_unload(rt.shutdown)
