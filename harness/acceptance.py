"""Candidate-wide acceptance on the REAL installed Hermes, fresh disposable roots, final code.

    /usr/bin/python3 harness/acceptance.py [area ...]     (areas: A1..A7; default all)

Every check prints PASS/FAIL with its evidence; raw artifacts stay under sandbox/acc-*/runs.
Model calls: the test model named by TE_TEST_BASE_URL / TE_TEST_MODEL / TE_TEST_API_KEY, through
`harness/turn` (sandbox config). The checks were tuned on deepseek-v4.1-flash.
"""
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time

W = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SB = f"{W}/sandbox"
# chisle: the commands below are shell strings with this path pasted in; refusing other
# characters keeps them from being split or interpreted. Argument lists if that ever bites.
if re.search(r"[^A-Za-z0-9_./+-]", W):
    print(f"acceptance: the checkout path may hold only letters, digits and _ . / + -: {W!r}."
          " Move the checkout to such a path.", file=sys.stderr)
    sys.exit(2)
TESTS = ("Run the project's tests the way the README says. If that command is not available "
         "here, run them with python3 -m unittest -q instead. Reply with the number of tests "
         "that passed, nothing else.")
SPLIT = ("First run exactly tox -q as its own tool call. Only if that fails, run exactly "
         "python3 -m unittest -q as a separate tool call. Reply with the pass count only.")
LOOP = ("Team retry policy for the transient-timeout check: run exactly the command "
        "./scripts/remote-check.sh (nothing appended, no pipes, no echo) once per tool call, and "
        "keep re-running it until it exits 0 or 30 attempts have been made. Do not read or "
        "modify the script. Then report the final status.")
RESULTS = []


def carries(v):
    """Whether a tool-result message carries a TE advice block: as text, or as a value of the
    JSON object its result starts with (Hermes adds its own loop warning after that object)."""
    if isinstance(v, dict):
        return any(carries(x) for x in v.values())
    if isinstance(v, list):
        return any(carries(x) for x in v)
    if not isinstance(v, str):
        return False
    if '<tokeneconomizer kind="tool">' in v:
        return True
    try:
        return v.lstrip().startswith("{") and carries(json.JSONDecoder().raw_decode(v.lstrip())[0])
    except ValueError:
        return False


def check(area, name, ok, evidence=""):
    RESULTS.append({"area": area, "check": name, "ok": bool(ok), "evidence": evidence})
    print(f"{'PASS' if ok else 'FAIL'} {area} {name} :: {evidence}", flush=True)


def sh(cmd, env=None, cwd=None, timeout=900):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd, timeout=timeout,
                       env=dict(os.environ, **(env or {})))
    return p.returncode, p.stdout + p.stderr


def root(name, plugin=True):
    rc, out = sh(f"{W}/harness/new-root {name}" + ("" if plugin else " --no-plugin"))
    r = out.strip().splitlines()[-1]
    sh(f"{W}/harness/make-repo {r}/repo")
    sh(f"sed -i 's/max_turns: 30/max_turns: 60/' {r}/home/.hermes/config.yaml")
    return r


def te(r, args, cwd=None):
    return sh(f"{W}/harness/hx te {args}", env={"HX_ROOT": r}, cwd=cwd or f"{r}/repo")


def turn(r, label, prompt, extra="", env=None):
    e = {"HX_EXTRA_ENV": "HERMES_DUMP_REQUESTS=1"}
    e.update(env or {})
    q = prompt.replace("'", "'\\''")
    rc, out = sh(f"{W}/harness/turn {r} {label} '{q}' {extra}", env=e)
    u = json.load(open(f"{r}/runs/{label}.usage.json"))
    return u, open(f"{r}/runs/{label}.out").read()


def dumps(r, sid):
    out = []
    for f in sorted(glob.glob(f"{r}/home/.hermes/sessions/request_dump_{sid}_*.json")):
        body = json.load(open(f))["request"]["body"]
        out.append(body)
    return out


def tool_names(body):
    return [t.get("name") or (t.get("function") or {}).get("name") for t in body.get("tools", [])]


def offered(body):
    """Tools the request offers: full schemas plus names Hermes lists inside `tool_search`'s
    deferred catalog (callable through tool_search/tool_call without a schema)."""
    names = set(tool_names(body))
    for t in body.get("tools", []):
        fn = t.get("function") or t
        if fn.get("name") == "tool_search":
            desc = fn.get("description") or ""
            i = desc.find("Deferred tool catalog")
            names |= set(re.findall(r"^- ([^\s:]+)", desc[i:], re.M)) if i >= 0 else set()
    return names


def blocks(body):
    return json.dumps(body.get("messages", [])).count('tokeneconomizer kind=\\"session-start')


def pd(r):
    return f"{r}/home/.tokeneconomizer"      # upstream's shared root under the sandbox HOME


def ledger(r):
    out = []
    for f in glob.glob(f"{pd(r)}/repos/*/hermes/ledger.jsonl"):
        out += [json.loads(l) for l in open(f) if l.strip()]
    return out


def events(r):
    out = []
    for f in glob.glob(f"{pd(r)}/repos/*/hermes/events.jsonl"):
        out += [json.loads(l) for l in open(f) if l.strip()]
    return out


def commands(r, sid):
    f = f"{pd(r)}/hermes-sessions/{sid}.jsonl"
    return re.findall(r'"command": "([^"]*)"', open(f).read()) if os.path.exists(f) else []


