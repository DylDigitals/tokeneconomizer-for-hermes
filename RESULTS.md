# Results

What was tested, on which versions, and what came out. Status per capability: `MATRIX.md`.

## Verified checkpoint (2026-11-14)

Implementation: `d16581fb8bbada3c9c2abdcaf46881def1c4a4f3`, merged into `main` and installed
in one installation's named profile, which serves its Discord gateway. Engine: `7042ea8`, with 15 documented Hermes patches.
These are recorded results from that checkpoint, not new runs performed during documentation cleanup.

- Independent review round 4: **PASS**, no required corrections. Earlier required findings were
  closed; final review covered the last command-validation delta, not a new whole-tree audit.
- Offline suite: **125 passed** on the final reviewed revision and again after promotion to `main`.
- Installed-plugin health check: **5 passed, 0 failed**. Installed files matched the implementation.
- Profile-specific pruning deployed: that profile's 89 approved entries and ownership record
  moved to its named-profile list. Plugin reload succeeded without a gateway restart. A fresh
  request capture retained the 89 denials; no Kanban or Jev tools were denied. Capture validates
  request filtering, not a fresh provider-billed savings measurement.
- The old development checkout was removed and its GitHub repository archived. This repository
  owns the maintained adapter; retained evidence is excluded from the shareable source payload.

### Measured benefit and limits

- Real-install snapshot, 169 sessions: optimizer overhead 3.4M of 134.7M effective tokens (2.5%),
  on the configured side model. This is an observed overhead ratio, not a net-savings estimate.
- `hermes te toolprune measure` on a Claude Opus subscription route, with 89 approved hidden tools:
  69,589 to 66,435 prompt tokens, **3,154 saved per request (4.5%)**. The removed content was
  deferred-tool listing lines; capture retained 72,607 of 80,319 tool characters.
- The standalone `<state root>/bin/te` launcher previously measured another agent and reported
  32 saved. That result is invalid for Hermes. The launcher now refuses session-running commands
  and directs callers to `hermes te`.
- Reloading mid-turn clears plugin tracking for that open turn. A later message tracks it again;
  a fresh session is the proof boundary for the installed hide list.
- No controlled real-work net-savings or task-speed improvement has been established. The Opus
  request comparison does not establish savings on other models or the same percentage on another profile.

### Remaining rollout work

Three more profiles on the same installation were enabled on 2026-11-14 (two coding profiles, one
on a Claude subscription provider and one on an OpenAI-compatible Codex proxy, plus one social
media profile). Each has its own state root, its own optimizer on `deepseek-v4.1-flash`, pruning
set to `propose` before enablement and its own never-hide list. The plugin loaded into the
running gateway without a restart. One fresh chat per profile on its own model was observed and
folded, with the request middleware seeing the tool catalog on both provider types; one coding
profile's Discord chats are followed. No hide list has been proposed or applied on them yet.

Measure other providers before claiming equivalent savings. Historical-session import stays
unsupported; the repository map remains optional.

## Engine update (2026-11-12)

The figures below describe the engine-update run, before the later profile-isolation and
command-validation fixes.


- Engine: `7042ea89dd5e5ecbc67ad865ad6e256e61be8bf0` (Microsoft head checked 2026-11-12),
  replacing `d0675a3`: 203 upstream commits. All 14 Hermes patches retained; all 67
  package files checked against the upstream and shipped checksum manifests.
- Candidate implementation: `8081e0a`; the later documentation changes do not alter executable code.
- Offline adapter suite: **66 passed**. The two new fold tests pass on the old engine and
  fail on the naive update: one detects missing resumed-session charges; the other detects
  another session's tools being treated as this session's unused tools. Both pass after repair.
- Upstream regression subset against the shipped patched engine: **78 passed** across
  environment facts, productive-turn loop detection and status explanations. Initial collection
  misses came from an incomplete exported test-support tree, not product failures; the complete
  support tree resolved them without modifying upstream tests or the engine.
- Real Hermes acceptance (2026-11-12–13): **55 passed, 0 failed**, including optional repository
  map and Chisle coexistence. Stock Hermes 0.22.2; `deepseek-v4.1-flash` on
  an OpenAI-compatible endpoint; isolated roots with only the test-model credential.
  Four real compactions retained one facts block per request. Ledger matched Hermes for 5/5
  sessions; guard decisions at calls 12 and 24 reached those same results. With Chisle enabled,
  its compression fired on four results and the real guard redirect at call 13 was not lost.
  API-server gateway, subagents, direct/deferred MCP pruning, learning, restore and off/on
  lifecycle all passed. Live Hermes source/state checks found no plugin leaks or source drift.
  Raw artifacts stay ignored under `evidence/` and `sandbox/`; they are not shareable payload.
