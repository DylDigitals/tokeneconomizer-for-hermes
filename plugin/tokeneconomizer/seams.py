"""Load the vendored upstream TokenEconomizer and extend it for the Hermes host.

Vendored upstream is Microsoft TokenEconomizer at PINNED_REVISION plus the small documented
patches in `_vendor/PATCHES.md`: `_vendor/UPSTREAM.sha256` hashes the original bytes,
`_vendor/PINNED.sha256` the shipped ones (checked at load). The process-wide registrations
below are identical data for every profile and are asserted, so an upstream change that moves a
target fails loudly. Per-profile behaviour lives on each runtime's context (`context.py`).
None of this touches Hermes itself.
"""
import contextvars
import hashlib
import os
import sys

VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")
PINNED_REVISION = "7042ea89dd5e5ecbc67ad865ad6e256e61be8bf0"
DENY_FILE = os.path.join(".tokeneconomizer", "hermes-tools.local.json")   # the root profile's
DENY_ENV = "TE_HERMES_DENY_FILE"    # a subprocess's (and the launcher's) profile pruning file
DENY_KEY = "deny_tools"

# Hermes's equivalents of the tools upstream protects for a person (Bash, Read, Edit, Agent,
# Skill, ToolSearch, WebFetch, ...): core file/terminal surface, skills, memory, delegation,
# clarification, todo, web and the deferred-tool bridge. Never proposed for pruning.
HERMES_PROTECTED = frozenset({
    "terminal", "process", "process_manage", "read_file", "write_file", "patch", "search_files",
    "web_search", "web_extract", "skills_list", "skill_view", "skill_manage", "todo",
    "todo_list", "memory", "session_search", "delegate_task", "clarify", "tool_search",
    "tool_describe", "tool_call",
    "hermes_tool_search",  # the tool_search bridge as sent to xAI models
})
# The NotebookEdit analogue: proposed only after upstream's high-risk evidence factor.
HERMES_HIGH_RISK = frozenset({"execute_code"})


def import_vendor():
    """Import the vendored package as top-level `tokeneconomizer` (upstream's absolute imports
    and `-m tokeneconomizer...` subprocesses need that name); refuse any other copy."""
    existing = sys.modules.get("tokeneconomizer")
    if existing is None and VENDOR not in sys.path:
        sys.path.insert(0, VENDOR)
    import tokeneconomizer
    origin = os.path.realpath(tokeneconomizer.__file__)
    if not origin.startswith(os.path.realpath(VENDOR) + os.sep):
        # Another Hermes profile in this process loaded its own copy of this plugin first.
        # The module name is process-wide; share it only if it is byte-identical to our pin.
        other = os.path.dirname(os.path.dirname(origin))
        bad = verify_pin(root=other)
        if bad:
            raise ImportError(f"tokeneconomizer already imported from {origin}, which differs "
                              f"from the pin under {VENDOR}: {bad[:3]}")
    return tokeneconomizer


def verify_pin(root=None):
    """[(path, problem)] for files under `root` (default: this plugin's vendor dir) that differ
    from this plugin's PINNED.sha256 (empty when intact)."""
    root = root or VENDOR
    problems = []
    with open(os.path.join(VENDOR, "PINNED.sha256"), encoding="utf-8") as fh:
        for line in fh:
            digest, _, rel = line.strip().partition("  ")
            path = os.path.join(root, rel)
            try:
                with open(path, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest() != digest:
                        problems.append((rel, "modified"))
            except OSError:
                problems.append((rel, "missing"))
    return problems


def install():
    """Register the Hermes host with the vendored library, once per process. Only identical
    data is registered (host class name, deny-file location, protected tool names), so it does
    not matter which profile's copy of this plugin runs it first. Per-profile behaviour (the
    optimizer runner, job spawning) lives on each runtime's own context (`context.py`)."""
    import_vendor()
    from tokeneconomizer.plugin import adapter, denylist, hosts
    from . import host as hermes_host

    # Process-wide, on the shared module: another profile's copy of this file may have run.
    if not getattr(hosts, "_hermes_seams_installed", False):
        assert isinstance(hosts.HOSTS, dict) and "claude" in hosts.HOSTS
        hosts.HOSTS["hermes"] = hermes_host.HermesHost

        assert isinstance(denylist._SETTINGS, dict) and isinstance(denylist._UNITS, dict)
        assert callable(denylist._json_list_keys)
        denylist._SETTINGS["hermes"] = DENY_FILE
        denylist._UNITS["hermes"] = "tool"
        upstream_keys = denylist._json_list_keys
        denylist._json_list_keys = (lambda h: (None, DENY_KEY) if h == "hermes"
                                    else upstream_keys(h))

        assert isinstance(adapter.INTERACTIVE_PROTECTED, frozenset)
        assert isinstance(adapter.HIGH_RISK, frozenset)
        adapter.INTERACTIVE_PROTECTED = adapter.INTERACTIVE_PROTECTED | HERMES_PROTECTED
        adapter.HIGH_RISK = adapter.HIGH_RISK | HERMES_HIGH_RISK
        hosts._hermes_seams_installed = True

    # Each profile prunes from its own file (`deny_file`). The file name is a context variable
    # read wherever upstream looks `hermes` up in `_SETTINGS`; it lives on the shared vendored
    # module, so every profile's copy of this plugin (and a reloaded copy) sets the same one.
    if not isinstance(getattr(denylist, "_hermes_deny_var", None), contextvars.ContextVar):
        var = contextvars.ContextVar("te_hermes_deny_file", default=DENY_FILE)

        class _Settings(dict):
            def get(self, key, default=None):
                return var.get() if key == "hermes" else dict.get(self, key, default)

            def __getitem__(self, key):
                return var.get() if key == "hermes" else dict.__getitem__(self, key)

        assert denylist._SETTINGS.get("hermes") == DENY_FILE
        denylist._SETTINGS = _Settings(denylist._SETTINGS)
        denylist._hermes_deny_var = var


def deny_var():
    """The process-wide context variable naming the current profile's pruning file."""
    from tokeneconomizer.plugin import denylist
    return denylist._hermes_deny_var


def deny_file(home):
    """A profile's pruning file, relative to the checkout: the root profile (or a home Hermes
    does not recognise as a profile) keeps `DENY_FILE`; a named profile gets its own."""
    try:
        from hermes_constants import profile_name_for_home
        name = profile_name_for_home(home)
    except Exception:
        name = None
    if not name or name == "default":
        return DENY_FILE
    return os.path.join(".tokeneconomizer", f"hermes-{name}-tools.local.json")


def valid_deny_file(rel):
    """`rel` if it names a pruning file inside `.tokeneconomizer/`, else None."""
    import re
    ok = re.fullmatch(r"\.tokeneconomizer/hermes(-[A-Za-z0-9_][A-Za-z0-9_.-]*)?-tools\.local\.json",
                      rel or "")
    return rel if ok and ".." not in rel else None


ENTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "te_entry.py")