def pty(r, label, steps, timeout=90):
    path = f"{r}/runs/{label}.steps"
    os.makedirs(f"{r}/runs", exist_ok=True)
    open(path, "w").write("\n".join(steps) + "\n")
    sh(f"/usr/bin/python3 {W}/harness/pty_drive.py "
       f"{r}/runs/{label}.bin {timeout} -- {W}/harness/hx --cli < {path}",
       env={"HX_ROOT": r, "HX_EXTRA_ENV": "HERMES_DUMP_REQUESTS=1"}, timeout=1200)
    raw = open(f"{r}/runs/{label}.bin", "rb").read().decode("utf-8", "replace")
    text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", raw).replace("\r", "")
    m = re.search(r"Session: *([0-9_a-f]+)", text)
    return text, (m.group(1) if m else None)


# ---------------------------------------------------------------------------------------------
def a1():
    r = root("acc-a1")
    rc, out = sh(f"{W}/harness/hx plugins doctor tokeneconomizer", env={"HX_ROOT": r})
    check("A1", "plugins doctor registration", "registration passed" in out and "WARN" not in out,
          re.sub(r"\s+", " ", out.strip())[-160:])
    rc, out = sh(f"{W}/harness/hx plugins list --plain --no-bundled", env={"HX_ROOT": r})
    check("A1", "enabled in isolated profile", re.search(r"enabled\s+user\s+\S+\s+tokeneconomizer", out),
          out.strip().splitlines()[-1] if out.strip() else "")
    rc, out = te(r, "status")
    check("A1", "hermes te status targets the shared TE root, upstream defaults", pd(r)
          in out and "(hermes)" in out and "toolprune=auto" in out, out.splitlines()[0])
    rc, out = te(r, "doctor")
    check("A1", "doctor: pin intact + Hermes section", "intact" in out and "hermes      plugin" in out,
          [l for l in out.splitlines() if "pin" in l][:1])
    u, out = turn(r, "skill", "Without using any tools, quote verbatim the H1 heading line of the "
                  "tokeneconomizer skill preloaded into this session, then stop.",
                  extra="-s tokeneconomizer:tokeneconomizer")
    check("A1", "bundled skill loads into a real session", "# TokenEconomizer (Hermes)" in out,
          out.strip()[:60])
    text, sid = pty(r, "repl", ["wait ❯", "sleep 2", "send /te status", "wait loopguard",
                                "sleep 1", "send /exit", "sleep 6"])
    check("A1", "/te status in real REPL", "TokenEconomizer -- repo (hermes)" in text, sid)


