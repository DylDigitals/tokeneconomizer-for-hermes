"""The Hermes host adapter for upstream TokenEconomizer.

Hermes has no hook *protocol* (plugins are Python callbacks), so, as upstream does for Pi, this
plugin speaks upstream's Claude-shaped protocol on Hermes's behalf:

* Each Hermes session gets a session log in Claude Code transcript form, written by the plugin
  from Hermes hooks (`SessionLog`). Upstream's own Claude readers (`trace.claude_*`) then count
  its prompts, tool calls and token usage unchanged. Tool names stay Hermes names, so tool
  pruning sees exactly the tools Hermes sent.
* For the mechanisms that read shell commands and file edits (micro-facts, loop guard), Hermes
  tool calls are translated to the Claude tool they correspond to (`to_claude_call`), the same
  move upstream makes for Codex and Pi logs.
* Output stays upstream-shaped; `deliver` routes `additionalContext` and `systemMessage`.
"""
import json
import os
import re
import threading
import time

from tokeneconomizer import trace
from tokeneconomizer.trace.jsonl import lines as jsonl_lines
from tokeneconomizer.plugin import hosts
from tokeneconomizer.plugin.adapter import LiveAdapter


class HermesAdapter(LiveAdapter):
    """Upstream's live adapter plus Hermes's admission check for paid facts."""

    def paid_fact_filter(self, spec, cited):
        return paid_fact_ok(spec, cited)

# Hermes tool -> (Claude tool, {hermes arg: claude arg}).
_CLAUDE_EQUIV = {
    "terminal": ("Bash", {"command": "command", "workdir": "cwd"}),
    "read_file": ("Read", {"path": "file_path", "offset": "offset", "limit": "limit"}),
    "write_file": ("Write", {"path": "file_path", "content": "content"}),
    "patch": ("Edit", {"path": "file_path", "old_string": "old_string",
                       "new_string": "new_string", "replace_all": "replace_all"}),
    "search_files": ("Grep", {"pattern": "pattern", "path": "path", "file_glob": "glob"}),
}


def to_claude_call(name, args):
    """(claude_name, claude_args) for a Hermes tool call; unknown tools pass through."""
    equiv = _CLAUDE_EQUIV.get(name)
    if not equiv or not isinstance(args, dict):
        return name, args if isinstance(args, dict) else {}
    cname, amap = equiv
    return cname, {amap[k]: v for k, v in args.items() if k in amap}


def tool_response(result):
    """A Hermes tool result string as an upstream `tool_response`: the parsed JSON object when
    the tool returned one (terminal's carries `exit_code`), else the text."""
    if isinstance(result, str):
        try:
            obj = json.loads(result)
        except ValueError:
            return result
        return obj if isinstance(obj, dict) else result
    return result


