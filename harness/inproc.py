"""In-process probe against the installed Hermes plugin manager (run via harness/hxpy)."""
import json, sys
from hermes_cli import plugins as hp
m = hp.get_plugin_manager(); m.discover_and_load()
out = {"loaded": [p for p in m.list_plugins() if p.get("name") == "tokeneconomizer"],
       "commands": sorted(k for k, v in hp.get_plugin_commands().items() if v.get("plugin") == "tokeneconomizer"),
       "skills": m.list_plugin_skills("tokeneconomizer"),
       "skill_path": str(m.find_plugin_skill("tokeneconomizer:tokeneconomizer"))}
for arg in sys.argv[1:]:
    h = hp.get_plugin_command_handler("te")
    out.setdefault("te", {})[arg] = hp.resolve_plugin_command_result(h(arg)) if h else None
print(json.dumps(out, indent=1, default=str))