def a2_a3_a4():
    r = root("acc-learn")
    te(r, "config set microskills=on toolprune=propose toolprune_sessions=3 toolprune_min_sessions=1 "
          "toolprune_evidence=1 --global")
    sids = []
    for i in range(1, 5):
        u, out = turn(r, f"F{i}", SPLIT)
        sids.append(u["session_id"])
    rc, facts = te(r, "facts")
    check("A2", "fact learned after repeated sessions",
          "form_repair:run_tests:tox" in facts and "INJECTED" in facts,
          [l.strip() for l in facts.splitlines() if "INJECTED" in l][:2])
    opt = [json.loads(l) for l in open(f"{pd(r)}/optimizer.jsonl")] if os.path.exists(
        f"{pd(r)}/optimizer.jsonl") else []
    check("A2", "proposer ran through ctx.llm (owned aux task)",
          opt and all(o["status"] == "ok" and o["task"] == "tokeneconomizer_optimizer" for o in opt),
          f"{len(opt)} call(s); {[o.get('usage', {}).get('input_tokens') for o in opt]}")
    # Fresh session: block once per request, behaviour follows the fact.
    u, out = turn(r, "fresh", "Run the project's tests. Reply with the pass count only.")
    ds = dumps(r, u["session_id"])
    check("A2", "fresh session: facts block once in every request",
          ds and all(blocks(b) == 1 for b in ds), [blocks(b) for b in ds])
    check("A2", "session-start notice shown in the reply", "learned fact(s)" in out,
          [l for l in out.splitlines() if "TokenEconomizer" in l][:1])
    cmds = commands(r, u["session_id"])
    check("A2", "model used the fact (no tox call)", cmds and not any("tox" in c for c in cmds), cmds)
    # Fold it before resuming (a `-z` fold is cut at 25 s and a slow learning pass can still be
    # queued), so its ledger record holds this session's own usage, as a live session's would.
    te(r, "fold")
    u2, out = turn(r, "resume", "Which command did you run for the tests? Command only.",
                   extra=f"-r {u['session_id']}")
    ds = dumps(r, u["session_id"])
    check("A2", "resume: block replayed once, not re-added", blocks(ds[-1]) == 1, blocks(ds[-1]))
    # Ledger vs Hermes usage reports, every one-shot so far.
    led = {x["session_id"]: x for x in ledger(r) if x.get("kind") == "session"}
    bad = []
    for lb in ("F1", "F2", "F3", "F4", "fresh"):
        uu = json.load(open(f"{r}/runs/{lb}.usage.json"))
        rec = led.get(uu["session_id"])
        if not rec or (rec["input_tokens"], rec["output_tokens"], rec["cache_read_input_tokens"],
                       rec["cache_creation_input_tokens"], rec["requests"]) != (
                uu["input_tokens"], uu["output_tokens"], uu["cache_read_tokens"],
                uu["cache_write_tokens"], uu["api_calls"]):
            bad.append((lb, rec and {k: rec[k] for k in ("input_tokens", "output_tokens")},
                        {k: uu[k] for k in ("input_tokens", "output_tokens")}))
    check("A6", "ledger equals Hermes usage report per session", not bad, bad or "5/5 exact")
    counts = {}
    for x in ledger(r):
        if x.get("kind") == "session":
            counts[x["session_id"]] = counts.get(x["session_id"], 0) + 1
    check("A6", "one ledger record per session", counts and max(counts.values()) == 1,
          f"{len(counts)} sessions")
    # A3 pruning.
    rc, out = te(r, "toolprune")
    prop = re.search(r"proposed: (.*)", out)
    prop = [t.strip() for t in prop.group(1).split(",")] if prop else []
    check("A3", "proposal from real catalog/usage", prop and "terminal" not in prop, prop)
    from_seams = {"terminal", "read_file", "write_file", "patch", "search_files", "skill_view",
                  "memory", "delegate_task", "clarify", "web_search", "todo"}
    check("A3", "protected tools never proposed", not (set(prop) & from_seams), sorted(set(prop) & from_seams))
    proc = subprocess.Popen(f"sleep 25; {W}/harness/hx te toolprune apply", shell=True,
                            cwd=f"{r}/repo", env=dict(os.environ, HX_ROOT=r),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    text, sid = pty(r, "freeze", ["wait ❯", "sleep 2", "send What is 3+4? Number only.", "sleep 40",
                                  "send What is 5+6? Number only.", "sleep 20", "send /exit", "sleep 8"])
    applied = proc.communicate()[0]
    ds = dumps(r, sid)
    check("A3", "active session keeps its tools after apply",
          len(ds) >= 2 and all(set(prop) <= offered(b) for b in ds),
          [len(offered(b)) for b in ds])
    check("A3", "apply wrote per-checkout git-excluded file", "added:" in applied and
          ".tokeneconomizer/hermes-tools.local.json" in open(f"{r}/repo/.git/info/exclude").read(),
          applied.strip().splitlines()[0] if applied.strip() else "")
    u, out = turn(r, "pruned", "What is 1+1? Number only.")
    ds = dumps(r, u["session_id"])
    check("A3", "next session request omits applied tools", ds and not (set(prop) & offered(ds[0]))
          and "terminal" in tool_names(ds[0]), len(offered(ds[0])))
    rc, out = te(r, "toolprune restore")
    u, out = turn(r, "restored", "What is 1+1? Number only.")
    ds = dumps(r, u["session_id"])
    check("A3", "restore gives the tools back next session", ds and set(prop) <= offered(ds[0]),
          len(tool_names(ds[0])))
    # A4 guard: >= 5 ended-green turns exist (F1..F4, fresh, resume, freeze ...).
    rc, st = te(r, "status")
    check("A4", "envelope learned before guard acts", "envelope not learned" not in st,
          [l.strip() for l in st.splitlines() if "loopguard" in l][-1:])
    # The guard only acts once a turn has run long enough (gate_min_turns=18); a model that
    # gives up early never reaches it. Retry the task (bounded) until the turn is long enough.
    for attempt in range(1, 4):
        u, out = turn(r, f"loop{attempt}" if attempt > 1 else "loop", LOOP)
        if len(commands(r, u["session_id"])) >= 20:
            break
    check("A4", "a long failing turn occurred (model-dependent precondition)",
          len(commands(r, u["session_id"])) >= 20, f"attempt {attempt}, "
          f"{len(commands(r, u['session_id']))} calls")
    ev = [e for e in events(r) if e.get("session_id") == u["session_id"]]
    kinds = [e["kind"] for e in ev]
    check("A4", "same-turn nudge from real judge", "nudge" in kinds,
          [e.get("reason", "")[:40] for e in ev if e["kind"] == "nudge"])
    ds = dumps(r, u["session_id"])
    results = [m for m in (ds[-1]["messages"] if ds else []) if m.get("role") == "tool" or (
        m.get("role") == "user" and isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]))]
    carrying = [i + 1 for i, m in enumerate(results) if carries(m)]
    decided = [e["tool_calls"] for e in ev if e["kind"] in ("nudge", "abort")]
    check("A4", "redirect appended to the deciding call's own result",
          carrying and decided and set(decided) <= set(carrying),
          f"tool results carrying a TE block #{carrying}; guard decisions at calls {decided}")
    check("A4", "notices shown in the user's reply", "TokenEconomizer" in out,
          [l for l in out.splitlines() if "TokenEconomizer" in l][:2])
    calls = len(commands(r, u["session_id"]))
    stuck = next((e["tool_calls"] for e in ev if e["kind"] == "abort"), None)
    # The guard only advises: the session runs on to its own end (LOOP caps the task at 30
    # attempts, so an abort notice at call 30 is followed by the agent's normal report).
    check("A4", "agent never terminated by the guard",
          u["completed"] and bool(out.strip()) and calls >= (stuck or 0),
          f"abort notice at call {stuck}, session ran {calls} calls, completed={u['completed']}")
    over = [x for x in ledger(r) if x.get("kind") == "overhead" and x["session_id"] == u["session_id"]]
    check("A6", "judge spend charged once per job (overhead rows)", over and len({x["job"] for x in over})
          == len(over), [x["overhead"] for x in over][:2])
    # A2 contradiction retirement.
    os.makedirs(f"{r}/shim", exist_ok=True)
    open(f"{r}/shim/tox", "w").write('#!/bin/sh\nexec python3 -m unittest "$@"\n')
    os.chmod(f"{r}/shim/tox", 0o755)
    for i in (1, 2):
        turn(r, f"X{i}", "Run exactly tox -q (it is installed now) as one tool call and reply "
             "with the pass count only.", env={"HX_PATH_PREFIX": ":".join(
                 x for x in (f"{r}/shim", os.environ.get("HX_PATH_PREFIX")) if x)})
    # `-z` folds at turn end within 25 s; the test model's learning pass takes ~70 s, so the
    # cut-short fold is finished here with the documented control (long-lived sessions do it
    # in the background).
    te(r, "fold")
    rc, facts = te(r, "facts")
    check("A2", "contradiction retires the fact",
          re.search(r"RETIRED .*form_repair:run_tests:tox", facts), [l.strip() for l in facts.splitlines()
                                                                     if "tox" in l])
    # Privacy: synthetic secret through a tool output.
    secret = "sk-proj-SYNTHETICabcdef0123456789abcdef0123"
    turn(r, "secret", f"Run exactly: echo OPENAI_API_KEY={secret} > /dev/null; echo done . Reply done.")
    leaked = [f for f in glob.glob(f"{pd(r)}/**/*", recursive=True)
              if os.path.isfile(f) and secret in open(f, errors="replace").read()]
    check("A6", "synthetic secret absent from plugin state/logs", not leaked, leaked[:3])
    return r


