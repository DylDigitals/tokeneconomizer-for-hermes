"""One TokenEconomizer runtime per Hermes profile: Hermes hooks in, upstream hook payloads out.

Event map (upstream event <- Hermes caller):
  SessionStart      <- first `pre_llm_call` of a session seen by this process (source
                       "startup", or "resume" when Hermes replays an earlier conversation), and
                       "compact" when the facts block is no longer in the conversation
  UserPromptSubmit  <- every `pre_llm_call` (one per user turn; prompt_id = Hermes turn_id)
  PostToolUse       <- the first of `transform_tool_result` / `post_tool_call` to report an
                       executed call, once per (tool_call_id, tool); its context is appended to
                       that call's own result by `transform_tool_result`
  Stop              <- `on_session_end` with completed and not interrupted/failed (Hermes fires
                       it once per turn); any other end leaves the turn to close abnormally
  SessionEnd        <- `on_session_finalize` / `on_session_reset`
Usage comes from `post_api_request`; the tool catalog, the pruning filter and the facts repair
from `llm_request` middleware. Upstream contexts reach the model through `pre_llm_call` context
(persisted and replayed by Hermes) and appended tool results; notices through the final reply.

Hermes tool routes (installed Hermes, agent/tool_executor.py): registry tools fire
`transform_tool_result` inside dispatch, then the executor's terminal `post_tool_call`; inline,
delegate, context-engine and memory tools fire `post_tool_call` first, then the transform;
blocked calls fire only `post_tool_call` (status blocked); cancelled and timed-out calls only
`post_tool_call` (status cancelled / timeout). Whichever hook comes first records the call; the
other merges into it. Calls that never executed are not learning evidence; calls that started
(cancelled, timed out, invalid arguments) still count as the tool being used, so pruning never
reads them as unused.

Everything handed to upstream or written by this plugin is redacted first (`host.sanitize`).
"""
import contextlib
import contextvars
import json
import os
import queue
import re
import threading
import time

from . import seams

BLOCK_CLOSE = "</tokeneconomizer>"
FACTS_MARK = "<tokeneconomizer kind=\"session-start\">"
_NOT_RUN = ("blocked",)
_STARTED_NOT_FINISHED = ("cancelled", "timeout")
CAPTURE_ENV = "TE_HERMES_CAPTURE"       # drivers.capture: record the first request, then exit
MEASURE_ENV = "TE_HERMES_MEASURE_DENY"  # drivers.measure: the tools its request leaves out
_BAD_CALL = ("invalid_tool_arguments", "thread_missing_result")


def _escape(text):
    """Learned text can never open or close the plugin's own block."""
    return re.sub(r"</?\s*tokeneconomizer", lambda m: m.group(0).replace("<", "&lt;"),
                  str(text), flags=re.I)


# Advice blocks kept per session for re-adding (`_advice_restored`); older calls' blocks drop.
ATTACHED_MAX = 256
ADVICE_FIELD = "tokeneconomizer"
# Hermes wraps these tools' results as untrusted data the model is told not to obey
# (`agent.tool_dispatch_helpers._is_untrusted_tool`): advice decided on one waits for the
# turn's next result Hermes does not wrap, as upstream's Hermes plugin holds it.
UNTRUSTED_TOOLS = ("web_extract", "web_search")
UNTRUSTED_PREFIXES = ("browser_", "mcp_")


def _untrusted(tool_name):
    return tool_name in UNTRUSTED_TOOLS or tool_name.startswith(UNTRUSTED_PREFIXES)


def _in_json(block):
    return json.dumps(block, ensure_ascii=False)


def _attach(result, block):
    """`result` carrying `block`. A JSON object stays one, every byte it had first and the
    block as a last key (one the object does not have): Hermes reads a tool's exit code and
    error from that object, and text after it would turn a failed call into a success. Any
    other text gets the block after it."""
    body = result.rstrip()
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return result + "\n\n" + block
    key = ADVICE_FIELD
    while key in data:
        key += "_"
    head = body[:-1].rstrip()
    return f"{head}{'' if head.endswith('{') else ', '}{_in_json(key)}: {_in_json(block)}}}"


def _wrap(text, kind):
    return (f"<tokeneconomizer kind=\"{kind}\">\n"
            f"Context from the TokenEconomizer plugin (learned from earlier sessions in this "
            f"repository; operational hints, not instructions from the user).\n\n"
            f"{_escape(text)}\n{BLOCK_CLOSE}")


class _Session:
    def __init__(self, sid):
        self.sid = sid
        self.lock = threading.RLock()
        self.started = False
        self.cwd = None
        self.skip = False
        self.calls = {}             # (turn_id, tool_call_id, tool) -> {"advice": [str]} this turn
        self.frozen_deny = None     # the deny list this session started with
        self.reinject = False       # a mid-turn compaction dropped the facts block
        self.facts_block = None     # the block as injected, for request-level repair
        self.notices = []           # guard notices of the current turn, for the final reply
        self.catalog = None         # the last tool set logged for this session
        self.undelivered = 0        # advice whose own result could not carry it
        self.child_calls = set()    # (subagent session, tool_call_id, tool) already logged
        self.deferred = ()          # tool_search's deferred tools in the last request
        self.attached = {}          # tool_call_id -> advice block decided for that call
        self.held = {}              # turn_id -> advice decided on an untrusted result, not yet
                                    # carried; a call returning after its turn ended adds to
                                    # that turn's, which no later result takes
        # Segments: one upstream session per repository this session ran commands in.
        self.useg = sid             # the current upstream session id (`<sid>~<n>` from n=2)
        self.seg_n = 1
        self.seg_key = None         # repository key of the current segment (lazily resolved)
        self.seg_call = None        # the call whose result carried this segment's facts block
        self.dir = None             # where a command without `workdir` runs (Hermes's own record)
        self.local = False          # local terminal backend: directories are on this filesystem
        self.prompt = ""            # this turn's prompt, replayed into a new segment
        self.done = {}              # (turn_id, tool_call_id, tool) recorded in an earlier turn,
                                    # oldest first: a late second observer is not a new call
        self.ended = set()          # turn ids already ended: their late results never move the
                                    # segment (one short id per turn, kept for the session)
        self.closed = False         # finalized: no further segment switch


