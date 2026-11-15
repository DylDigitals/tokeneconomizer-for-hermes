"""`/te` and `hermes te`: upstream's `te` CLI for this profile.

Commands that call no model run upstream's CLI in a subprocess with an explicit environment, so
every command stays on this profile's state root (`$HERMES_HOME/plugin-data/tokeneconomizer`)
and the right repository, whatever else runs in the Hermes process. `fold` needs the optimizer,
so it runs on the profile's own worker in this process. `probe`, `toolprune measure` and
`toolprune refresh-catalog` are Hermes drivers (`drivers.py`).

Repository: `/te` acts on the session's working directory, resolved by Hermes's own scoped
resolver (a refusal is reported, never replaced by another checkout); `hermes te` on the
shell's directory.
"""
import os
import shlex
import subprocess
import sys

from . import seams

TIMEOUT_S = 120


def run(runtime, argv, cwd):
    """(exit_code, output) of `te <argv>` for this profile in repository `cwd`."""
    argv = list(argv) or ["status"]
    words, rest, cwd_opt = seams.split_command(argv)
    if words[:1] == ["fold"]:
        sids = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--session"]
        return runtime.run_fold(cwd_opt or cwd, sids, verbose="--verbose" in argv)
    if seams.runs_hermes_sessions(words, rest):
        err = seams.driver_option_error(words, rest)
        if err:
            return 2, err
        from . import drivers
        with runtime.deny_scope():
            return drivers.run(runtime, words + rest, cwd_opt or cwd)
    if words[:1] == ["judge"]:
        return 2, "te judge: internal (the loop guard runs it)"
    env = dict(runtime.env(), PYTHONUTF8="1")
    try:
        p = subprocess.run([sys.executable, seams.ENTRY, *argv, "--host", "hermes", "--cwd", cwd],
                           cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 124, f"te {' '.join(argv)}: timed out after {TIMEOUT_S}s"
    out = (p.stdout + (p.stderr if p.returncode else "")).rstrip()
    if argv[0] == "doctor":
        out += "\n" + hermes_doctor(runtime)
    return p.returncode, out


def hermes_doctor(runtime):
    """The Hermes side of `te doctor`: what upstream's doctor cannot see from here."""
    import json
    from tokeneconomizer.plugin.adapter import catalog_path
    bad = seams.verify_pin()
    lines = ["hermes      plugin adapter (host `hermes`)",
             f"  vendor    upstream {seams.PINNED_REVISION[:12]} + documented Hermes patches "
             "(_vendor/PATCHES.md): " + ("intact" if not bad else f"MODIFIED: {bad[:3]}")]
    try:
        with open(catalog_path(runtime.root, "hermes"), encoding="utf-8") as fh:
            n = len(json.load(fh).get("tools") or [])
        lines.append(f"  catalog   {n} tool(s) seen in this profile's requests (pruning uses "
                     "each session's own)")
    except (OSError, ValueError):
        lines.append("  catalog   none yet (captured from the first model request)")
    route = "ctx.llm, task tokeneconomizer_optimizer (auxiliary.tokeneconomizer_optimizer; "
    route += "default: the main model route)"
    lines.append(f"  optimizer {route}")
    try:
        from hermes_cli.config import load_config_readonly
        entry = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}).get(
            "tokeneconomizer") or {}
        granted = bool((entry.get("llm") or {}).get("allow_model_override"))
    except Exception:
        granted = False
    lines.append("  paid_model " + ("honoured (model override granted)" if granted else
                 "ignored unless plugins.entries.tokeneconomizer.llm.allow_model_override: true"))
    lines.append("  limits    -z folds at turn end; codex_app_server mode bypasses request "
                 "middleware (no pruning, no facts repair)")
    return "\n".join(lines)


def slash(runtime, raw_args):
    try:
        argv = shlex.split(raw_args or "")
    except ValueError as e:
        return f"te: {e}"
    try:
        cwd = runtime.resolve_cwd(strict=True)
    except Exception as e:
        return f"te: cannot resolve this session's working directory ({type(e).__name__}); " \
               "nothing was changed"
    code, out = run(runtime, argv, cwd)
    return out + ("" if code == 0 else f"\n(exit {code})")


def cli(runtime, argv):
    """`hermes te ...`: the shell's directory."""
    return run(runtime, argv, os.path.realpath(os.getcwd()))