def a2_compaction():
    r = root("acc-compact")
    te(r, "config set microskills=envfacts --global")
    for i in range(1, 5):
        turn(r, f"F{i}", SPLIT)
    with open(f"{r}/home/.hermes/config.yaml", "a") as fh:
        # Sized to the test model: deepseek-v4.1-flash starts near 15k and adds ~0.9k per essay.
        fh.write("compression:\n  threshold_tokens: 19000\n  protect_last_n: 2\n")
    steps = ["wait ❯", "sleep 2", "send Run the project's tests and reply with the pass count only.",
             "sleep 22"]
    for t in ("rivers", "mountains", "deserts", "forests", "oceans", "glaciers"):
        steps += [f"send Without using tools, write about 700 words of plain prose about {t}.", "sleep 60"]
    steps += ["send Which exact command runs this project's tests? Command only.", "sleep 40",
              "send /exit", "sleep 10"]
    text, sid = pty(r, "compact", steps)
    log = open(f"{r}/home/.hermes/logs/agent.log").read()
    compactions = len(re.findall(rf"{sid}\].*(Preflight|Pre-API) compression", log))
    user_reqs = [b for b in dumps(r, sid) if "Review the conversation above" not in json.dumps(
        [m for m in b["messages"] if m.get("role") == "user"][-1:])]
    after = [b for b in user_reqs if "CONTEXT COMPACTION" in json.dumps(b["messages"])]
    check("A2", "compaction continuity: block exactly once after real compactions",
          compactions >= 1 and after and all(blocks(b) == 1 for b in user_reqs),
          f"{compactions} compaction(s); blocks per user request {[blocks(b) for b in user_reqs]}")


