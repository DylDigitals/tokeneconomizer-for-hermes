# Plan: group learning by the project a session works in

Status: merged to `main` at `972c7d1` on November 13, 2026, and live on one installation's
named profile since November 14, 2026.
Resident Discord verification passed November 14: a gateway chat started in the home folder,
`cd` into the repository opened a second per-project segment with its own learning. The
`workdir=` switch (command in another repo without `cd`) is proven only by sandbox A9.
Offline suite 91 passed;
real-Hermes A9 (gateway session plain → `cd repo` → `workdir=repo2`) 6/6 at `d6839f4`.
Implementation review: rounds 1–3 BLOCK (late duplicate observers, finalize racing a switch,
plugin reload losing Hermes's terminal cwd, cached plain-folder classification, call-id reuse,
bounded stale-turn memory), each fixed with a test that failed first; round 4 PASS_WITH_RISKS.
Accepted limits: ended-turn ids are kept until finalize (~1.75 MiB per 10,000 turns); past 256
remembered calls a very late duplicate may be recorded twice, never switched; an idle API-server
session's last segment folds at the next session start or `te fold`, since Hermes finalizes only
running sessions at shutdown.
Review round 1 (`1276a93`): BLOCK, six required corrections (R1–R6). Round 2 (`db80c12`): R2, R3,
R4, R6 closed; R1 and R5 remedies corrected below. Round 3 (`ad26590`): PASS, no required
corrections.

## Problem

The adapter pins each Hermes session to one repository at its first turn, from
`resolve_agent_cwd()`. Gateway sessions (Discord, Telegram, Slack, API server, cron) all start
in the profile's configured folder, usually the home directory, so every gateway session writes
to one notebook. Facts, tool evidence and the guard envelope for every project are mixed there.

Measured on one installation's named profile, Discord sessions since 2026-11-10
(read-only query of `state.db`, terminal results that echoed a directory change):

- 70 sessions; 0 recorded a session `cwd`.
- 44 ran terminal commands inside a git repository; 26 never did.
- Of the 44: 31 entered the first repository during the first user turn; 7 had one user turn.
- Of the 44: 15 used one repository, 29 used two to nine.

So the switch must happen mid-turn, and a session must be able to move more than once.

## Design: one upstream session per project

A Hermes session becomes a chain of upstream sessions ("segments"). Each segment is pinned to one
repository and gets its own transcript log, facts injection, guard state, ledger and fold, through
the public hook events the adapter already sends. Upstream keeps a session pinned wherever the
agent moves (`context.build`); a new segment is a new upstream session, so that rule holds.
Re-pinning one upstream session instead would conflict with its pin and fold records.

### Where a command ran (R1)

Hermes's `terminal` result echoes `cwd` only when the command changed directory
(`tools/terminal_tool_result.py`, `changed_cwd`), and a per-call `workdir` never changes the
session's directory. The adapter therefore derives each call's directory itself:

1. the result's echoed `cwd` when present: where the command finished, also after `workdir=R`
   with `cd S` inside;
2. else `args.workdir` when given;
3. else the session's tracked directory (initially `s.cwd`).

The tracked directory updates only from an echoed `cwd` on a call without `workdir`, as Hermes's
own session-directory record does (`record_session_cwd`).

Local terminal backend only: a container, Modal or SSH path is not on this filesystem. The backend
is the scope-aware `TERMINAL_ENV` (`tools.terminal_scope.terminal_env("TERMINAL_ENV", "local")`,
what the terminal tool itself reads), read once per session. On any other backend no switch
happens and behaviour is today's.

### Switch rule

Checked in `transform_tool_result` (and `tool_execution` for the multimodal path) for `terminal`
only, never for web, browser or MCP results:

1. `repo_identity(dir)`. Switch only when `how` is `remote` or `path` (a git checkout) and its
   `key` differs from the current segment's key. A plain directory (home, scratch) never starts a
   segment; worktrees and clones of one repository share a key, so moving between them is no
   switch.
2. Under the session lock (R6):
   - Current segment: upstream `SessionEnd` (reason `project-switch`); its fold runs as today. An
     open turn there is closed by `SessionEnd` and is not an ended-green turn.
   - New segment id `<logical sid>~<n>`, `cwd` = that directory: append the current turn's prompt
     to the new segment's log (R4), then `SessionStart` (source `startup`), then
     `UserPromptSubmit` with the stored prompt and `turn_id`.
   - Log the new segment's catalog as segment 1 does (`offered | deny`), plus a `mask` record
     with the session's frozen deny list (R5, below).
   - Context from both new-segment events is attached to this same terminal result (`_attach`).
     `terminal` is not an untrusted tool, so it is never held.
   - Persist the segment file (below).
3. Later events (`PostToolUse`, `Stop`, usage, subagent records, `SessionEnd` at finalize) go to
   the current segment. Every writer selects the target log and appends under the same session
   lock the switch holds, so a late usage or subagent record cannot land in an already-folded log
   (R6).

### Facts block across a switch (R2)

- `s.facts_block` is the current segment's block. A destination that emits no facts sets it to
  `None` explicitly; the earlier segment's block stays in history and is not repaired.
- Presence check for repair looks for the current block's own text, raw or JSON-escaped (a block
  attached inside a JSON result is escaped), not for any facts marker. The home block surviving
  in history no longer suppresses repair of the project block, and an intact escaped block is not
  added twice.
- `llm_request` restores attached advice first, then checks facts presence on the restored
  request, so restoration and repair never both add the block.

### A segment's own tool mask (R5, engine patch P15)

