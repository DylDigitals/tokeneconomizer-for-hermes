"""Subprocess entry for `te` commands and detached jobs under the Hermes host.

Loads this plugin directory as a package (outside Hermes), registers the Hermes host with the
vendored library, then runs upstream's CLI. This process has no Hermes LLM facade, so it never
runs a model call: `fold` and `judge` run inside Hermes on the profile's own worker
(`runtime.py`), and are refused here rather than run with no proposer.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "te_hermes_plugin", os.path.join(HERE, "__init__.py"), submodule_search_locations=[HERE])
_mod = importlib.util.module_from_spec(_spec)
sys.modules["te_hermes_plugin"] = _mod
_spec.loader.exec_module(_mod)

from te_hermes_plugin import seams  # noqa: E402

seams.install()
from tokeneconomizer.plugin import cli  # noqa: E402
from tokeneconomizer.plugin.context import LiveContext  # noqa: E402

MODEL_JOBS = ("fold", "judge")


def _spawn(self, argv, log_name):
    # A job this command starts goes through this entry too (the host must be registered).
    if argv and argv[0] in MODEL_JOBS:
        return False            # left pending: Hermes folds it at the next session start
    seams.subprocess_spawn(self, argv, log_name)
    return True


def _no_runner(self):
    # Upstream's default for an unknown host is the `claude` CLI: never another agent here.
    return None


# Only in this subprocess: there is no Hermes process state to share.
LiveContext.spawn = _spawn
LiveContext.side_runner = _no_runner

if __name__ == "__main__":
    args = sys.argv[1:]
    # The profile whose pruning file this command reads and writes (Runtime.env, the launcher).
    rel = seams.valid_deny_file(os.environ.get(seams.DENY_ENV, ""))
    if rel:
        seams.deny_var().set(rel)
    words, rest, _ = seams.split_command(args)
    if words[:1] and words[0] in MODEL_JOBS:
        print(f"te {words[0]}: runs inside Hermes (`/te {words[0]}` or `hermes te {words[0]}`), "
              "where the optimizer has the profile's model route", file=sys.stderr)
        sys.exit(2)
    if seams.runs_hermes_sessions(words, rest):
        # Upstream drives another agent's CLI for these; Hermes sessions run in drivers.py.
        head = " ".join(words[:2] if words[0] != "probe" else words[:1])
        print(f"te {head}: runs Hermes sessions, so use `hermes te {head}` "
              "(`hermes -p <profile> te ...` for a named profile)", file=sys.stderr)
        sys.exit(2)
    sys.exit(cli.main(args))