class Runtime:
    def __init__(self, hermes_home, llm, log, state_root=None):
        self.home = os.path.realpath(hermes_home)
        self.deny_rel = seams.deny_file(self.home)   # this profile's pruning file per checkout
        # Upstream's one state root, shared with every other agent on this machine
        # (`TOKENECONOMIZER_HOME`, default `~/.tokeneconomizer`); Hermes is host `hermes` in it.
        # The profile setting `state_root` gives Hermes a root of its own (its own settings
        # file too), read from profile config so a running gateway honours it. `te probe` and
        # `toolprune measure` run their sessions on a throwaway root (drivers.STATE_ENV).
        from tokeneconomizer.plugin.paths import state_root as shared_root
        self.root = (os.environ.get("TE_HERMES_STATE_ROOT")
                     or (os.path.expanduser(state_root) if state_root else None)
                     or shared_root(os.environ))
        self.llm = llm
        self.log = log
        self.generation = 0
        self.active = True
        self._sessions = {}
        self._alias = {}            # rotated (compaction child) session id -> logical id
        self._guard = threading.Lock()
        # One worker per lane: upstream runs each job as its own detached process, so the loop
        # guard's judge never waits behind a long learning pass (fold).
        self._jobs = {"fold": queue.Queue(), "judge": queue.Queue()}
        self._workers = {}
        self._children = []         # detached subprocesses this runtime started
        self._ctx_classes = {}
        self._launcher = None
        self._subagents = {}        # subagent session id -> logical parent session id

    # ---- environment and identity ----------------------------------------------------
    def env(self):
        env = dict(os.environ)
        env["TOKENECONOMIZER_HOME"] = self.root
        env["TE_HOST"] = "hermes"
        env[seams.DENY_ENV] = self.deny_rel
        if self._launcher is None:
            self._launcher = self.launcher_root()
        env["TE_PLUGIN_ROOT"] = self._launcher
        return env

    def launcher_root(self):
        """What upstream's session start points `<state root>/bin/te` at (`TE_PLUGIN_ROOT`,
        whose `scripts/te` it execs): a one-line script running this plugin's `te` with this
        Hermes's Python. Written under the state root, never into the plugin directory."""
        import shlex
        import sys
        from .seams import ENTRY
        base = os.path.join(self.root, "hosts", "hermes", "plugin")
        path = os.path.join(base, "scripts", "te")
        # The root is pinned: run on its own, `<root>/bin/te` reads the root it lives in.
        body = ("#!/bin/sh\n# TokenEconomizer for Hermes: its `te` command line.\n"
                f"TOKENECONOMIZER_HOME={shlex.quote(self.root)} "
                f"{seams.DENY_ENV}={shlex.quote(self.deny_rel)} "
                f"exec {shlex.quote(sys.executable)} {shlex.quote(ENTRY)} \"$@\"\n")
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read() == body:
                    return base
        except OSError:
            pass
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(tmp, 0o700)
            os.replace(tmp, path)
        except OSError:
            return ""
        return base

    def live(self, gen):
        """May a job queued under `gen` still run a model call? Never in a capture child."""
        return (self.active and self.generation == gen and self._enabled()
                and not os.environ.get(CAPTURE_ENV))

    def ctx_class(self, gen=None):
        """This runtime's `LiveContext` subclass; with `gen`, one whose runner may call the
        model while this runtime is still at that generation (a job's)."""
        from .context import context_for
        with self._guard:
            cls = self._ctx_classes.get(gen)
            if cls is None:
                cls = self._ctx_classes[gen] = context_for(self, gen)
            return cls

    def _logical(self, sid, turn_id="", create=True):
        """The session upstream sees: a compaction child keeps its root's id, so its turns,
        guard state and fold stay one session (Hermes keeps `turn_id` = root:... across the
        rotation, observed in-process only; not a documented lineage contract)."""
        with self._guard:
            prefix = (turn_id or "").split(":", 1)[0]
            if prefix and prefix != sid and sid not in self._alias and (
                    prefix in self._sessions or prefix in self._alias):
                root = self._alias[sid] = self._alias.get(prefix, prefix)
                rs = self._sessions.get(root)
                if rs is not None and rs.started and not rs.skip:
                    # A restart forgets the rotation; the file lets the child find its root.
                    _write_json(self._state_path(sid, "alias"), {"root": root})
            if create and sid not in self._alias and sid not in self._sessions:
                root = (_read_json(self._state_path(sid, "alias")) or {}).get("root")
                if isinstance(root, str) and root and root != sid:
                    self._alias[sid] = root
            sid = self._alias.get(sid, sid)
            s = self._sessions.get(sid)
            if s is None and create:
                s = self._sessions[sid] = _Session(sid)
                self._load_segment(s)
            return s

    def _existing(self, sid):
        """The tracked logical session for `sid`, or None (never creates one)."""
        with self._guard:
            return self._sessions.get(self._alias.get(sid, sid))

    def resolve_cwd(self, strict=False):
        """Where this session's commands run: Hermes's own resolver (session-scoped override,
        then the scoped TERMINAL_CWD, then the launch dir), read in the caller's context. With
        `strict`, a resolver failure raises instead of falling back to the process's cwd."""
        try:
            from agent.runtime_cwd import resolve_agent_cwd
            cwd = str(resolve_agent_cwd())
        except Exception:
            if strict:
                raise
            cwd = os.environ.get("TERMINAL_CWD") or os.getcwd()
        return os.path.realpath(cwd)

    def _log_path(self, sid):
        return os.path.join(self.root, "hermes-sessions", f"{sid}.jsonl")

    def _state_path(self, sid, kind):
        return os.path.join(self.root, "hermes-sessions", f"{sid}.{kind}.json")

    def _child_log_path(self, sid, child):
        """A subagent's log where upstream's Claude readers look for it:
        `<session log without .jsonl>/subagents/<subagent>.jsonl`."""
        return os.path.join(self.root, "hermes-sessions", sid, "subagents", f"{child}.jsonl")

    # ---- subagents: their tool use and cost belong to the session that spawned them ------
    def subagent_start(self, parent_session_id="", child_session_id="", **_):
        if not parent_session_id or not child_session_id:
            return
        with self._guard:
            root = self._subagents.get(parent_session_id) or self._alias.get(
                parent_session_id, parent_session_id)
            if root in self._sessions:
                self._subagents[str(child_session_id)] = root

    def _parent_of(self, sid):
        """The tracked session a subagent session belongs to, or None."""
        if not sid:
            return None
        with self._guard:
            p = self._subagents.get(sid)
            s = self._sessions.get(p) if p else None
        if s is None or s.skip or not s.started or not self._enabled():
            return None
        return s

    def _child_record(self, s, child, tool_name, args, result, tool_call_id, ok):
        """One subagent tool call in its log (once per call): pruning's used-tool evidence.
        Subagents run no mechanism (upstream skips them), so nothing else happens."""
        from .host import SessionLog, sanitize, tool_response
        from tokeneconomizer.plugin.hosts import response_text
        key = (child, tool_call_id, tool_name)
        with s.lock:
            if tool_call_id and key in s.child_calls:
                return
            if tool_call_id:
                s.child_calls.add(key)
        resp = sanitize(tool_response(result))
        args = sanitize(args if isinstance(args, dict) else {})
        with s.lock:        # never into a segment a switch already ended
            SessionLog(self._child_log_path(s.useg, child)).tool_call(
                tool_call_id, tool_name, args, response_text(resp), ok, s.cwd)

    def _enabled(self):
        return self.active and os.environ.get("TE_DISABLE", "") not in ("1", "true", "yes", "on")

    # ---- calling upstream --------------------------------------------------------------
    def _call(self, s, event, **fields):
        """Run one upstream hook event for session `s`; returns its output dict."""
        from tokeneconomizer.plugin import hook
        env = self.env()
        try:
            with s.lock:    # the segment is read under the lock a switch holds
                payload = dict(fields, hook_event_name=event, session_id=s.useg, cwd=s.cwd,
                               transcript_path=self._log_path(s.useg))
                with self.deny_scope():
                    return hook.run(payload, env, host_hint="hermes",
                                    context_cls=self.ctx_class()) or {}
        except Exception:
            hook._record_error(env)
            return {}
        finally:
            # As upstream's hook entry does after every event: Hermes's umask, not
            # te_bootstrap's 077, created the root, so keep it private to its owner here.
            from tokeneconomizer.plugin.paths import secure_root
            secure_root(env)

    @staticmethod
    def _texts(out):
        ctx = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""
        return ctx, (out or {}).get("systemMessage") or ""

    def _session_log(self, s):
        from .host import SessionLog
        return SessionLog(self._log_path(s.useg))

    # ---- Hermes hooks ------------------------------------------------------------------
    def pre_llm_call(self, session_id="", turn_id="", user_message="", conversation_history=None,
                     is_first_turn=False, platform="", **_):
        if not self._enabled() or not session_id or platform == "subagent":
            return None
        from .host import sanitize
        s = self._logical(session_id, turn_id)
        if s.skip:
            return None
        parts = []
        if os.environ.get(CAPTURE_ENV):
            # Capture child: only the request's tools are wanted. No upstream event, so no fold,
            # no notice and no state write; llm_request records the request and exits.
            s.cwd, s.started = s.cwd or self.resolve_cwd(), True
            return None
        if not s.started:
            here = self.resolve_cwd()
            s.cwd, s.dir = s.cwd or here, _terminal_cwd() or here
            s.local = _local_backend()
            if not self._config(s).enabled:
                # Switched off (`te config set enabled=false`): this session is left alone
                # entirely, from its first turn to its last.
                s.skip = True
                return None
            s.started = True
            source = "startup" if is_first_turn else "resume"
            out = self._call(s, "SessionStart", source=source)
            parts += self._delivery(out, "session-start", s)
            self._load_block(s)
            if source == "resume" and not self._block_in(conversation_history, s):
                # Replay lost it (e.g. a conversation compacted before this process).
                parts += self._delivery(self._call(s, "SessionStart", source="compact"),
                                        "session-start", s)
        elif self._injected(s) and (s.reinject
                                    or not self._block_in(conversation_history, s)):
            s.reinject = False
            parts += self._delivery(self._call(s, "SessionStart", source="compact"),
                                    "session-start", s)
        self._load_block(s)
        prompt = sanitize(str(user_message or ""))
        with s.lock:
            s.prompt = prompt
            self._session_log(s).prompt(prompt, s.cwd)
        out = self._call(s, "UserPromptSubmit", prompt_id=turn_id, prompt=prompt)
        parts += self._delivery(out, "prompt", s)
        return {"context": "\n\n".join(parts)} if parts else None

    def _delivery(self, out, kind, s=None):
        from .host import screen_block
        ctx, notice = self._texts(out)
        if kind == "session-start":
            ctx = screen_block(ctx)     # cached or pre-policy paid lines are rechecked here
        parts = [_wrap(ctx, kind)] if ctx else []
        if parts and kind == "session-start" and s is not None:
            s.facts_block = parts[0]
        if notice:
            self.log.info("TokenEconomizer: %s", notice)
            if s is not None:
                # Upstream's session-start notices (facts added, tools applied or proposed,
                # repository map added): shown with this turn's reply, see transform_llm_output.
                with s.lock:
                    s.notices += [ln for ln in notice.splitlines() if ln.strip()]
        return parts

    def _load_block(self, s):
        """The current segment's facts block from its upstream record when this process did
        not deliver it (a restart)."""
        if s.facts_block is None:
            rec = self._session_record(s) or {}
            from .host import screen_block
            text = screen_block(rec.get("injected_text") or "")
            if text:
                s.facts_block = _wrap(text.rstrip("\n"), "session-start")

    def _block_in(self, obj, s, history=True):
        """Whether `obj` still carries this segment's facts block. Segment 1: any facts block
        (upstream's rule). Later segments: their own block's text, raw or as `_attach` put it in
        JSON, or (in history) the row of the call that carried it, which `_advice_restored`
        repairs; the first segment's block surviving in history is not this one."""
        if s.seg_n == 1 or not s.facts_block:
            return _has_mark(obj)
        if _has_mark(obj, (s.facts_block, _in_json(s.facts_block)[1:-1])):
            return True
        return history and bool(s.seg_call) and any(
            isinstance(m, dict) and m.get("tool_call_id") == s.seg_call for m in obj or ())

    def _injected(self, s):
        rec = self._session_record(s)
        return bool(rec and rec.get("injected_chars"))

    def _session_record(self, s):
        from tokeneconomizer.plugin import repos
        from tokeneconomizer.plugin.paths import repo_state_dir
        try:
            repo = repos.pinned(self.root, "hermes", s.useg)
            if repo is None:
                return None
            path = os.path.join(repo_state_dir(repo, "hermes", self.env()), "sessions",
                                f"{s.useg}.json")
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _has_block(history):
        return any(isinstance(m, dict) and (_has_mark(m.get("content"))
                                            or _has_mark(m.get("api_content")))
                   for m in history or ())

    # ---- tool observation: one record per executed call ------------------------------
    def _tracked(self, session_id, turn_id):
        s = self._logical(session_id, turn_id, create=False) if session_id else None
        if s is None or s.skip or not s.started or not self._enabled():
            return None
        return s

    def _observe(self, s, tool_name, args, result, tool_call_id, turn_id, ok, upstream=True):
        """Record one call in the session log (pruning's used-tool evidence) and, for an
        executed call, run upstream's PostToolUse on it. Returns its context text."""
        from .host import sanitize, tool_response
        from tokeneconomizer.plugin.hosts import response_text
        args = sanitize(args if isinstance(args, dict) else {})
        resp = sanitize(tool_response(result))
        self._session_log(s).tool_call(tool_call_id, tool_name, args, response_text(resp),
                                       ok, s.cwd)
        if not upstream:
            return ""
        out = self._call(s, "PostToolUse", prompt_id=turn_id, tool_name=tool_name,
                         tool_input=args, tool_response=resp, tool_use_id=tool_call_id)
        ctx, notice = self._texts(out)
        if notice:
            self.log.info("TokenEconomizer: %s", notice)
            s.notices += [ln for ln in notice.splitlines() if ln.strip()]
        return ctx

    def post_tool_call(self, tool_name="", args=None, result=None, session_id="",
                       tool_call_id="", turn_id="", status="", error_type=None, **_):
        """The executor's terminal observer, once per call on every route. Records the calls
        no transform will report (cancelled, timed out, invalid) and, on routes where it runs
        before the transform, the executed call itself; its advice then waits for that same
        call's transform."""
        if status in _NOT_RUN or not tool_name:
            return              # a refused call never ran (no PostToolUse on Claude Code either)
        parent = self._parent_of(session_id)
        if parent is not None:
            self._child_record(parent, session_id, tool_name, args, result, tool_call_id,
                               status not in ("error",) + _STARTED_NOT_FINISHED)
            return
        s = self._tracked(session_id, turn_id)
        if s is None:
            return
        key = (turn_id, tool_call_id, tool_name)
        with s.lock:
            if tool_call_id and (key in s.calls or key in s.done):
                return          # the transform already recorded it
            not_finished = status in _STARTED_NOT_FINISHED or error_type in _BAD_CALL
            if not_finished:
                self._observe(s, tool_name, args, result, tool_call_id, turn_id, False,
                              upstream=False)
                if tool_call_id:
                    s.calls[key] = {"advice": []}
                return
            if not tool_call_id:
                return          # no identity to merge on: the transform records it
            facts = self._maybe_switch(s, tool_name, args, result, tool_call_id, turn_id)
            ctx = self._observe(s, tool_name, args, result, tool_call_id, turn_id,
                                status != "error")
            s.calls[key] = {"advice": [ctx] if ctx else [], "facts": facts}

    def transform_tool_result(self, tool_name="", args=None, result=None, session_id="",
                              tool_call_id="", turn_id="", status="", **_):
        parent = self._parent_of(session_id)
        if parent is not None and tool_name:
            self._child_record(parent, session_id, tool_name, args, result, tool_call_id,
                               status != "error")
            return None
        s = self._tracked(session_id, turn_id)
        if s is None or not tool_name:
            return None
        key = (turn_id, tool_call_id, tool_name)
        with s.lock:
            entry = s.calls.get(key) if tool_call_id else None
            if entry is None and tool_call_id and key in s.done:
                return None     # recorded in an earlier turn; its advice ended with that turn
            if entry is None:
                facts = self._maybe_switch(s, tool_name, args, result, tool_call_id, turn_id)
                ctx = self._observe(s, tool_name, args, result, tool_call_id, turn_id,
                                    status != "error")
                entry = {"advice": [ctx] if ctx else [], "facts": facts}
                if tool_call_id:
                    s.calls[key] = entry
            texts, entry["advice"] = entry["advice"], []
            facts, entry["facts"] = entry.get("facts") or "", ""
            if _untrusted(tool_name):
                if texts:
                    s.held.setdefault(turn_id, []).extend(texts)
                return None
            if texts and not isinstance(result, str):
                # A multimodal result cannot carry text: the advice is dropped, not moved to
                # another call's result.
                s.undelivered += len(texts)
                self.log.info("TokenEconomizer: advice for %s not delivered (non-text result)",
                              tool_name)
                return None
            if isinstance(result, str):
                texts = s.held.pop(turn_id, []) + texts
        if not texts and not facts:
            return None
        block = "\n\n".join(([facts] if facts else []) + [_wrap(t, "tool") for t in texts])
        if tool_call_id:
            with s.lock:
                s.attached[tool_call_id] = block
                while len(s.attached) > ATTACHED_MAX:      # oldest first (insertion order)
                    s.attached.pop(next(iter(s.attached)))
        return _attach(result, block)

    def tool_execution(self, tool_name="", args=None, next_call=None, session_id="",
                       tool_call_id="", turn_id="", **_):
        """Tool execution middleware, for the one result shape `transform_tool_result` cannot
        replace: a multimodal envelope (`{"_multimodal": True, "content": [...]}`, e.g. an
        image). Its call is observed here, and its advice rides that same result as a text
        part (and in `text_summary`, which text-only providers get), as Claude Code adds a
        hook's context to whatever the tool returned."""
        result = next_call(args)
        if not (isinstance(result, dict) and result.get("_multimodal")
                and isinstance(result.get("content"), list)) or not tool_name:
            return result
        s = self._tracked(session_id, turn_id)
        if s is None:
            return result
        key = (turn_id, tool_call_id, tool_name)
        with s.lock:
            entry = s.calls.get(key) if tool_call_id else None
            if entry is None:
                ctx = self._observe(s, tool_name, args, result.get("text_summary") or "",
                                    tool_call_id, turn_id, True)
                entry = {"advice": [ctx] if ctx else []}
                if tool_call_id:
                    s.calls[key] = entry
            texts, entry["advice"] = entry["advice"], []
            if _untrusted(tool_name):
                if texts:
                    s.held.setdefault(turn_id, []).extend(texts)
                return result
            texts = s.held.pop(turn_id, []) + texts
        if not texts:
            return result
        text = "\n\n".join(_wrap(t, "tool") for t in texts)
        out = dict(result, content=list(result["content"]) + [{"type": "text", "text": text}])
        if isinstance(result.get("text_summary"), str):
            out["text_summary"] = result["text_summary"] + "\n\n" + text
        return out

    def transform_llm_output(self, response_text="", session_id="", turn_id="", platform="",
                             **_):
        """The turn's guard notices (redirected / still stuck), where the person reads: the
        final reply. Claude Code shows them as hook `systemMessage`s; Hermes gives plugins no
        such channel, so a notice the model did not already pass on is appended. Another
        plugin's earlier string return wins (first-wins transform); notices are then lost."""
        if not session_id or platform == "subagent" or not isinstance(response_text, str):
            return None
        s = self._existing(session_id)
        if s is None:
            return None
        with s.lock:
            notices, s.notices = s.notices, []
        missing = [n for n in dict.fromkeys(notices) if n not in response_text]
        if not missing:
            return None
        return response_text.rstrip() + "\n\n" + "\n".join(missing)

    def post_api_request(self, session_id="", turn_id="", api_request_id="", model="",
                         usage=None, platform="", **_):
        if not session_id:
            return
        parent = self._parent_of(session_id)
        if parent is not None:
            # A subagent's requests are its session's cost (Claude Code: subagent transcripts).
            from .host import SessionLog
            with parent.lock:
                SessionLog(self._child_log_path(parent.useg, session_id)).usage(
                    api_request_id or None, model, usage)
            return
        if platform == "subagent":
            return
        s = self._logical(session_id, turn_id, create=False)
        if s is None or s.skip or not s.started:
            return
        with s.lock:        # the switch holds it: a racing request lands in one segment
            self._session_log(s).usage(api_request_id or None, model, usage)

    def on_session_end(self, session_id="", turn_id="", completed=False, failed=False,
                       interrupted=False, platform="", **_):
        if not session_id or platform == "subagent":
            return
        s = self._logical(session_id, turn_id, create=False)
        if s is None or s.skip or not s.started:
            return
        if completed and not failed and not interrupted:
            self._call(s, "Stop", prompt_id=turn_id, stop_hook_active=False)
        # Otherwise the turn is left open; upstream closes it abnormally at the next prompt.
        with s.lock:
            # Advice belongs to its own call: anything its result never carried ends here.
            s.undelivered += sum(len(e["advice"]) for e in s.calls.values()) \
                + sum(map(len, s.held.values()))
            s.done.update(dict.fromkeys(k for k in s.calls if k[1]))
            while len(s.done) > ATTACHED_MAX:   # oldest first; past it a late duplicate is
                s.done.pop(next(iter(s.done)))  # recorded twice, never switched (s.ended)
            if turn_id:
                s.ended.add(turn_id)
            s.calls.clear()
            s.held.clear()
        if _oneshot():
            # `hermes -z` runs one turn and leaves through os._exit: no on_session_finalize, no
            # atexit. Its only turn ending is the session ending; fold now, bounded below the
            # host's hook timeout (30 s). A fold cut short is redone by a later session start.
            self.on_session_finalize(session_id=session_id, reason="oneshot")
            self.drain(min(25.0, _exit_wait()))

    def on_session_finalize(self, session_id="", reason="", **_):
        if not session_id:
            return
        with self._guard:
            sid = self._alias.get(session_id, session_id)
            s = self._sessions.get(sid)
        if s is None or s.skip or not s.started:
            return
        with s.lock:        # one transition against a switch: end whichever segment is current
            if s.closed:
                return
            s.closed = True
            self._call(s, "SessionEnd", reason=reason or "exit")
        with self._guard:
            self._sessions.pop(sid, None)
            for k in [k for k, v in self._alias.items() if v == sid]:
                self._alias.pop(k)
            for k in [k for k, v in self._subagents.items() if v == sid]:
                self._subagents.pop(k)

    # ---- request middleware: catalog, pruning filter, facts repair ---------------------
    def llm_request(self, request=None, session_id="", turn_id="", api_mode="", platform="",
                    **_):
        if os.environ.get(CAPTURE_ENV):
            self._capture_and_exit(request, session_id)
        if not isinstance(request, dict) or not session_id:
            return None
        parent = self._parent_of(session_id)
        if parent is not None:
            # A deny rule on Claude Code holds for the session's subagents too.
            return self._child_request(parent, request)
        if platform == "subagent":
            return None
        s = self._logical(session_id, turn_id, create=False)
        if s is None or s.skip or not s.started:
            return None
        with s.lock:
            attached = dict(s.attached)
        readvised = _advice_restored(request, attached)
        if readvised is not None:
            request = readvised
        # After restoration: a restored segment facts block is not repaired a second time.
        repaired = self._repair_facts(s, request, api_mode)
        if repaired is not None:
            request = repaired
        tools = request.get("tools")
        if not isinstance(tools, list):
            return None if repaired is None and readvised is None else {
                "request": request, "source": "tokeneconomizer",
                "reason": "facts block or tool advice restored"}
        names = [n for n in (_tool_name(t) for t in tools) if n]
        search = next((t for t in tools if _tool_name(t) == "tool_search"), None)
        deferred = _deferred_names(_description(search)) if search is not None else []
        offered = set(names) | set(deferred)
        with s.lock:
            s.deferred = tuple(deferred)
            if s.frozen_deny is None and os.environ.get(MEASURE_ENV) is not None:
                # `te toolprune measure` child (drivers.measure): exactly the tools it names.
                s.frozen_deny = frozenset(
                    t for t in os.environ[MEASURE_ENV].split(",") if t.strip())
            if s.frozen_deny is None:
                s.frozen_deny = (frozenset(self._deny_list(s))
                                 if self._enabled() and self._config(s).enabled else frozenset())
            deny = s.frozen_deny
            if offered != s.catalog:
                # What this session was offered, including what the mask holds back from it:
                # a held-back tool is not evidence of non-use, but stays in the catalog.
                s.catalog = offered
                self._session_log(s).catalog(offered | deny)
        self._record_catalog(offered)
        kept, dropped, unlisted = _prune(tools, deny, search, deferred)
        searched = _searches_filtered(request, deny, deferred)
        if searched is not None:
            request = searched
        if not dropped and not unlisted:
            if repaired is None and searched is None and readvised is None:
                return None
            return {"request": request, "source": "tokeneconomizer",
                    "reason": "facts block, tool_search results or tool advice repaired"}
        return {"request": dict(request, tools=kept), "source": "tokeneconomizer",
                "reason": f"toolprune: {dropped} tool(s) and {unlisted} deferred listing(s) "
                          f"omitted"}

    def _child_request(self, parent, request):
        deny = parent.frozen_deny
        tools = request.get("tools")
        if not deny or not isinstance(tools, list):
            return None
        search = next((t for t in tools if _tool_name(t) == "tool_search"), None)
        deferred = _deferred_names(_description(search)) if search is not None else []
        kept, dropped, unlisted = _prune(tools, deny, search, deferred)
        searched = _searches_filtered(request, deny, deferred)
        if not dropped and not unlisted:
            return None if searched is None else {
                "request": searched, "source": "tokeneconomizer",
                "reason": "toolprune (subagent): tool_search results filtered"}
        return {"request": dict(searched or request, tools=kept), "source": "tokeneconomizer",
                "reason": f"toolprune (subagent): {dropped} tool(s) and {unlisted} deferred "
                          f"listing(s) omitted"}

    def _capture_and_exit(self, request, session_id):
        """`te toolprune measure/refresh-catalog` child (drivers.capture): write the first
        request's tools (before and after this checkout's deny list), then end the process,
        whatever the request holds and whether or not the write worked: never a provider send."""
        try:
            from .drivers import write_capture
            tools = request.get("tools") if isinstance(request, dict) else None
            tools = tools if isinstance(tools, list) else []
            s = self._logical(session_id, create=False) if session_id else None
            deny = frozenset(self._deny_list(s)) if s is not None and s.cwd else frozenset()
            search = next((t for t in tools if _tool_name(t) == "tool_search"), None)
            deferred = _deferred_names(_description(search)) if search is not None else []
            write_capture(os.environ[CAPTURE_ENV], tools,
                          _prune(tools, deny, search, deferred)[0], deferred, deny)
        finally:
            os._exit(0)

    def pre_tool_call(self, tool_name="", args=None, session_id="", **_):
        """A denied deferred tool is gone from the listing, but `tool_search` can still find
        it; refuse describing or calling it, as a deny rule makes the tool absent on Claude
        Code. Hermes unwraps a local `tool_call` before this hook, so the concrete name
        arrives here; `tool_describe` arrives as the bridge with `names`."""
        s = self._existing(session_id) or self._parent_of(session_id)
        if s is None or not s.frozen_deny:
            return None
        args = args or {}
        if tool_name == "tool_describe":
            names = args.get("names")
            names = [names] if isinstance(names, str) else list(names or [])
            name = next((n for n in names if n in s.frozen_deny), None)
        elif tool_name == "tool_call":
            # A batch that reached this hook still wrapped: refuse it if any entry is denied.
            calls = args.get("calls") if isinstance(args.get("calls"), list) else [args]
            name = next((c.get("name") for c in calls
                         if isinstance(c, dict) and c.get("name") in s.frozen_deny), None)
        else:
            name = tool_name
        if name and name in s.frozen_deny:
            return {"action": "block", "message": (
                f"{name} is switched off for this repository by TokenEconomizer (unused in its "
                f"recent sessions). `/te toolprune restore {name}` gives it back from the next "
                f"session.")}
        return None

    def _repair_facts(self, s, request, api_mode):
        """A compaction between `pre_llm_call` and the request (Hermes's pre-API and mid-turn
        compaction) can summarize away the row that carried the facts block. Put it back into
        this turn's user message for the request, and have the next prompt persist it again,
        as upstream re-injects at SessionStart(compact). Chat-completions, Anthropic messages
        and native Responses (`input`) shapes; never into system instructions."""
        block = s.facts_block
        if not block:
            return None
        if api_mode in ("chat_completions", "anthropic_messages"):
            key = "messages"
        elif api_mode == "codex_responses":
            key = "input"
        else:
            return None
        items = request.get(key)
        if not isinstance(items, list) or self._block_in(items, s, history=False):
            return None
        idx = next((i for i in range(len(items) - 1, -1, -1) if _is_prompt_row(items[i])), None)
        if idx is None:
            return None
        items = list(items)
        row = dict(items[idx])
        c = row.get("content")
        part = "input_text" if key == "input" else "text"
        if isinstance(c, str):
            row["content"] = c + "\n\n" + block
        else:
            row["content"] = list(c) + [{"type": part, "text": block}]
        items[idx] = row
        s.reinject = True
        self.log.info("TokenEconomizer: facts block restored after compaction (session %s)", s.sid)
        return dict(request, **{key: items})

    # ---- segments: a session continues as a new upstream session per repository ---------
    def _call_dir(self, s, args, result):
        """Where a terminal call ran: the directory Hermes echoed (where the command finished,
        echoed only when it changed), else its `workdir`, else the tracked directory. Only an
        echo on a call without `workdir` moves the tracked directory, as Hermes records it."""
        echoed = None
        if isinstance(result, str) and '"cwd"' in result:
            try:
                data = json.loads(result)
            except ValueError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("cwd"), str) and data["cwd"]:
                echoed = data["cwd"]
        workdir = args.get("workdir") if isinstance(args, dict) else None
        workdir = workdir if isinstance(workdir, str) and workdir.strip() else None
        base = s.dir or s.cwd
        if echoed and not workdir:
            s.dir = echoed
        return os.path.realpath(os.path.join(base, os.path.expanduser(echoed or workdir or base)))

    def _repo_of(self, s, d):
        """Read fresh each call (local metadata, no git process): a folder can become a
        repository mid-session."""
        from tokeneconomizer.plugin.paths import repo_identity
        r = repo_identity(d, self.env())
        return r.how, r.key

    def _maybe_switch(self, s, tool_name, args, result, tool_call_id, turn_id):
        """The facts (and prompt) context of a new segment when this terminal call ran in a
        different git repository than the current segment's; else "". Caller holds s.lock."""
        if tool_name != "terminal" or not s.local or s.closed or os.environ.get(CAPTURE_ENV):
            return ""
        if turn_id and turn_id in s.ended:
            return ""           # a late result from an ended turn describes a past directory
        from tokeneconomizer.plugin.paths import BY_PATH, BY_REMOTE
        try:
            d = self._call_dir(s, args, result)
            if not os.path.isdir(d):
                return ""
            if s.seg_key is None:
                s.seg_key = self._repo_of(s, os.path.realpath(s.cwd))[1]
            how, key = self._repo_of(s, d)
        except Exception:
            return ""
        if how not in (BY_REMOTE, BY_PATH) or key == s.seg_key:
            return ""
        return self._switch(s, d, key, turn_id, tool_call_id)

    def _switch(self, s, d, key, turn_id, tool_call_id):
        old = s.cwd
        s.cwd = d
        enabled = self._config(s).enabled
        s.cwd = old
        if not enabled:
            return ""           # switched off in the destination: stay where we are
        self._call(s, "SessionEnd", reason="project-switch")
        s.seg_n += 1
        s.useg, s.cwd, s.seg_key = f"{s.sid}~{s.seg_n}", d, key
        s.facts_block, s.reinject, s.seg_call = None, False, tool_call_id
        log = self._session_log(s)
        log.prompt(s.prompt, s.cwd)
        deny = s.frozen_deny or frozenset()
        if s.catalog is not None:
            log.catalog(s.catalog | deny)
        log.mask(deny)
        parts = self._delivery(self._call(s, "SessionStart", source="startup"),
                               "session-start", s)
        parts += self._delivery(self._call(s, "UserPromptSubmit", prompt_id=turn_id,
                                           prompt=s.prompt), "prompt", s)
        self._save_segment(s)
        self.log.info("TokenEconomizer: session %s continues in %s as %s", s.sid, d, s.useg)
        return "\n\n".join(parts)

    def _save_segment(self, s):
        _write_json(self._state_path(s.sid, "segment"), {
            "n": s.seg_n, "useg": s.useg, "cwd": s.cwd, "key": s.seg_key, "call": s.seg_call,
            "frozen_deny": None if s.frozen_deny is None else sorted(s.frozen_deny)})

    def _load_segment(self, s):
        """A session seen first after a restart continues its saved segment."""
        d = _read_json(self._state_path(s.sid, "segment"))
        if not isinstance(d, dict) or not isinstance(d.get("useg"), str):
            return
        s.seg_n, s.useg, s.cwd = int(d.get("n") or 1), d["useg"], d.get("cwd")
        s.seg_key, s.seg_call = d.get("key"), d.get("call")
        if isinstance(d.get("frozen_deny"), list):
            s.frozen_deny = frozenset(d["frozen_deny"])

    def _record_catalog(self, names):
        """The profile-wide snapshot `te status`/`doctor` show; pruning's evidence uses each
        session's own catalog (its session log), not this union."""
        from tokeneconomizer.plugin.adapter import catalog_path
        path = catalog_path(self.root, "hermes")
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh).get("tools") or []
        except (OSError, ValueError):
            old = []
        merged = sorted(set(old) | set(names))
        if merged == sorted(old):
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"tools": merged, "source": "hermes llm_request", "at": time.time()}, fh)
        os.replace(tmp, path)

    def _config(self, s):
        """Upstream's merged config (defaults, global, repository file, environment)."""
        from tokeneconomizer.plugin.config import load_config
        from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
        env = self.env()
        repo = repo_identity(s.cwd, env)
        return load_config(self.root, repo_dir=os.path.dirname(repo_state_dir(repo, "hermes", env)),
                           env=env)

    def _deny_list(self, s):
        """The checkout's deny list as the session starts: the plugin's own entries and any the
        person added, read from the per-checkout file (`te toolprune apply` writes it)."""
        from tokeneconomizer.plugin.paths import repo_identity
        try:
            repo = repo_identity(s.cwd, self.env())
            path = os.path.join(repo.root, self.deny_rel)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            lst = data.get(seams.DENY_KEY) if isinstance(data, dict) else None
            return [t for t in lst or () if isinstance(t, str)
                    and t not in seams.HERMES_PROTECTED]
        except (OSError, ValueError):
            return []

    # ---- background jobs ---------------------------------------------------------------
    def spawn(self, ctx, argv, log_name):
        """Upstream's detached jobs. `fold` and `judge` need the profile's LLM facade, so they
        run on this runtime's worker, bound to the generation they were queued under; with the
        runtime off or unloaded they are not started (a fold stays pending for a later session
        start). The rest stay subprocesses (no model calls), owned by this runtime."""
        if argv and argv[0] in ("fold", "judge"):
            if not self.live(self.generation) or not ctx.config.enabled:
                return False
            self._ensure_worker(argv[0])
            job = list(argv) + ["--cwd", ctx.repo.root]
            self._jobs[argv[0]].put((self.generation, contextvars.copy_context(), job, log_name,
                                     None))
            return True
        if not self.active:
            return False
        proc = seams.subprocess_spawn(ctx, argv, log_name)
        with self._guard:
            self._children = [p for p in self._children if p.poll() is None] + [proc]
        return True

    def run_fold(self, cwd, session_ids=(), timeout=None, verbose=False):
        """`/te fold` and `hermes te fold`: fold now, on the bound worker (the paid proposer
        included). Returns (exit code, report)."""
        if not self.live(self.generation):
            return 1, "te fold: TokenEconomizer is off for this profile"
        done = threading.Event()
        result = {}
        argv = ["fold"] + [a for sid in session_ids for a in ("--session", sid)] + ["--cwd", cwd]
        self._ensure_worker("fold")
        self._jobs["fold"].put((self.generation, contextvars.copy_context(), argv, "fold.log",
                        (done, result)))
        if not done.wait(timeout or 600):
            return 124, "te fold: still running (it finishes in the background)"
        recs = result.get("records") or []
        lines = [json.dumps(r, default=str) for r in recs] if verbose else []
        lines.append(f"folded {len([r for r in recs if r.get('kind') == 'session'])} session(s)")
        if result.get("charged"):
            lines.append(f"charged {result['charged']} resumed session(s)' later requests")
        if result.get("deferred"):
            lines.append("deferred: the optimizer could not run now; left pending")
        if result.get("error"):
            lines.append(f"error: {result['error']}")
        return (1 if result.get("error") else 0), "\n".join(lines)

    def _ensure_worker(self, lane):
        with self._guard:
            w = self._workers.get(lane)
            if w is None or not w.is_alive():
                w = self._workers[lane] = threading.Thread(
                    target=self._work, args=(lane,), name=f"tokeneconomizer-{lane}", daemon=True)
                w.start()

    def _work(self, lane):
        while True:
            job = self._jobs[lane].get()
            if job is None:
                return
            gen, cvctx, argv, log_name, reply = job
            if argv[0] == "_signal":
                argv[1].set()
                continue
            if not self.live(gen):
                if reply:
                    reply[1]["deferred"] = True
                    reply[0].set()
                continue
            cvctx.run(self._run_job, gen, argv, log_name, reply)

    @contextlib.contextmanager
    def deny_scope(self):
        """Upstream code run inside resolves `hermes` to this profile's pruning file."""
        var = seams.deny_var()
        token = var.set(self.deny_rel)
        try:
            yield
        finally:
            var.reset(token)

    def _run_job(self, gen, argv, log_name, reply=None):
        with self.deny_scope():
            return self._run_job_scoped(gen, argv, log_name, reply)

    def _run_job_scoped(self, gen, argv, log_name, reply=None):
        from tokeneconomizer.plugin import fold, hook, judge
        from . import host
        from .runner import Defer
        env = self.env()
        opt = _argv_opts(argv)
        out = reply[1] if reply else {}
        try:
            sids = opt.get("--session") or []
            cwd = (opt.get("--cwd") or [None])[-1]
            if not cwd:
                raise RuntimeError("job without a repository")
            ctx = self.ctx_class(gen).build(host.HermesHost(), cwd, env,
                                            session_id=(sids or [None])[0])
            if not ctx.config.enabled:
                out["deferred"] = True      # switched off since it was queued: left pending
                return
            if argv[0] == "fold":
                recs = out.setdefault("records", [])
                for sid in sids or fold.pending_sessions(ctx):
                    if not self.live(gen):
                        out["deferred"] = True
                        return
                    rec = fold.fold_session(ctx, sid)
                    if rec is not None:
                        recs.append(rec)
                        if rec.get("kind") == "usage_delta":
                            out["charged"] = out.get("charged", 0) + 1
            else:
                judge.run_job(ctx, sids[0], opt["--job"][0])
        except Defer:
            out["deferred"] = True          # the fold stays pending; nothing was recorded
        except Exception as e:
            out["error"] = type(e).__name__
            hook._record_error(env)
        finally:
            if reply:
                reply[0].set()

    def shutdown(self):
        """Plugin unload: no new work, queued jobs dropped, in-flight results discarded (their
        spend still charged), this runtime's detached subprocesses terminated. An in-flight
        `ctx.llm` call cannot be cancelled; it ends at its timeout."""
        self.active = False
        self.generation += 1
        for q in self._jobs.values():
            q.put(None)
        with self._guard:
            children, self._children = self._children, []
        for proc in children:
            if proc.poll() is None:
                proc.terminate()

    def drain_at_exit(self):
        """Process exit (a one-shot `hermes -z` ends right after finalizing): give queued folds
        and judge jobs a bounded chance to finish (`TE_HERMES_EXIT_WAIT`, default 120 s).
        Anything still running is abandoned; the next session start folds a session that
        missed its fold, as upstream does. Sessions this process ran that Hermes never
        finalized (one-shot `-z` exits without `on_session_finalize`) end here first."""
        if not self.active:
            return True
        with self._guard:
            open_sids = [sid for sid, s in self._sessions.items() if s.started and not s.skip]
        for sid in open_sids:
            self.on_session_finalize(session_id=sid, reason="process_exit")
        if not any(w.is_alive() for w in self._workers.values()):
            return True
        return self.drain(_exit_wait())

    def drain(self, timeout=None):
        """Wait until queued jobs have run on every lane (tests and controls)."""
        end = None if timeout is None else time.monotonic() + timeout
        signals = []
        for lane, q in self._jobs.items():
            done = threading.Event()
            self._ensure_worker(lane)
            q.put((self.generation, contextvars.copy_context(), ["_signal", done], "", None))
            signals.append(done)
        return all(d.wait(None if end is None else max(0.0, end - time.monotonic()))
                   for d in signals)


