"""Two Hermes profiles in ONE process: separate plugin managers, runtimes, state roots and
command handlers. Run via harness/hxpy with HX_ROOT set; profiles live under $HX_ROOT/p1, p2."""
import json, os, subprocess, sys
# Mirror hermes_cli/main.py bootstrap: the venv's editable finder lists root modules from
# install time, so modules added by a later Hermes update (e.g. hermes_yaml) need the root.
sys.path.insert(0, os.environ["HERMES_SRC"])     # set by harness/hxpy
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from hermes_cli import plugins as hp

base = sys.argv[1]
out = {}
for name in ("p1", "p2"):
    home = os.path.join(base, name, ".hermes")
    tok = set_hermes_home_override(home)
    try:
        m = hp.get_plugin_manager(); m.discover_and_load()
        repo = os.path.join(base, name, "repo")
        os.environ["TERMINAL_CWD"] = repo
        h = hp.get_plugin_command_handler("te")
        out[name] = {"manager": id(m), "status": hp.resolve_plugin_command_result(h("status"))}
    finally:
        reset_hermes_home_override(tok)
mods = [k for k in sys.modules if k == "tokeneconomizer"]
import tokeneconomizer
out["vendor_origin"] = tokeneconomizer.__file__
from tokeneconomizer.plugin import sidecall
# No process-wide runner patch: each profile's runtime binds its own (context.py).
out["runner_for_patched"] = sidecall.runner_for.__module__ != "tokeneconomizer.plugin.sidecall"
print(json.dumps(out, indent=1))
