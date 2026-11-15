"""Hermes drivers for upstream's host-CLI commands: `probe`, `toolprune measure`,
`toolprune refresh-catalog`.

Upstream drives the host agent's own CLI. Here the host is Hermes, so the drivers run
`hermes chat -q ... --oneshot` for this profile (its provider and auth, nothing copied):

* **capture** (`refresh-catalog`): one one-shot whose first model request this plugin captures
  in its own request middleware -- the tools Hermes built, before and after the checkout's deny
  list -- and then ends the child process before anything is sent. No model call; the child may
  leave one unanswered session in this profile's history.
* **measure**: as upstream does with `claude -p`, the prompt tokens the provider counts for two
  small one-shot requests (`Reply with exactly: OK`) in an empty directory, with and without
  the tools. Two real model calls on this profile's route, under a throwaway state root.
* **probe**: upstream's end-to-end check, on upstream's own throwaway trap repository
  (`probe.make_repo`), under a state root of its own (your learned state is never touched).
  It makes real model calls: `--sessions` one-shot sessions (default 6) plus one with the
  proposed deny list, which it applies and then restores.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

CAPTURE_ENV = "TE_HERMES_CAPTURE"
STATE_ENV = "TE_HERMES_STATE_ROOT"
MEASURE_ENV = "TE_HERMES_MEASURE_DENY"   # measure child: the tools to leave out of its request


def _opt(argv, name):
    return argv[argv.index(name) + 1] if name in argv[:-1] else None


def run(runtime, argv, cwd):
    if argv[0] == "probe":
        sessions, keep = 6, "--keep" in argv
        if "--sessions" in argv:
            try:
                sessions = max(1, int(argv[argv.index("--sessions") + 1]))
            except (IndexError, ValueError):
                return 2, "te probe: --sessions takes a number"
        lines = []
        code = probe(runtime, sessions=sessions, keep=keep, out=lines.append,
                     model=_opt(argv, "--model"))
        return code, "\n".join(lines)
    action = argv[1]
    if not _middleware_route():
        return 1, f"te toolprune {action}: not available while model.openai_runtime is " \
                  "codex_app_server (that runtime bypasses request middleware)"
    if action == "measure":
        names = [a for a in argv[2:] if not a.startswith("-")]
        return measure(runtime, cwd, names)
    cap = capture(runtime, cwd)
    if cap is None:
        return 1, f"te toolprune {action}: no request captured (Hermes did not reach its " \
                  "first model request; check `hermes doctor`)"
    return refresh_catalog(runtime, cap)


# ---- capture ---------------------------------------------------------------------------
def _middleware_route():
    """False when this profile may run turns on the codex app-server runtime, which hands the
    turn to codex before request middleware runs: a capture there would reach the provider.
    Unreadable config is treated the same (fail closed)."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.config_read_errors import FailedConfigRead
        cfg = load_config_readonly()
        if isinstance(cfg, FailedConfigRead):
            return False            # defaults / last-known-good: not this profile's config
        model = (cfg or {}).get("model") or {}
        return str(model.get("openai_runtime") or "").strip().lower() != "codex_app_server"
    except Exception:
        return False

def _hermes():
    cand = os.path.join(os.path.dirname(sys.executable), "hermes")
    return cand if os.access(cand, os.X_OK) else (shutil.which("hermes") or cand)


def _child_env(runtime, cwd, **extra):
    env = {k: v for k, v in os.environ.items() if k not in (CAPTURE_ENV, STATE_ENV, MEASURE_ENV,
                                                       "TE_DISABLE")}
    env.update(HERMES_HOME=runtime.home, TERMINAL_CWD=cwd, PYTHONUTF8="1", **extra)
    return env


def capture(runtime, cwd, timeout=180):
    """{"offered": {tool: chars}, "sent": {tool: chars}, "deferred": [...], "deny": [...]} of the
    first request a fresh one-shot session in `cwd` would send, or None."""
    if not _middleware_route():
        return None
    fd, path = tempfile.mkstemp(prefix="te-capture-", suffix=".json")
    os.close(fd)
    os.unlink(path)
    try:
        subprocess.run([_hermes(), "chat", "-q", "ping", "--oneshot"], cwd=cwd,
                       env=_child_env(runtime, cwd, **{CAPTURE_ENV: path}),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def write_capture(path, original, sent, deferred, deny):
    """Called in the capturing child by the request middleware."""
    from .runtime import _tool_name

    def sizes(tools):
        return {n: len(json.dumps(t, sort_keys=True)) for t in tools or ()
                if (n := _tool_name(t))}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"offered": sizes(original), "sent": sizes(sent), "deferred": deferred,
                   "deny": sorted(deny), "at": time.time()}, fh)