class SessionLog:
    """Append-only Claude-transcript lines for one Hermes session (one writer lock per file)."""

    _locks = {}
    _locks_guard = threading.Lock()

    def __init__(self, path):
        self.path = path
        with self._locks_guard:
            self._lock = self._locks.setdefault(path, threading.Lock())

    def _write(self, obj):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        line = _redact(json.dumps(obj, default=str)) + "\n"
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)

    def prompt(self, text, cwd):
        self._write({"type": "user", "cwd": cwd, "timestamp": time.time(),
                     "message": {"role": "user", "content": str(text or "")}})

    def tool_call(self, call_id, name, args, result_text, ok, cwd):
        self._write({"type": "assistant", "cwd": cwd, "timestamp": time.time(),
                     "message": {"role": "assistant", "content": [
                         {"type": "tool_use", "id": call_id, "name": name,
                          "input": args if isinstance(args, dict) else {}}]}})
        self._write({"type": "user", "timestamp": time.time(), "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                         "content": result_text, "is_error": not ok}]}})

    def catalog(self, names):
        """The tools one model request was offered (pruning's per-session catalog)."""
        self._write({"type": "te_catalog", "timestamp": time.time(), "tools": sorted(names)})

    def mask(self, names):
        """The tools held back from this session by a deny list it started with elsewhere
        (a later segment): no evidence about them here (`session_mask`, engine P15)."""
        self._write({"type": "te_mask", "timestamp": time.time(), "tools": sorted(names)})

    def usage(self, request_id, model, usage):
        """One API request's usage, or a marker that the provider reported none."""
        if not isinstance(usage, dict):
            self._write({"type": "te_usage_unknown", "request_id": request_id, "model": model,
                         "timestamp": time.time()})
            return
        self._write({"type": "assistant", "timestamp": time.time(), "message": {
            "id": request_id, "role": "assistant", "model": model, "content": [],
            "usage": {"input_tokens": int(usage.get("input_tokens") or 0),
                      "cache_read_input_tokens": int(usage.get("cache_read_tokens") or 0),
                      "cache_creation_input_tokens": int(usage.get("cache_write_tokens") or 0),
                      "output_tokens": int(usage.get("output_tokens") or 0),
                      "reasoning_tokens": int(usage.get("reasoning_tokens") or 0)}}})


REDACTION_FAILED = "[redaction unavailable: omitted]"


def _redact(text):
    """Hermes's secret redactor, forced on: every copy the plugin keeps is redacted. If the
    redactor fails, the text is dropped, never kept raw."""
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text, force=True)
    except Exception:
        return REDACTION_FAILED


def sanitize(obj, _depth=0):
    """`obj` with every string redacted (dict keys kept): what the plugin hands upstream and
    writes anywhere. Redaction is secret-pattern scrubbing, not private-data classification."""
    if isinstance(obj, str):
        return _redact(obj) if obj else obj
    if _depth > 12:
        return None
    if isinstance(obj, dict):
        return {k: sanitize(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v, _depth + 1) for v in obj]
    return obj


def session_catalog(path):
    """{tool: ""} the session's model requests were offered (`te_catalog` lines), or None when
    none was recorded: then fold falls back to the profile snapshot."""
    names = set()
    seen = False
    for line in jsonl_lines(path) if path and os.path.exists(path) else ():
        if '"te_catalog"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        seen = True
        names.update(t for t in ev.get("tools") or () if isinstance(t, str))
    return {t: "" for t in names} if seen else None


# ---- paid fact admission ----------------------------------------------------------------
_MAX_INJECTION = 200
_UNSAFE = re.compile(
    r"[<>{}]|```|https?://|\b(ignore|disregard|override|system prompt|previous instructions|"
    r"you are|act as|pretend|jailbreak|exfiltrat\w*|send|upload|curl|wget|password|token|"
    r"secret|api[_ -]?key|credential\w*|sudo|rm -rf)\b", re.I)
_TOKEN = re.compile(r"[\w./+-]{3,}")


def session_mask(path):
    """{tools} of the log's `te_mask` record, or None when it has none (a first segment)."""
    found = None
    for line in jsonl_lines(path):
        if '"te_mask"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "te_mask":
            found = (found or set()) | {t for t in ev.get("tools") or () if isinstance(t, str)}
    return found


def paid_fact_ok(spec, cited):
    """Admit a paid proposer's fact only as one short declarative line about what earlier
    sessions observed: no markup or delimiters, no instructions to the agent beyond the
    repository fact, and (on admission, when `cited` is given) at least one of its command or
    file words seen in the cited sessions' commands. Heuristic and conservative: a rejected
    fact is quarantined, not injected. Rechecked on every render, so stored state is too."""
    text = str(spec.get("injection") or spec.get("claim") or "")
    if not text or len(text) > _MAX_INJECTION or "\n" in text or "\r" in text:
        return False
    if _UNSAFE.search(text):
        return False
    if cited is None:
        return True
    seen = " ".join(str(dg.get("_cmd_blob", "")) for dg in cited)
    words = {w for w in _TOKEN.findall(text) if any(c in w for c in "./-_") or w.isalpha()}
    return any(w in seen for w in words if len(w) >= 3 and w.lower() not in _COMMON)


PAID_HEADER = "# repo operating manual"


def screen_block(block):
    """`block` (a rendered facts block) less any paid-manual line `paid_fact_ok` rejects; a paid
    section left with no lines is dropped with its header. Free environment facts (templated by
    upstream, never model-written) are kept as they are."""
    if not block or PAID_HEADER not in block:
        return block
    sections, cur = [], []
    for ln in block.splitlines():
        if ln.startswith("# ") and cur:
            sections.append(cur)
            cur = []
        cur.append(ln)
    sections.append(cur)
    out = []
    for sec in sections:
        if sec[0].startswith(PAID_HEADER):
            sec = [sec[0]] + [ln for ln in sec[1:] if not ln.strip() or (
                ln.startswith("- ") and paid_fact_ok({"injection": ln[2:]}, None))]
            if not any(ln.startswith("- ") for ln in sec):
                continue
        out += sec
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n" if out else ""


_COMMON = frozenset("the and this that with from use run not are for its has have when "
                    "repository repo tests test instead only".split())


def _translate(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as fh:
        for line in jsonl_lines(src):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            content = (ev.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    b["name"], b["input"] = to_claude_call(b.get("name"), b.get("input"))
            fh.write(json.dumps(ev) + "\n")


def unknown_usage_requests(path):
    n = 0
    for line in jsonl_lines(path):
        n += '"te_usage_unknown"' in line
    return n


class HermesHost(hosts.Host):
    name = "hermes"
    interrupt = "Press Ctrl+C to interrupt it."
    end_run = "Press Ctrl+C to end the run."
    prompt_context = True           # pre_llm_call context reaches the model
    relays_notices = True           # plugin messages have no user-visible channel of their own
    keeps_start_context = True      # Hermes persists and replays pre_llm_call context
    command_prefix = "/te"          # how a person runs `te <name>` here (controller notices)
    adapter_cls = HermesAdapter

    def session_catalog(self, transcript_path):
        return session_catalog(transcript_path)

    def session_mask(self, transcript_path):
        return session_mask(transcript_path)

    def cwd(self, p, env):
        return p.get("cwd") or env.get("TERMINAL_CWD") or os.getcwd()

    def env_session_id(self, env):
        return str(env.get("TE_SESSION_ID") or env.get("HERMES_SESSION_ID") or "")

    def turn_key(self, p):
        return str(p.get("prompt_id") or "")

    def is_subagent(self, p):
        return bool(p.get("agent_id"))

    def headless(self, env, transcript=None):
        return str(env.get("TE_HERMES_HEADLESS", "")).strip() == "1"

    def tool_call(self, p):
        name, args = to_claude_call(str(p.get("tool_name") or ""), p.get("tool_input") or {})
        resp = p.get("tool_response")
        return name, args, hosts.response_text(resp), hosts._exit_code(resp)

    def mechanism_trace(self, transcript_path, workdir):
        """The session log with Hermes tools renamed to their Claude equivalents, its subagents'
        logs beside it in Claude Code's layout (`<trace>/subagents/*.jsonl`)."""
        if not transcript_path or not os.path.exists(transcript_path):
            return None
        out = os.path.join(workdir, "hermes_trace.jsonl")
        _translate(transcript_path, out)
        for sub in trace.claude_subagent_files(transcript_path):
            _translate(sub, os.path.join(workdir, "hermes_trace", "subagents",
                                         os.path.basename(sub)))
        return out

    def session_usage(self, transcript_path):
        usage = trace.claude_session_usage(transcript_path, include_subagents=True)
        unknown = sum(unknown_usage_requests(p) for p in
                      [transcript_path] + trace.claude_subagent_files(transcript_path))
        if unknown:
            # A request whose provider reported no usage: the session's cost is unknown, not
            # the sum of the known part (upstream records effective_tokens=None, "no_usage").
            usage = dict(usage, input_tokens=None, unknown_requests=unknown)
        return usage

    def prompt_count(self, transcript_path):
        return trace.claude_prompt_count(transcript_path)

    def tool_usage(self, trace_path):
        # Fold hands pruning the translated trace; count in Hermes names, the tools Hermes sends.
        # Subagents included, as on Claude Code: a tool a subagent used is in use.
        counts = trace.claude_tool_usage(os.path.realpath(trace_path), include_subagents=True)
        out = {}
        for name, n in counts.items():
            name = _HERMES_NAME.get(name, name)
            out[name] = out.get(name, 0) + n
        return out


_HERMES_NAME = {cname: hname for hname, (cname, _) in _CLAUDE_EQUIV.items()}