- Independent read-only review: **PASS_WITH_RISKS**, no required corrections, on `8081e0a`.
- New controls checked in a real isolated Hermes: `te status --explain` succeeds;
  `te history import --dry-run` refuses with `hermes: no session history the plugin reads`.
- Repository privacy (at that time): 114 tracked files and Git history checked against 31 configured
  credential values, zero matches. Targeted personal-reference scan: zero matches.
  `.gitignore` also excludes local secrets, environments, test artifacts and per-folder TE state.

Non-blocking limits noted by review:
- One-hour cache-write subdivision is not carried by the Hermes usage bridge; cost estimates
  can use the shorter cache-write rate. Token counts are still preserved.
- `te doctor` can print both the upstream Hermes-install line and this adapter's own health block.
- A session-end hook queued behind a busy state lock can defer learning until a later pending sweep.
- Historical-session import remains unsupported and off; its internal imported-fold branch is
  not part of the Hermes adapter's supported path.

The remainder records the previous engine's evidence, not a fresh test of this update.

## Historical baseline versions

| Item | Value |
|---|---|
| Upstream TokenEconomizer | 0.1.0, `d0675a3385948d330a793863794a432f3490b1ac`, vendored with patches P1–P14 (`plugin/tokeneconomizer/_vendor/PATCHES.md`). `UPSTREAM.sha256` = original bytes, `PINNED.sha256` = shipped bytes. |
| Hermes Agent | 0.22.2 (`d923a5e`), stock, no source edits. Linux. |
| Test model | `deepseek-v4.1-flash` through an OpenAI-compatible endpoint. |

## Historical baseline summary

- **Offline** (`harness/te-test`): 64 passed. The real vendored code with a fake model, following Hermes's real hook order. Each fix made after a review has a test that failed on the code before the fix.
- **Real Hermes sessions** (`harness/te-test real`, fresh throwaway sandboxes):
  - Full run, 2026-11-10: 52 of 53. The one miss was model behaviour: the learned fact was in every request and the model still tried the failing command first. The same check passed on the run before, with the same model.
  - Facts, pruning, guard, accounting and Chisle areas rerun on 2026-11-12 after the last loop-guard fix: 26 of 26.
  - Same day, after the harness was made machine-independent: install, profile, subagent and Chisle areas 15 of 16, then guard and Chisle areas 26 of 26. The one miss was model behaviour: the test model wrapped the retried command in a script, the failures came back as one-line results, and the guard had nothing to act on.
- **Real use:** one installation has run it in a Discord gateway since 2026-11-12, with learned facts on, pruning as proposals only and the guard on. `harness/te-test live` checks such an install without changing its settings.
- **Historical savings:** −10% total effective tokens in one small sandbox comparison (below).
  Later real-install request measurements and their limits are recorded in the verified checkpoint above.
- **Upstream's own tests:** the unchanged suite at the previous pin `d0675a3` gave 397 passed, 1 skipped on the original bytes. Not rerun on the patched copy.

## Acceptance areas (real Hermes)

| ID | Area | Observed |
|---|---|---|
| A1 | Packaging, skill, controls | doctor registration OK; enabled in the isolated profile; the model quoted the preloaded skill; `/te status` worked in a PTY REPL; `hermes te status` used the profile root |
| A2 | Learning | `form_repair:run_tests:tox` and a distiller fact learned and injected; the real proposer ran via `ctx.llm`. Fresh session: block once per request, session-start notice in the reply, model ran `python3 -m unittest` (no `tox`). Resume replayed the block once. Two real compactions kept it exactly once per request. A contradicting session retired the fact. |
| A3 | Pruning | Proposal from the real catalog `browser_exec, browser_vault, computer_use, execute_code, manage_connections, text_to_speech, vision_analyze`; no protected tool proposed. Apply wrote the per-checkout git-excluded file. An apply during a session left it at 27 tools; the next session sent 18; restore brought it back to 20. Auto mode applied only after the idle window. Deferred (`tool_search` listing): unused MCP tool proposed, gone from the listing next session, call refused. Direct MCP (`tool_search` off): unused tool omitted, used one kept. |
| A4 | Guard | Envelope learned. A 30-call failing turn occurred; the real judge triggered a nudge (`judge-stuck@0.72`) and the redirect was appended to the deciding call's own result. Notices appeared in the reply. The session completed; the guard never terminated it. |
| A5 | Identity, lifecycle, off | Two profiles in one process: separate managers and state roots, no process-global optimizer runner. Worktree: an apply there left the main checkout's file unchanged; learned state shared. Plugin disabled and `enabled=false`: full tool list, no state; re-enabling resumed omission. The real Hermes home and source were unchanged by the sandbox runs. |
| A6 | Accounting, privacy | The ledger equals Hermes's usage report exactly for 5/5 sessions (input, output, cache read, cache write, requests), one record per session. Judge spend is charged once per job. A synthetic secret is absent from all plugin state. |
| A7 | Repository map | Injected once; the model answered from it without tools. |
| A8 | Sub-agents, gateway, search | Sub-agent tool calls and tokens counted under the parent. An `api_server` gateway session was tracked and pruned. `tool_search` left out denied tools. A signed-out optimizer route paused the plugin's calls and said so once. |