def _prune(tools, deny, search, deferred):
    """(tools to send, schemas dropped, deferred listings removed): `tools` without the denied
    ones, the prompt-cache marker moved off a dropped tool, and denied names cut from the
    `tool_search` listing. No side effects: the request path and capture both use it."""
    kept = [t for t in tools if _tool_name(t) not in deny]
    dropped = len(tools) - len(kept)
    if dropped and kept and any(isinstance(t, dict) and "cache_control" in t for t in tools) \
            and not any(isinstance(t, dict) and "cache_control" in t for t in kept):
        # The prompt-cache breakpoint rode a dropped tool: move it to the last kept one.
        marker = next(t["cache_control"] for t in tools
                      if isinstance(t, dict) and "cache_control" in t)
        kept[-1] = dict(kept[-1], cache_control=marker)
    unlisted = 0
    if search is not None and deny & set(deferred):
        desc, unlisted = _filter_listing(_description(search), deny)
        kept = [_with_description(t, desc) if t is search else t for t in kept]
    return kept, dropped, unlisted


def _search_filtered(tool_name, result, deny, deferred=()):
    """A `tool_search` result (JSON: `results[].matches`, `tools` by name, `total_available`)
    without the denied tools, or None when there is nothing to remove."""
    if tool_name != "tool_search" or not deny or not isinstance(result, str):
        return None
    try:
        data = json.loads(result)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return None
    tools = data.get("tools") if isinstance(data.get("tools"), dict) else {}
    gone = {n for n in tools if n in deny}
    for group in data["results"]:
        if isinstance(group, dict) and isinstance(group.get("matches"), list):
            gone.update(n for n in group["matches"] if n in deny)
    if not gone:
        return None
    for group in data["results"]:
        if isinstance(group, dict) and isinstance(group.get("matches"), list):
            group["matches"] = [n for n in group["matches"] if n not in deny]
    if tools:
        data["tools"] = {n: v for n, v in tools.items() if n not in deny}
    if isinstance(data.get("total_available"), int):
        # The search counted every deferred tool, the denied ones included.
        data["total_available"] = max(0, data["total_available"] - len(
            gone | (set(deny) & set(deferred))))
    return json.dumps(data, ensure_ascii=False)