def _tokens(chars):
    return round(chars / 4)


def refresh_catalog(runtime, cap):
    from tokeneconomizer.plugin.adapter import catalog_path
    tools = sorted(set(cap["offered"]) | set(cap.get("deferred") or ()))
    path = catalog_path(runtime.root, "hermes")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"tools": tools, "source": "hermes capture", "at": time.time()}, fh)
    return 0, (f"recorded {len(tools)} tools a fresh session is offered "
               f"({len(cap['offered'])} with schemas, {len(cap.get('deferred') or ())} deferred "
               f"by tool search; local capture, no model call) -> {path}")


def _prompt_tokens(runtime, deny, timeout=300):
    """(prompt tokens, cost) the provider reported for the first request of a one-shot
    `Reply with exactly: OK` in an empty directory, `deny` left out of it."""
    root = tempfile.mkdtemp(prefix="te-measure-hermes-")
    try:
        work, state = os.path.join(root, "cwd"), os.path.join(root, "state")
        os.makedirs(work)
        env = _child_env(runtime, work, **{STATE_ENV: state, MEASURE_ENV: ",".join(deny)},
                         TE_MICROSKILLS="off", TE_LOOPGUARD="off", TE_REPOMAP="off")
        subprocess.run([_hermes(), "chat", "-q", "Reply with exactly: OK", "--oneshot"],
                       cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=timeout)
        from tokeneconomizer.trace.jsonl import lines
        logs = os.path.join(state, "hermes-sessions")
        for name in sorted(os.listdir(logs)) if os.path.isdir(logs) else ():
            for line in lines(os.path.join(logs, name)):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                u = (ev.get("message") or {}).get("usage")
                if u:
                    return sum(int(u.get(k) or 0) for k in (
                        "input_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens")), u.get("cost_usd")
        return None, None
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def measure(runtime, cwd, names=()):
    """Upstream's `toolprune measure`: the named tools, else the proposal, else what the
    checkout has applied; prompt tokens with and without them."""
    from tokeneconomizer.plugin import cli, denylist
    from tokeneconomizer.plugin.context import LiveContext
    from .host import HermesHost
    ctx = LiveContext.build(HermesHost(), cwd, runtime.env(), settle=False)
    unt = ctx.store.read("untool.json") or {}
    tools = list(names) or cli._proposal(ctx, unt) or denylist.applied_for(
        ctx.store, ctx.repo.root, "hermes")
    if not tools:
        return 1, "no proposal to measure"
    full, c1 = _prompt_tokens(runtime, ())
    lean, c2 = _prompt_tokens(runtime, tools)
    if full is None or lean is None:
        return 1, "te toolprune measure: a one-shot request reported no usage " \
                  "(check `hermes doctor`)"
    cost = (round((c1 or 0) + (c2 or 0), 4) if c1 is not None or c2 is not None else None)
    return 0, json.dumps({"tools": tools, "prompt_tokens_full": full,
                          "prompt_tokens_lean": lean, "saved_per_request": full - lean,
                          "probe_cost_usd": cost}, indent=2)


# ---- probe -----------------------------------------------------------------------------
PROMPT = ("Run this repository's test suite and report how many tests pass. Do not install "
          "anything. Keep the answer to one line.")


def _session_logs(state):
    d = os.path.join(state, "hermes-sessions")
    try:
        return {n: os.path.getmtime(os.path.join(d, n)) for n in os.listdir(d)}
    except OSError:
        return {}