## Workflow comparison (sandbox, 2026-11-10)

Two repetitions per arm, `harness/compare.sh <on|off> <rep>` and `harness/compare_report.py`. Nine one-shot sessions per arm on a synthetic repository: control, tests ×6, a retry-loop task (25-attempt cap), tests. On arm: `microskills=on toolprune=auto`, idle window 0 and 1-session evidence (accelerated). Main-session effective tokens are weighted input 1, cache read 0.10, cache write 1.25, output 5. The plugin's own calls use the figure the plugin recorded, which weights output 6 for a model with no price curve.

| Arm | Correct | API calls | Prompt tokens | Main effective | Optimizer effective | Total effective | Elapsed |
|---|---|---|---|---|---|---|---|
| off-1 | 9/9 | 64 | 865,640 | 206,489 | 0 | 206,489 | 490 s |
| off-2 | 9/9 | 62 | 867,860 | 233,215 | 0 | 233,215 | 801 s |
| on-1 | 9/9 | 64 | 682,913 | 183,756 | 9,038 (7 calls) | 192,794 | 764 s |
| on-2 | 9/9 | 65 | 702,594 | 182,527 | 20,585 (6 calls) | 203,112 | 810 s |
| **mean off** | 9/9 | 63 | 866,750 | 219,852 | 0 | 219,852 | 645 s |
| **mean on** | 9/9 | 64.5 | 692,754 | 183,142 | 14,812 | 197,953 | 787 s |

- Prompt tokens −20%, total effective tokens −10% after the plugin's own model calls; both on runs below both off runs. Off runs differ by 13% from each other, so −10% is a small margin.
- Where it came from: pruning removed the same 7 unused tools in both on runs, so each request was smaller. API calls did not drop: the learned fact did not shorten the tests task, and the guard did not shorten the loop task (the task itself asks for 25 attempts).
- Slower with the plugin on: each one-shot waits up to 25 s for its learning pass at exit. Long-lived (gateway) sessions do this in the background.
- Limits of this test: a tiny repository and a ~25-tool sandbox toolset, where 7 tools are a large share. A profile with many MCP tools has more to prune; a profile that uses most of its tools has less. Concurrent runs make elapsed time noisy.

## Host observations

- `hermes -z` exits through `os._exit`, so it never fires `on_session_finalize`; the plugin folds at the end of the one-shot turn.
- `-z` resolves the terminal cwd from config. The plugin follows `TERMINAL_CWD` and skips `$HOME`.
- Pre-API compaction runs after `pre_llm_call`. The `llm_request` repair covers it.
- `post_tool_call` runs on a timeout worker and can finish after the same call's `transform_tool_result` (`MATRIX.md` row 22).
- Hermes adds its own loop hint to repeated identical failures, so models may stop early. The guard test uses a failure that looks transient.
- Tool search defers MCP schemas by default.
- With the test model, learning takes about 70 s per session.

## Limitations (Hermes interface limits)

`MATRIX.md` rows 17, 24, 27, 34, 36, 37:
- `codex_app_server` runtime: no pruning or request repair (Hermes runs no request middleware there).
- Notices appear at the end of the reply, not as a separate status line.
- The stored transcript keeps Hermes's raw `tool_search` result; the model sees the filtered one.
- The `-z` fold is bounded to 25 s; the next session start or `te fold` finishes it.
- An in-flight optimizer call cannot be cancelled. The plugin requests a 180 s timeout; Hermes's own retries and fallback can extend it. Its result is dropped.
- Discord sessions have no automated test (real use on one installation only); the TUI was not exercised. The `api_server` gateway was tested.
- Rotated-session compaction lineage is covered by unit tests only; this Hermes compacted in place.
- A loop redirect lost to another plugin's rewrite is re-added by tool-call id; on a route where Hermes rewrites ids (Mistral), it stays lost. The last 256 such blocks per session are kept.
- A redirect decided on a web, browser or MCP result waits for the next ordinary tool result in the same turn; if the turn has none, it is dropped.
- The guard reads the first 20,000 characters of a tool result (upstream's limit).
- Redaction does not classify data. Pruning of the deferred listing depends on Hermes's listing text format.

## Review

The code went through independent read-only reviews at each stage; every required finding was fixed with a test that failed before the fix. The last round returned no required corrections.