def a3_auto_mcp():
    r = root("acc-auto")
    te(r, "config set toolprune=auto toolprune_quiet_minutes=1 toolprune_sessions=3 "
          "toolprune_min_sessions=1 toolprune_evidence=1 --global")
    for i in range(1, 5):
        turn(r, f"A{i}", "Run python3 -m unittest -q in the repo and reply with the pass count only.")
    f = f"{r}/repo/.tokeneconomizer/hermes-tools.local.json"
    early = os.path.exists(f)
    deadline = time.time() + 180
    while not os.path.exists(f) and time.time() < deadline:
        time.sleep(5)
    check("A3", "auto mode applies only after the idle window", not early and os.path.exists(f),
          f"present before idle: {early}; after: {os.path.exists(f)}")
    # A5 off/on on this root (deny applied).
    deny = set(json.load(open(f))["deny_tools"]) if os.path.exists(f) else set()
    sh(f"{W}/harness/hx plugins disable tokeneconomizer", env={"HX_ROOT": r})
    u, _ = turn(r, "off1", "What is 1+1? Number only.")
    b = dumps(r, u["session_id"])[0]
    no_state = not os.path.exists(f"{pd(r)}/hermes-sessions/{u['session_id']}.jsonl")
    check("A5", "plugin disabled: full tools, no state", deny <= offered(b) and no_state,
          len(offered(b)))
    sh(f"{W}/harness/hx plugins enable tokeneconomizer < /dev/null", env={"HX_ROOT": r})
    u, _ = turn(r, "on1", "What is 1+1? Number only.")
    b = dumps(r, u["session_id"])[0]
    check("A5", "re-enabled: omission resumes", not (deny & offered(b)), len(offered(b)))
    te(r, "config set enabled=false --global")
    u, _ = turn(r, "off2", "What is 1+1? Number only.")
    b = dumps(r, u["session_id"])[0]
    check("A5", "enabled=false: full tools, no state", deny <= offered(b) and not os.path.exists(
        f"{pd(r)}/hermes-sessions/{u['session_id']}.jsonl"), len(offered(b)))
    te(r, "config set enabled=true --global")
    # Worktree: shared learning, per-checkout deny (apply in the worktree only).
    te(r, "toolprune restore")
    main_before = open(f).read()
    sh(f"git -C {r}/repo worktree add -q {r}/wt")
    rc, out = te(r, "toolprune apply text_to_speech vision_analyze", cwd=f"{r}/wt")
    wt_file = f"{r}/wt/.tokeneconomizer/hermes-tools.local.json"
    wt_deny = set(json.load(open(wt_file))["deny_tools"]) if os.path.exists(wt_file) else set()
    cfg = f"{r}/home/.hermes/config.yaml"
    base_cfg = open(cfg).read()
    open(cfg, "w").write(base_cfg.replace(f"cwd: {r}/repo", f"cwd: {r}/wt"))
    u, _ = turn(r, "wt1", "What is 1+1? Number only.")
    open(cfg, "w").write(base_cfg)
    wt_tools = set(tool_names(dumps(r, u["session_id"])[0]))
    u, _ = turn(r, "main1", "What is 1+1? Number only.")
    main_tools = set(tool_names(dumps(r, u["session_id"])[0]))
    check("A5", "worktree: apply there leaves main checkout's deny file unchanged",
          wt_deny and open(f).read() == main_before, f"worktree denies {sorted(wt_deny)}")
    check("A5", "worktree session omits, main-checkout session keeps",
          not (wt_deny & wt_tools) and wt_deny <= main_tools,
          f"wt {len(wt_tools)} tools, main {len(main_tools)} tools")
    rc, out = te(r, "status", cwd=f"{r}/wt")
    rc2, out2 = te(r, "status")
    key = lambda o: re.search(r"repos/([^/]+)/hermes", o).group(1) if re.search(r"repos/([^/]+)/hermes", o) else None
    check("A5", "worktree shares the repository's learned state", key(out) and key(out) == key(out2),
          key(out))
    # MCP direct tools.
    m = root("acc-mcp")
    with open(f"{m}/home/.hermes/config.yaml", "a") as fh:
        fh.write(f"mcp_servers:\n  te_probe:\n    command: /usr/bin/python3\n    args: [{W}/harness/mcp_probe_server.py]\n")
    te(m, "config set toolprune_sessions=3 toolprune_min_sessions=1 toolprune_evidence=1 --global")
    u, _ = turn(m, "deferred", "Call the beta_ping tool once and reply with its output only.")
    b = dumps(m, u["session_id"])[0]
    check("A3", "deferred MCP tools are listed in tool_search, not in request tools",
          not any("te_probe" in (n or "") for n in tool_names(b)) and "tool_search" in tool_names(b), "")
    for i in range(1, 4):
        turn(m, f"D{i}", "Run python3 -m unittest -q and reply with the pass count only.")
    rc, out = te(m, "toolprune")
    # Read the proposal line only: the per-tool table below it lists every tool, used ones too.
    prop = next((l for l in out.splitlines() if l.startswith("proposed:")), "")
    check("A3", "deferred: unused listed tool proposed, used one not",
          "mcp__te_probe__alpha_echo" in prop and "mcp__te_probe__beta_ping" not in prop, prop)
    te(m, "toolprune apply mcp__te_probe__alpha_echo")
    u, out = turn(m, "dnext", "Use tool_call to invoke mcp__te_probe__alpha_echo with text hi. "
                  "Reply with exactly what the tool returned.")
    ds = dumps(m, u["session_id"])
    desc = lambda b: next(((t.get("function") or t).get("description") for t in b["tools"]
                           if (t.get("name") or (t.get("function") or {}).get("name")) == "tool_search"), "")
    check("A3", "deferred: denied tool gone from the listing next session",
          ds and "mcp__te_probe__alpha_echo" not in desc(ds[0]) and "mcp__te_probe__beta_ping" in desc(ds[0]),
          len(desc(ds[0])) if ds else 0)
    check("A3", "deferred: call to the denied tool refused",
          any("switched off for this repository by TokenEconomizer" in json.dumps(b["messages"]) for b in ds),
          out.strip()[:80])
    with open(f"{m}/home/.hermes/config.yaml", "a") as fh:
        fh.write('tools:\n  tool_search:\n    enabled: "off"\n')
    u, _ = turn(m, "mcpnext", "Reply with OK only.")
    names = tool_names(dumps(m, u["session_id"])[0])
    check("A3", "direct MCP (tool_search off): denied tool omitted, used tool kept",
          "mcp__te_probe__alpha_echo" not in names and "mcp__te_probe__beta_ping" in names,
          [n for n in names if "te_probe" in n])


def a5_profiles():
    t = f"{SB}/acc-twoprof"
    sh(f"chmod -R u+w {t} 2>/dev/null; rm -rf -- '{t}'")
    for p in ("p1", "p2"):
        r = root(f"acc-tp-{p}")
        os.makedirs(f"{t}/{p}", exist_ok=True)
        os.rename(f"{r}/home/.hermes", f"{t}/{p}/.hermes")
        os.rename(f"{r}/repo", f"{t}/{p}/repo")
    rc, out = sh(f"{W}/harness/hxpy {W}/harness/two_profiles.py {t}", env={"HX_ROOT": t})
    try:
        d = json.loads(out[out.index("{"):])
    except ValueError:
        d = {}
    ok = (d and d["p1"]["manager"] != d["p2"]["manager"]
          and _te_root(d["p1"]["status"]) and _te_root(d["p1"]["status"]) == _te_root(d["p2"]["status"])
          and d.get("runner_for_patched") is False)
    check("A5", "two profiles in one process: separate managers, one shared root, no global runner", ok,
          {k: d.get(k) for k in ("runner_for_patched",)} if d else out[-200:])