Fold scores a session's tool use against the destination checkout's catalog and mask
(`fold.py` `_fold_toolprune`; `toolprune/stage.py` streak loop): every tool in that repository's
historical catalog that the session did not call gets its unused streak incremented, whether or
not this session could see it. A tool hidden by the starting notebook's deny list would therefore
count as unused in the project notebook. Catalog filtering cannot fix this, because the historical
catalog already holds the tool.

P15, mirroring P11 (`session_catalog`):

- Adapter `host.py`: `session_mask(transcript)` returns the tools of the log's `mask` record, or
  `None` when there is none (segment 1, every session today).
- `fold.py` `_fold_toolprune`: set `stage.session_mask` from it (empty when `None`).
- `toolprune/stage.py`: `self.session_mask = set()` in `ToolingStage.__init__`; in the streak
  loop a tool in `session_mask` is skipped, neither reset nor incremented. The session is no
  evidence about a tool it could not see. Fold's prompt and live counters advance only when a
  streak increments, so they skip it too (round 3, read and probed).

Engine diff of a few lines; recorded in `PATCHES.md`, `PINNED.sha256` regenerated. Sessions
without a `mask` record behave exactly as today.

### Kept per Hermes session, persisted (R3)

`<state root>/hermes-sessions/<logical sid>.segment.json`, written atomically on each switch:
`{"n", "useg", "cwd", "key", "frozen_deny"}`.

- `frozen_deny`: the tool list never changes mid-session, so the prompt cache stays warm, and a
  restart does not swap in the destination checkout's deny list.
- Compaction children: when `_logical` aliases a child to its root, it also writes
  `<child sid>.alias.json` = `{"root": ...}`. A session id seen for the first time after a
  restart is resolved through its alias file before the segment file is read, so root →
  compaction child → restart continues the current segment.
- Guard notices for the reply and subagent binding stay in memory, as today.

## Code changes (adapter only)

- `runtime.py` `_Session`: `useg`, `seg_n`, `seg_key`, `dir` (tracked directory), `prompt`,
  `turn_id`.
- Upstream payloads and log paths use `s.useg` instead of `s.sid` (`_call`, `_log_path`,
  `_child_log_path`, `_session_record`, `_session_log`). Hermes-side maps keep `s.sid`.
- `pre_llm_call`: store sanitized prompt and turn; first sight of a session id resolves alias and
  segment files.
- `_logical`: persist alias.
- `transform_tool_result` / `tool_execution`: directory derivation and switch rule.
- `post_api_request`, `_child_record`, `_observe`: append under the session lock.
- `llm_request` / `_repair_facts`: restore order and block-specific presence check.
- `host.py`: `SessionLog.mask(...)` writer and `session_mask(transcript)` reader.
- New helpers `_call_dir`, `_switch`, `_load_segment`, `_save_segment`. Expected under 220 lines
  plus tests.
- Engine P15: `fold.py` and `toolprune/stage.py`, a few lines.

No new hook, no new setting. Off switch unchanged: a disabled session never switches.

## Tests

Positive tests fail on `main` and pass after the change; regression tests already pass on `main`
and must keep passing.

Offline, through the production hook entry points:

1. Fail-first. Home-started session, `cd R && …`: R's facts on that result; at finalize segment 1
   folds into home and segment 2 into R, each exactly once; segment 2's fold counts one prompt and
   runs pruning learning.
2. Fail-first. `terminal(workdir=R)` with no directory change: switches to R; the next call with
   no `workdir` and no echo runs in the tracked directory again. `terminal(workdir=R)` running
   `cd S`: switches to S.
3. Fail-first. Home → R → S → R: four segments, each folded into its own repository.
4. Fail-first. Gateway restart mid-segment, session resumed: continues `~2` with the saved
   frozen deny list, no new `SessionStart` in home.
5. Fail-first. Root → compaction child → restart → child resumes: same logical session, `~2`.
6. Fail-first. Usage and subagent records after a switch land in the current segment; the sum over
   segments equals Hermes's usage; a usage record racing a switch is charged once.
7. Fail-first. Tool X is in R's historical catalog with unused streak 1 and hidden by the home
   deny list: after a segment-2 fold into R, X's streak is still 1 and R proposes nothing for it;
   the same holds when R has its own applied deny list. Regression: a session with no `mask`
   record scores exactly as on `main`.
8. Fail-first. Facts repair after compaction re-adds R's block once when it was dropped, does not
   add it when the escaped attachment is intact, and does not double with advice restoration.
9. Regression (passes on `main`): non-git directory, linked worktree of the current repository,
   web/browser/MCP result mentioning a repository path, non-local backend, disabled plugin: no
   switch, no segment file.

Real Hermes (harness): one API-server gateway session started in a plain directory that runs
`cd <fixture repo>` mid-turn, then `terminal(workdir=<second repo>)`; check the facts blocks land
on those results, three folds land in the right repositories, and the ledger matches Hermes.

## Limits

- Commands before the first switch stay with the starting notebook, as intended.
- File tools (`read_file`, `patch`) with paths in another repository do not switch; only where
  commands run does. Revisit only if real sessions show work done without the terminal.
- Tool pruning evidence splits across notebooks. Gateway sessions start in the home notebook,
  whose evidence now excludes tools used only inside projects, so its proposals grow. With
  `toolprune=propose` (current setting) nothing is hidden; `auto` on a gateway profile needs a
  separate decision after this lands.
- Each switch costs one fold side call on the optimizer model. A session that hops through nine
  repositories folds nine times.
- Non-local terminal backends keep one notebook per session.
- Existing home-notebook facts stay where they are; per-project notebooks fill from new sessions.

## Rollout

Branch → fail-first tests → fix → independent review until no required corrections → real-Hermes
harness → user approval → copy into the profile's plugin folder and reload plugins in the running
gateway (no restart).