def _tool_results(request, ids, fn):
    """`request` with `fn(call_id, content)` (new content, or None to keep it) applied to the
    tool results whose call ids are in `ids`: chat completions (`role: tool`), Anthropic
    messages (`tool_result` blocks) and Responses `input` (`function_call_output`). None when
    nothing changed."""
    hit = []

    def apply(cid, c):
        new = fn(cid, c)
        if new is None:
            return c
        hit.append(1)
        return new

    out = {}
    for key in ("messages", "input"):
        items = request.get(key)
        if not isinstance(items, list):
            continue
        rows = []
        for it in items:
            if isinstance(it, dict):
                if it.get("role") == "tool" and it.get("tool_call_id") in ids:
                    it = dict(it, content=apply(it["tool_call_id"], it.get("content")))
                elif it.get("type") == "function_call_output" and it.get("call_id") in ids:
                    it = dict(it, output=apply(it["call_id"], it.get("output")))
                elif isinstance(it.get("content"), list):
                    it = dict(it, content=[
                        dict(b, content=apply(b["tool_use_id"], b.get("content")))
                        if isinstance(b, dict) and b.get("type") == "tool_result"
                        and b.get("tool_use_id") in ids else b for b in it["content"]])
            rows.append(it)
        out[key] = rows
    return dict(request, **out) if hit else None