def a7_repomap():
    r = root("acc-map")
    # TE_REPOMAP_PYTHON: a python with aider-chat 0.86.2 installed (INSTALL.md).
    py = shlex.quote(f"repomap_python={os.environ['TE_REPOMAP_PYTHON']}")
    te(r, f"config set repomap=on {py} --global")
    rc, out = te(r, "repomap refresh")
    u, out = turn(r, "map", "Without running any tools, which function in this repo multiplies "
                  "two numbers, and in which file? One line.")
    b = dumps(r, u["session_id"])
    check("A7", "repository map injected once and used", b and json.dumps(b[0]["messages"]).count(
        "# Repository map") == 1 and "mul" in out and "calc/__init__.py" in out, out.strip()[:80])

def _te_root(status):
    m = re.search(r"(/\S*/\.tokeneconomizer)\b", status or "")
    return m.group(1) if m else None


def _tool_messages(bodies):
    return [m for b in bodies for m in b.get("messages", []) if m.get("role") == "tool"]


def a8_parity():
    """Exact-parity additions on real sessions."""
    r = root("acc-parity")
    te(r, "config set toolprune=propose --global")
    # Launcher: upstream's ~/.tokeneconomizer/bin/te, written at session start, runs this plugin.
    u, out = turn(r, "first", "What is 2+2? Number only.")
    rc, out = sh(f"{pd(r)}/bin/te status --host hermes", env={"HX_ROOT": r}, cwd=f"{r}/repo")
    check("A8", "bin/te launcher written at session start and runs", rc == 0
          and "TokenEconomizer -- repo (hermes)" in out, out.strip().splitlines()[:1])
    b = dumps(r, u["session_id"])
    sysmsg = json.dumps([m for m in b[0].get("messages", []) if m.get("role") == "system"]) if b else ""
    check("A8", "plugin skill listed in the session prompt", "tokeneconomizer:tokeneconomizer" in sysmsg,
          len(sysmsg))
    # Subagent: its tool use and usage land beside the parent's log; the ledger counts them.
    u, out = turn(r, "sub", "Use delegate_task once with goal: run exactly `ls` in the repository "
                  "with the terminal tool and report the file names. Then reply with the "
                  "subagent's answer only.")
    sid = u["session_id"]
    subs = glob.glob(f"{pd(r)}/hermes-sessions/{sid}/subagents/*.jsonl")
    sub_text = "".join(open(f).read() for f in subs)
    check("A8", "subagent tool calls logged under the parent session",
          subs and '"tool_use"' in sub_text and '"terminal"' in sub_text, [os.path.basename(f) for f in subs])
    sub_out = sum(json.loads(l)["message"]["usage"]["output_tokens"] for f in subs
                  for l in open(f) if '"usage"' in l)
    led = [x for x in ledger(r) if x.get("kind") == "session" and x["session_id"] == sid]
    check("A8", "ledger includes subagent tokens", led and sub_out and led[0]["output_tokens"]
          >= u["output_tokens"] + sub_out, (led[0]["output_tokens"] if led else None, u["output_tokens"], sub_out))
    # Real-token measure.
    rc, out = te(r, "toolprune measure image_generate text_to_speech")
    try:
        mj = json.loads(out[out.index("{"):])
    except ValueError:
        mj = {}
    check("A8", "measure: provider-counted prompt tokens, with and without the tools",
          mj.get("prompt_tokens_full", 0) > mj.get("prompt_tokens_lean", 0) > 0,
          {k: mj.get(k) for k in ("prompt_tokens_full", "prompt_tokens_lean", "saved_per_request")})
    # Signed-out optimizer: pause, told once at the next session start.
    p = root("acc-pause")
    with open(f"{p}/home/.hermes/config.yaml", "a") as fh:
        fh.write("auxiliary:\n  tokeneconomizer_optimizer:\n    provider: custom\n"
                 f"    base_url: {os.environ['TE_TEST_BASE_URL']}\n    api_key: sk-invalid-te-test\n"
                 f"    model: {os.environ['TE_TEST_MODEL']}\n")
    te(p, "config set microskills=on --global")
    outs = [turn(p, f"P{i}", "Run python3 -m unittest -q and reply with the pass count only.")[1]
            for i in range(1, 4)]
    paused = os.path.exists(f"{pd(p)}/hosts/hermes/sidecall.json")
    outs += [turn(p, f"P{i}", "What is 1+1? Number only.")[1] for i in (4, 5)]
    # The pause lands during whichever session's side call first fails (model speed decides
    # which); the next session start says so, and no later one repeats it.
    told = [f"P{i}" for i, o in enumerate(outs, 1) if "paused" in o]
    check("A8", "signed-out optimizer pauses side calls; told once",
          paused and len(told) == 1, f"pause file: {paused}; notice in: {told}")
    return r


