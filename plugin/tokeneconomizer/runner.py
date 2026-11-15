"""Upstream's optimizer side call (micro-fact proposer, stuck judge) on Hermes's own LLM route.

Upstream runs `claude -p` (or the host's CLI) with no tools; here the same prompt goes through
`ctx.llm.complete`, the host-owned plugin LLM facade: the user's configured provider and auth,
no copied credentials. It fires no hooks or middleware, so the call cannot recurse into this
plugin. The prompt is scrubbed with Hermes's egress redactor first.

Each runner is built for one job by the runtime that owns it (`OptimizerRunner(binding)`), so
two profiles in one process, or a reloaded plugin, never share one. It returns upstream's
`(text, effective_tokens)`; upstream's ledger owns the token count. Every call, and every call
that could not be made, is appended to `<state root>/optimizer.jsonl` (sizes and usage only,
never text).
"""
import json
import os
import re
import time

TASK = "tokeneconomizer_optimizer"
TIMEOUT_S = 180


class Defer(RuntimeError):
    """The call cannot run now (no live binding: switched off, unloaded, superseded). The
    proposer re-raises it, so its fold is left pending and runs again later."""
    defer = True


class Binding:
    """What a runner needs from the plugin instance whose job is running."""

    def __init__(self, llm, root, live):
        self.llm = llm          # ctx.llm
        self.root = root        # TE state root of this profile
        self.live = live        # () -> bool: still enabled, same generation


class OptimizerRunner:
    """Upstream's runner protocol: `runner(prompt, model) -> (text, effective_tokens)`.

    * no live binding           -> raises `Defer` (no call made, nothing charged);
    * the call failed            -> raises (spend unknown; Hermes's auxiliary usage has it);
    * finished after off/unload  -> ("", tokens): its spend is charged, its advice dropped;
    * billed, empty reply        -> ("", tokens).

    `paid_model` picks the side-call model; on Hermes that is ctx.llm's model override, which
    the host refuses unless plugins.entries.tokeneconomizer.llm.allow_model_override is set."""

    def __init__(self, binding, config=None):
        self.b = binding
        self.model = (getattr(config, "paid_model", "") or "").strip() or None

    def __call__(self, prompt, _model="agent"):
        b = self.b
        if b is None or not b.live():
            _audit(b, status="deferred")
            raise Defer("tokeneconomizer optimizer: no live binding")
        from agent.redact import redact_for_egress
        from tokeneconomizer.plugin import accounting
        text = redact_for_egress(prompt)
        started = time.time()
        try:
            res = b.llm.complete([{"role": "user", "content": text}], task=TASK,
                                 model=self.model, timeout=TIMEOUT_S,
                                 purpose="tokeneconomizer optimizer", max_tokens=2048)
        except Exception as e:
            _audit(b, status="error", error=type(e).__name__, model=self.model,
                   prompt_chars=len(text), spend="unknown",
                   elapsed_s=round(time.time() - started, 2))
            if signed_out(e):
                pause(b.root, e)
            raise
        u = res.usage
        usage = None
        if u is not None and (u.input_tokens or u.output_tokens):
            usage = {"input_tokens": int(u.input_tokens or 0),
                     "cache_read_input_tokens": int(u.cache_read_tokens or 0),
                     "cache_creation_input_tokens": int(u.cache_write_tokens or 0),
                     "output_tokens": int(u.output_tokens or 0), "model": res.model}
        eff, priced_by = accounting.effective(usage)
        late = not b.live()
        _audit(b, status="dropped_late" if late else ("ok" if res.text else "empty"),
               model=res.model, provider=res.provider, prompt_chars=len(text),
               reply_chars=len(res.text or ""), usage=usage, effective_tokens=eff,
               priced_by=priced_by, cost_usd=getattr(u, "cost_usd", None),
               elapsed_s=round(time.time() - started, 2))
        # Recorded before the drop decision: a late reply's spend is still charged. The reply
        # is redacted like everything else upstream keeps (fail-closed).
        from .host import _redact
        return ("" if late or not res.text else _redact(res.text)), eff


_SIGNED_OUT = re.compile(
    r"To authenticate|/login\b|\b[Nn]ot (?:logged|signed) in|\bunauthori[sz]ed\b|"
    r"authentication (?:failed|error|required)|invalid (?:x-)?api[_ -]?key|"
    r"no api[_ -]?key|api[_ -]?key (?:is )?(?:missing|not (?:set|found|configured))|"
    r"(?:missing|no|expired|invalid) (?:usable )?credentials|"
    r"(?:expired|invalid|revoked) (?:oauth |access |refresh )?token", re.I)


def signed_out(exc):
    """Whether the optimizer's route failed because it is not signed in: HTTP 401/403, or
    what the provider or Hermes says when no usable login is configured."""
    for obj in (exc, getattr(exc, "response", None)):
        code = getattr(obj, "status_code", None) or getattr(obj, "status", None)
        if code in (401, 403):
            return True
    return bool(_SIGNED_OUT.search(f"{type(exc).__name__}: {exc}"))


def pause(root, exc):
    """Upstream's pause on this host's side calls (`sidecall.paused`): every later call would
    fail the same way. Upstream then says so once, at the next session start, and
    `te config set microskills=...` / `loopguard=...` lifts it."""
    from tokeneconomizer.plugin import sidecall
    tail = " ".join(f"{type(exc).__name__}: {exc}".split())[-300:]
    try:
        from .host import _redact
        tail = _redact(tail)
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} sidecall hermes: no reply "
                     f"(not signed in) {tail}\n")
        path = sidecall._pause_path("hermes", root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), "reason": tail[-200:]}, fh)
    except OSError:
        pass


def _audit(b, **fields):
    if b is None:
        return
    try:
        os.makedirs(b.root, exist_ok=True)
        with open(os.path.join(b.root, "optimizer.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(fields, at=time.time(), task=TASK), default=str) + "\n")
    except OSError:
        pass