def _inspect(state, name):
    from tokeneconomizer.plugin import probe as up
    from tokeneconomizer.trace.jsonl import lines
    cmds, usage, injected, catalog = [], [], False, None
    for line in lines(os.path.join(state, "hermes-sessions", name)):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "te_catalog":
            catalog = ev.get("tools")
        msg = ev.get("message") or {}
        for b in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
            if b.get("type") == "tool_use" and b.get("name") == "terminal":
                cmds.append(str((b.get("input") or {}).get("command") or ""))
        if msg.get("usage"):
            usage.append(msg["usage"])
    sid = name[:-len(".jsonl")]
    for root, _, files in os.walk(os.path.join(state, "repos")):
        if f"{sid}.json" in files and os.path.basename(root) == "sessions":
            with open(os.path.join(root, f"{sid}.json"), encoding="utf-8") as fh:
                rec = json.load(fh)
            injected = "fails here" in (rec.get("injected_text") or "")
    tests, tried = up.test_commands(cmds)
    first = tests[0][:60] if tests else None
    n = len(usage)
    per = (round(sum(sum(int(u.get(k) or 0) for k in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        for u in usage) / n) if n else None)
    return {"session": sid, "injected": injected, "tests": tests, "first_test": first,
            "tried_pytest": tried, "input_per_request": per,
            "tools": len(catalog) if catalog is not None else None}


def _line(r):
    return (f"  {r['label']:>10}: fact in context={'yes' if r['injected'] else 'no '}  "
            f"first test command: {r['first_test'] or '-'}  pytest attempts: "
            f"{r['tried_pytest']}  input/request: {r['input_per_request']} tokens")


def probe(runtime, sessions=6, keep=False, out=print, model=None):
    from tokeneconomizer.plugin import denylist, probe as up
    from tokeneconomizer.plugin.context import LiveContext
    from .host import HermesHost
    root = tempfile.mkdtemp(prefix="te-probe-hermes-")
    state = os.path.join(root, "state")
    try:
        repo, trap = up.make_repo(root)
        env = _child_env(runtime, repo, **{STATE_ENV: state}, TE_TOOLPRUNE="propose",
                         TE_MICROSKILLS="envfacts")
        env["PATH"] = trap + os.pathsep + env.get("PATH", "")
        out(f"TokenEconomizer probe -- Hermes profile {runtime.home}, {sessions} session(s) "
            "+ 1 with the proposed deny list (real model calls)")
        cap = capture(runtime, repo)
        out(f"  catalog: {len(cap['offered']) if cap else 0} tools offered "
            "(local capture, no model call)")
        tenv = dict(runtime.env(), TOKENECONOMIZER_HOME=state)
        ctx = LiveContext.build(HermesHost(), repo, tenv, settle=False)
        results = []

        def one(label):
            before = _session_logs(state)
            p = subprocess.run([_hermes(), "chat", "-q", PROMPT, "--oneshot"]
                               + (["-m", model] if model else []), cwd=repo,
                               env=env, stdin=subprocess.DEVNULL, capture_output=True,
                               encoding="utf-8", errors="replace", timeout=900)
            new = [n for n, m in _session_logs(state).items() if before.get(n) != m]
            if not new:
                out(f"  {label}: no session ran ({(p.stderr or p.stdout or '').strip()[-120:]})")
                return None
            r = dict(_inspect(state, max(new, key=lambda n: _session_logs(state)[n])),
                     label=label, answer=(p.stdout or "").strip().splitlines()[-1:] or [""])
            out(_line(r) + f"  answer: {r['answer'][0][:40]}")
            return r

        for i in range(1, sessions + 1):
            r = one(f"session {i}")
            if r is None:
                return 1
            results.append(r)
        proposal = (ctx.store.read("untool.json") or {}).get("proposal") or []
        lean = None
        if proposal:
            denylist.apply(repo, proposal, ctx.store, host="hermes")
            try:
                lean = one("deny list")
                lean_cap = capture(runtime, repo)
            finally:
                denylist.restore(repo, ctx.store, host="hermes")
        everything = results + ([lean] if lean else [])
        without = [r for r in everything if not r["injected"] and r["tests"]]
        with_ = [r for r in everything if r["injected"] and r["tests"]]
        out("")
        out(f"  without the fact: {sum(1 for r in without if r['tried_pytest'])}/{len(without)} "
            f"sessions tried pytest, which is not installed; with it: "
            f"{sum(1 for r in with_ if r['tried_pytest'])}/{len(with_)}"
            + ("" if with_ else "  (the fact never cleared its firing gate; run more "
               "--sessions)"))
        if proposal and cap and lean_cap:
            a, b = sum(cap["offered"].values()), sum(lean_cap["sent"].values())
            out(f"  deny list {', '.join(proposal)}: tool definitions ~{_tokens(a)} -> "
                f"~{_tokens(b)} tokens per request (schema chars / 4)")
        elif not proposal:
            out("  deny list: nothing proposed (a tool is proposed once unused over "
                "`toolprune_evidence` prompts across `toolprune_min_sessions` sessions)")
        return 0
    finally:
        if keep:
            out(f"  kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