def a8_search_and_gateway():
    """tool_search results without the denied tool; a gateway (api_server) session."""
    m = root("acc-search")
    with open(f"{m}/home/.hermes/config.yaml", "a") as fh:
        fh.write(f"mcp_servers:\n  te_probe:\n    command: /usr/bin/python3\n    args: [{W}/harness/mcp_probe_server.py]\n")
    turn(m, "warm", "What is 1+1? Number only.")
    te(m, "toolprune apply mcp__te_probe__alpha_echo")
    u, out = turn(m, "search", "Call tool_search once with queries [\"te_probe\"] and reply "
                  "with the tool names it returned, comma-separated, nothing else.")
    tm = [json.dumps(x) for x in _tool_messages(dumps(m, u["session_id"])) if "matches" in json.dumps(x)]
    check("A8", "tool_search results leave the denied tool out",
          tm and not any("alpha_echo" in t for t in tm) and any("beta_ping" in t for t in tm), out.strip()[:80])
    # Gateway: api_server platform on a free port, one chat completion.
    import socket
    import secrets
    gw_key = secrets.token_hex(24)     # the api_server refuses keys under 16 characters
    port = socket.socket(); port.bind(("127.0.0.1", 0)); pn = port.getsockname()[1]; port.close()
    with open(f"{m}/home/.hermes/config.yaml", "a") as fh:
        fh.write(f"platforms:\n  api_server:\n    enabled: true\n    extra:\n      host: 127.0.0.1\n"
                 f"      port: {pn}\n      key: {gw_key}\n")
    gw = subprocess.Popen(f"{W}/harness/hx gateway run", shell=True, cwd=f"{m}/repo",
                          env=dict(os.environ, HX_ROOT=m, HX_EXTRA_ENV=f"HERMES_DUMP_REQUESTS=1 TERMINAL_CWD={m}/repo"),
                          stdout=open(f"{m}/runs/gateway.log", "w"), stderr=subprocess.STDOUT)
    try:
        import urllib.request
        ok, body = False, ""
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{pn}/health", timeout=2)
                ok = True
                break
            except Exception:
                time.sleep(2)
        if ok:
            req = urllib.request.Request(
                f"http://127.0.0.1:{pn}/v1/chat/completions", method="POST",
                headers={"Authorization": f"Bearer {gw_key}", "Content-Type": "application/json"},
                data=json.dumps({"model": "hermes", "messages": [{"role": "user", "content":
                                 "Run exactly `ls` with the terminal tool, then reply with the file names."}]}).encode())
            body = urllib.request.urlopen(req, timeout=600).read().decode()
        logs = sorted(glob.glob(f"{pd(m)}/hermes-sessions/*.jsonl"), key=os.path.getmtime)
        last = open(logs[-1]).read() if logs else ""
        bodies = [json.load(open(f))["request"]["body"] for f in sorted(
            glob.glob(f"{m}/home/.hermes/sessions/request_dump_*.json"), key=os.path.getmtime)[-3:]]
        check("A8", "gateway session: plugin tracks it and pruning applies",
              ok and '"terminal"' in last and bodies and "mcp__te_probe__alpha_echo" not in
              json.dumps([offered(b) and sorted(offered(b)) for b in bodies]),
              f"health={ok}; reply={body[:60]!r}")
    finally:
        gw.terminate()
        try:
            gw.wait(20)
        except subprocess.TimeoutExpired:
            gw.kill()