def _searches_filtered(request, deny, deferred=()):
    """`request` with the results of its own `tool_search` calls minus the denied tools, or
    None when none changed. Hermes answers tool_search inline, outside `transform_tool_result`,
    so the model-bound copy is where a denied tool is removed. Only a result whose call the
    history names as tool_search is touched; user text or another tool's output that merely
    looks like one is left alone."""
    if not deny:
        return None

    def filt(c):
        if isinstance(c, str):
            return _search_filtered("tool_search", c, deny, deferred)
        if isinstance(c, list):
            parts = [(b, filt(b["text"])) if isinstance(b, dict) and b.get("type") == "text"
                     and isinstance(b.get("text"), str) else (b, None) for b in c]
            if any(f is not None for _, f in parts):
                return [b if f is None else dict(b, text=f) for b, f in parts]
        return None

    ids = set()
    for key in ("messages", "input"):
        for it in request.get(key) if isinstance(request.get(key), list) else ():
            if not isinstance(it, dict):
                continue
            for tc in it.get("tool_calls") or ():
                if isinstance(tc, dict) and (tc.get("function") or {}).get("name") == "tool_search":
                    ids.add(tc.get("id"))
            if it.get("type") == "function_call" and it.get("name") == "tool_search":
                ids.add(it.get("call_id"))
            for b in it["content"] if isinstance(it.get("content"), list) else ():
                if isinstance(b, dict) and b.get("type") == "tool_use" \
                        and b.get("name") == "tool_search":
                    ids.add(b.get("id"))
    ids.discard(None)
    return _tool_results(request, ids, lambda cid, c: filt(c)) if ids else None