# `te` commands that run Hermes sessions (drivers.py); outside Hermes they are refused.
DRIVER_ACTIONS = ("measure", "refresh-catalog")
_GLOBAL_OPTS = ("--host", "--cwd")       # upstream accepts both before or after the subcommand
_VALUED_OPTS = ("--sessions", "--model")   # probe's options that take a value
HELP = ("-h", "--help")


def split_command(argv):
    """(words, other_args, cwd) of a `te` command line: the positional words, every other
    option as given, and the `--cwd` value (None if absent). `--host`/`--cwd` and their values
    are taken out wherever they stand, as upstream's parser accepts them."""
    words, rest, cwd, i = [], [], None, 0
    while i < len(argv):
        a = argv[i]
        name, eq, val = a.partition("=")
        if name in _GLOBAL_OPTS:
            if not eq:
                nxt = argv[i + 1] if i + 1 < len(argv) else ""
                val = "" if nxt.startswith("-") else nxt
                i += 1 if val else 0
            if not val or (name == "--host" and val != "hermes"):
                rest.append(a)              # malformed: left for the caller to refuse
            elif name == "--cwd":
                cwd = val
        elif name in _VALUED_OPTS:
            rest.append(name)               # `--sessions=6` and `--sessions 6` alike
            if eq:
                rest.append(val)
            elif i + 1 < len(argv):
                i += 1
                rest.append(argv[i])
        elif a.startswith("-"):
            rest.append(a)
        else:
            words.append(a)
        i += 1
    return words, rest, cwd


def runs_hermes_sessions(words, rest=()):
    """True for `probe` and `toolprune|untool measure|refresh-catalog`, unless help is asked
    for (upstream's parser prints that, and runs nothing)."""
    if set(rest) & set(HELP):
        return False
    return words[:1] == ["probe"] or (
        words[:1] in (["toolprune"], ["untool"]) and words[1:2] in ([a] for a in DRIVER_ACTIONS))


def driver_option_error(words, rest):
    """Why the options of a Hermes-session command are wrong (checked before any session
    starts, so nothing is spent on a typo), or None."""
    allowed = ("--keep", "--sessions", "--model") if words[0] == "probe" else ()
    extra = words[1:] if words[0] == "probe" else words[2:] if words[1] != "measure" else []
    if extra:
        return f"te {' '.join(words[:2])}: unexpected argument {extra[0]}"
    i = 0
    while i < len(rest):
        opt = rest[i]
        if opt not in allowed:
            return f"te {' '.join(words[:2])}: unknown option {opt}"
        if opt in _VALUED_OPTS:
            val = rest[i + 1] if i + 1 < len(rest) else ""
            if not val or val.startswith("-") or (opt == "--sessions" and not val.isdigit()):
                return f"te probe: {opt} takes a {'number' if opt == '--sessions' else 'value'}"
            i += 1
        i += 1
    return None


def subprocess_spawn(ctx, argv, log_name):
    """Upstream's detached `te <argv>` (no model calls), through the entry point that
    registers the Hermes host. Returns the process; its owner terminates it on unload."""
    import subprocess
    env = dict(ctx.env, PYTHONUTF8="1")
    cmd = [sys.executable, ENTRY, *argv, "--host", "hermes", "--cwd", ctx.repo.root]
    ctx.store.ensure()
    with open(ctx.store.path(log_name), "a", encoding="utf-8") as log:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                cwd=ctx.repo.root, env=env, start_new_session=True)