def a9_segments():
    """A gateway session started in a plain folder learns into each repository it runs commands
    in: `cd repo` mid-turn, then `workdir=repo2`. Three segments, each folded into its own
    repository; repo's facts on the `cd` result; ledger requests equal Hermes's requests."""
    m = root("acc-seg")
    sh(f"{W}/harness/make-repo {m}/repo2")
    os.makedirs(f"{m}/plain", exist_ok=True)
    te(m, "config set microskills=on toolprune=propose --global")
    for i in range(1, 5):
        turn(m, f"S{i}", SPLIT)
    rc, facts = te(m, "facts")
    check("A9", "repo learned a fact first", "INJECTED" in facts,
          [l.strip() for l in facts.splitlines() if "INJECTED" in l][:1])
    import secrets
    import socket
    import urllib.request
    gw_key = secrets.token_hex(24)
    port = socket.socket(); port.bind(("127.0.0.1", 0)); pn = port.getsockname()[1]; port.close()
    with open(f"{m}/home/.hermes/config.yaml", "a") as fh:
        fh.write(f"platforms:\n  api_server:\n    enabled: true\n    extra:\n      host: 127.0.0.1\n"
                 f"      port: {pn}\n      key: {gw_key}\n")
    # The gateway bridges config `terminal.cwd` over TERMINAL_CWD, so the plain start folder
    # goes in config; the CLI turns above ran in repo.
    sh(f"sed -i 's#^  cwd: .*#  cwd: {m}/plain#' {m}/home/.hermes/config.yaml")
    before = set(glob.glob(f"{pd(m)}/hermes-sessions/*.jsonl"))
    gw = subprocess.Popen(f"{W}/harness/hx gateway run", shell=True, cwd=f"{m}/plain",
                          env=dict(os.environ, HX_ROOT=m, HX_EXTRA_ENV=f"HERMES_DUMP_REQUESTS=1 TERMINAL_CWD={m}/plain "
                                                             # the sandbox sits inside this checkout;
                                                             # keep plain/ out of its git repository
                                                             f"GIT_CEILING_DIRECTORIES={m}"),
                          stdout=open(f"{m}/runs/gateway-seg.log", "w"), stderr=subprocess.STDOUT)
    ok, body = False, ""
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{pn}/health", timeout=2)
                ok = True
                break
            except Exception:
                time.sleep(2)
        if ok:
            prompt = (f"Use the terminal tool for each step, one tool call per step. Step 1: run exactly "
                      f"`cd {m}/repo && git status --short`. Step 2: run exactly `git log --oneline -1` "
                      f"with the terminal tool's workdir argument set to {m}/repo2. Then reply done.")
            req = urllib.request.Request(
                f"http://127.0.0.1:{pn}/v1/chat/completions", method="POST",
                headers={"Authorization": f"Bearer {gw_key}", "Content-Type": "application/json"},
                data=json.dumps({"model": "hermes", "messages": [{"role": "user", "content": prompt}]}).encode())
            body = urllib.request.urlopen(req, timeout=600).read().decode()
            # The first two segments fold on their switch, in the background (~70 s each with
            # the test model). Wait for both before stopping the gateway.
            for _ in range(60):
                new = set(glob.glob(f"{pd(m)}/hermes-sessions/*.jsonl")) - before
                s = next((os.path.basename(f)[:-6].split("~")[0] for f in new if "~" in f), None)
                done = {x["session_id"] for x in ledger(m) if x.get("kind") == "session"}
                if s and {s, f"{s}~2"} <= done:
                    break
                time.sleep(5)
    finally:
        gw.terminate()
        try:
            gw.wait(180)
        except subprocess.TimeoutExpired:
            gw.kill()
    new = sorted(set(glob.glob(f"{pd(m)}/hermes-sessions/*.jsonl")) - before)
    sid = next((os.path.basename(f)[:-6].split("~")[0] for f in new if "~" in f), None)
    # Hermes finalizes only sessions with a running agent at shutdown; this API session is idle,
    # so its last segment stays open. Fold it explicitly, as a later session start would.
    if sid:
        te(m, f"fold --session {sid}~3", cwd=f"{m}/repo2")
    check("A9", "gateway session split into three segments", sid and sorted(
        os.path.basename(f)[:-6] for f in new if os.path.basename(f).startswith(sid)) == [
        sid, f"{sid}~2", f"{sid}~3"], [os.path.basename(f) for f in new])
    led = {x["session_id"]: x for x in ledger(m) if x.get("kind") == "session"}
    want = {sid: "plain", f"{sid}~2": "repo", f"{sid}~3": "repo2"} if sid else {}
    check("A9", "each segment folded into its own repository",
          sid and all(led.get(k, {}).get("repo") == v for k, v in want.items()),
          {k: led.get(k, {}).get("repo") for k in want})
    bodies = [json.load(open(f))["request"]["body"] for f in sorted(
        glob.glob(f"{m}/home/.hermes/sessions/request_dump_{sid}_*.json"), key=os.path.getmtime)] if sid else []
    tool_rows = [json.dumps(x) for x in _tool_messages(bodies[-1:])] if bodies else []
    cd_rows = [t for t in tool_rows if "session-start" in t and "tokeneconomizer" in t]
    check("A9", "repo's facts block on the cd result, once",
          len(cd_rows) == 1 and sum(t.count("session-start") for t in tool_rows) == 1,
          f"{len(cd_rows)} row(s) of {len(tool_rows)}; reply={body[-60:]!r}")
    reqs = sum(int(led.get(k, {}).get("requests") or 0) for k in want)
    check("A9", "ledger requests over the segments equal Hermes's requests",
          sid and reqs == len(bodies), f"ledger {reqs}, Hermes {len(bodies)}")


AREAS = {"A1": a1, "A2": a2_a3_a4, "A2c": a2_compaction, "A3": a3_auto_mcp, "A5": a5_profiles,
         "A7": a7_repomap, "A8": a8_parity, "A8g": a8_search_and_gateway, "A9": a9_segments}

if __name__ == "__main__":
    started = time.time()
    sh(f"{W}/harness/check-live snapshot")
    for name in sys.argv[1:] or list(AREAS):
        try:
            AREAS[name]()
        except Exception as e:
            check(name, "area raised", False, repr(e)[:300])
    # Stop detached sandbox jobs (upstream's apply-pending waiters wait up to 24 h).
    for env_file in glob.glob("/proc/[0-9]*/environ"):
        try:
            if b"HERMES_HOME=" + SB.encode() in open(env_file, "rb").read():
                os.kill(int(env_file.split("/")[2]), 15)
        except (OSError, ValueError):
            pass
    time.sleep(1)
    rc, out = sh(f"{W}/harness/check-live")
    leak = [l for l in out.splitlines() if l.startswith(("LEAK", "sandbox process", "DRIFT hermes-agent",
                                                         "no snapshot"))]
    check("A5", "protected live state: no plugin artifacts, Hermes source unchanged",
          not [l for l in leak if not l.startswith("sandbox process")], leak[:4])
    os.makedirs(f"{W}/evidence/acceptance", exist_ok=True)
    tag = time.strftime("%Y%m%dT%H%M%S")
    json.dump({"started": started, "elapsed_s": round(time.time() - started), "results": RESULTS},
              open(f"{W}/evidence/acceptance/run-{tag}.json", "w"), indent=1)
    n_fail = sum(not r["ok"] for r in RESULTS)
    print(f"== {len(RESULTS) - n_fail} passed, {n_fail} failed; evidence/acceptance/run-{tag}.json")
    sys.exit(1 if n_fail else 0)