def _advice_restored(request, attached):
    """`request` with each advice block our `transform_tool_result` attached put on its own
    call's result where that result lacks it, or None. Hermes keeps the first string a
    `transform_tool_result` hook returns, so another plugin's rewrite (Chisle compressing a
    long output) can win over ours on the very call the guard decided on."""
    if not attached:
        return None

    def put(cid, c):
        block = attached[cid]
        marks = (block, _in_json(block)[1:-1])      # as text, and as `_attach` put it in JSON
        if isinstance(c, str):
            return None if any(m in c for m in marks) else c + "\n\n" + block
        if isinstance(c, list):
            texts = [b.get("text") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            if any(isinstance(t, str) and m in t for t in texts for m in marks):
                return None
            return list(c) + [{"type": "text", "text": block}]
        return None

    return _tool_results(request, set(attached), put)


def _local_backend():
    """Whether this session's commands run on this machine (Hermes's scope-aware read)."""
    try:
        from tools.terminal_scope import terminal_env
        return (terminal_env("TERMINAL_ENV", "local") or "local") == "local"
    except Exception:
        return False


def _terminal_cwd():
    """Hermes's in-process record of where this session's next plain command runs, read with
    the terminal tool's own session key: a plugin reload keeps it, a Hermes restart clears it."""
    try:
        from gateway.session_context import get_session_env
        from tools.terminal_tool import get_session_cwd
        return get_session_cwd(get_session_env("HERMES_SESSION_KEY", ""))
    except Exception:
        return None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _exit_wait():
    try:
        return float(os.environ.get("TE_HERMES_EXIT_WAIT", "120"))
    except ValueError:
        return 120.0


def _oneshot():
    import sys
    return any(a in ("-z", "--oneshot") or a.startswith("--oneshot=") for a in sys.argv[1:])


def _argv_opts(argv):
    out = {}
    for i, a in enumerate(argv):
        if a.startswith("--") and i + 1 < len(argv):
            out.setdefault(a, []).append(argv[i + 1])
    return out


_CATALOG_HEAD = "Deferred tool catalog"
_LISTED = re.compile(r"^- ([^\s:]+)(?::.*)?$")
_GROUP = re.compile(r"^(.*) tools \((\d+)\):$")
_COUNT = re.compile(r"^Search (\d+) additional tools")


def _description(tool):
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    return fn.get("description") or ""


def _with_description(tool, desc):
    if isinstance(tool.get("function"), dict):
        return dict(tool, function=dict(tool["function"], description=desc))
    return dict(tool, description=desc)


def _listing_lines(desc):
    """(head, listing lines) of the `tool_search` description: Hermes embeds one `- name: desc`
    line per deferred tool under `<group> tools (n):` headers (tools/tool_search_catalog.py).
    Tools a listing does not name are unknown, never counted as offered."""
    i = desc.find(_CATALOG_HEAD)
    return (desc, []) if i < 0 else (desc[:i], desc[i:].split("\n"))


def _deferred_names(desc):
    _, lines = _listing_lines(desc)
    return [m.group(1) for m in (_LISTED.match(ln) for ln in lines) if m]


def _filter_listing(desc, deny):
    """The description without denied tools' lines (group and total counts kept true)."""
    head, lines = _listing_lines(desc)
    out, removed, group = [], 0, None
    for ln in lines:
        g, m = _GROUP.match(ln), _LISTED.match(ln)
        if g:
            group = [ln, g.group(1), 0]
            out.append(group)
        elif m and m.group(1) in deny:
            removed += 1
        else:
            if m and group is not None:
                group[2] += 1
            out.append(ln)
    text = []
    for item in out:
        if isinstance(item, list):
            if item[2]:
                text.append(f"{item[1]} tools ({item[2]}):")
        else:
            text.append(item)
    head = _COUNT.sub(lambda c: f"Search {max(0, int(c.group(1)) - removed)} additional tools",
                      head, count=1)
    return head + "\n".join(text), removed


def _has_mark(obj, marks=(FACTS_MARK,)):
    """Whether any string inside a request/message structure carries one of `marks` (default:
    any facts block)."""
    if isinstance(obj, str):
        return any(m in obj for m in marks)
    if isinstance(obj, dict):
        return any(_has_mark(v, marks) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_has_mark(v, marks) for v in obj)
    return False


def _is_prompt_row(m):
    """A user message the person wrote (not a tool result), in Chat, Anthropic or Responses
    shape."""
    if not isinstance(m, dict) or m.get("role") != "user":
        return False
    if m.get("type") not in (None, "message"):
        return False
    c = m.get("content")
    if isinstance(c, str):
        return True
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") in ("text", "input_text") for b in c) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def _tool_name(t):
    if not isinstance(t, dict):
        return None
    fn = t.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return fn["name"]
    return t.get("name") if isinstance(t.get("name"), str) else None
