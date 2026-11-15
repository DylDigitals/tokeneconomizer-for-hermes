---
name: tokeneconomizer
description: What the TokenEconomizer plugin does in this Hermes profile and how to inspect or change it with /te.
---
# TokenEconomizer (Hermes)

A plugin that cuts repeated waste per repository: it learns short facts from earlier sessions,
omits tools a repository never uses, redirects a turn stuck in a failing loop, and can add a
repository map. Upstream TokenEconomizer (`7042ea8`), adapted to Hermes hooks.

## Controls

`/te <command>` in a session, or `hermes te <command>` in a shell (acts on that directory).

| Command | Does |
|---|---|
| `status` / `status --explain` | Repository, state root, settings, sessions folded, tokens, facts, pruning, guard; explain current learning/settings state. |
| `facts` (`microskills`) | Learned facts with state: CANDIDATE, ACTIVE, INJECTED, RETIRED. |
| `config` / `config set k=v [--global]` / `config unset k` | Show or change settings; bad values are refused. |
| `toolprune` (`untool`) | Pruning evidence and proposal. |
| `toolprune apply [tool…]` / `toolprune restore [tool…]` | Write or remove deny entries for this checkout; takes effect next session. |
| `repos` | Repository identity, checkouts, sessions. |
| `repomap` / `repomap refresh` | Show or rebuild the repository map. |
| `doctor` | Health, plus the Hermes adapter: pin, captured catalog, optimizer route, limits. |
| `fold [--session ID] [--verbose]` | Fold finished sessions that missed their fold. |
| `toolprune measure [tool…]` | Prompt tokens with and without the tools (two real one-shot requests). |
| `toolprune refresh-catalog` | Record the tools a fresh session is offered (local capture, no model call). |
| `probe [--sessions N] [--model M] [--keep]` | End-to-end check on a throwaway trap repository (real model calls). |

Settings (`config set`): `microskills=off|envfacts|on`, `toolprune=off|propose|auto`,
`loopguard=off|nudge`, `repomap=off|on`, `shadow=true` (decide and log, act on nothing),
`enabled=false` (plugin does nothing for new sessions), plus upstream tunables
(`microskills_max_facts`, `toolprune_quiet_minutes`, `loopguard_aggr`, …; see `config`).

## Hermes defaults and behaviour

- Upstream defaults: micro-facts off, pruning auto, guard nudge, map off. Installation guidance
  sets facts on and pruning to `propose` before enabling. Check `status`; do not assume defaults
  describe the active profile. Settings live in the selected state root's `config.json`, with
  repository settings and `TE_*` environment overrides.
- Set `plugins.entries.tokeneconomizer.settings.state_root` to a separate directory per profile.
  Without it, state falls back to `TOKENECONOMIZER_HOME` or `~/.tokeneconomizer`, shared with
  other agents. Separate roots isolate settings, learning, accounting and launchers; they are
  not allocated automatically. Runtime-generated files are not stored in the plugin source.
- Use `hermes te` for `probe`, `toolprune measure` and `toolprune refresh-catalog`.
  `<state root>/bin/te` refuses these commands because they need the Hermes runtime. For a named
  profile bind both: `HERMES_HOME=~/.hermes/profiles/<name> hermes --profile <name> te …`.
- Facts reach the model as a delimited `<tokeneconomizer kind="session-start">` block in the
  first user message: hints learned from this repository, not user instructions. Hermes
  replays it on resume; after compaction it is put back once.
- Pruning omits denied tools from the model request (`llm_request` middleware) from the next
  session, subagents included; a running session keeps its tool list. `tool_search` results
  and the deferred listing leave them out, and `tool_describe`/`tool_call` refuse them. The
  list is per checkout and profile: `.tokeneconomizer/hermes-tools.local.json` for root/default,
  `.tokeneconomizer/hermes-<profile>-tools.local.json` for a named profile (git-excluded when
  applicable). Learning is grouped by project within that profile's state root; clones and
  worktrees share learning, not their checkout-local deny files.
- Core tools (terminal, files, skills, memory, delegation, web, both `tool_search` and
  `hermes_tool_search`) are never proposed. `toolprune_protect` adds names or glob patterns to
  the pruning protection set. This protects against TE pruning, not other compression or an
  unavailable service. Restore existing denials explicitly; adding protection is not a restore
  command. `/te toolprune restore <tool>` then `/new` makes a restored tool available again.
- Subagents' tool use and token usage count toward the session that spawned them.
- Plugin notices (facts added, tools applied or proposed, still stuck) appear once at the end
  of that turn's reply, prefixed `TokenEconomizer`.
- Guard: after the repository's usual turn length is learned (5 normal turns), a turn that
  keeps failing the same way gets one redirect appended to a tool result (image results
  included); if it is still stuck, a notice is added to the reply. It never stops the agent:
  press Ctrl+C to interrupt it.
- Model calls (fact proposer every 3 sessions with `microskills=on`, stuck judge) use Hermes's
  own route via auxiliary task `tokeneconomizer_optimizer`, with secrets redacted; spend shows
  in `status`. If that route says it is not signed in, the calls pause and the next session
  says so once; `config set microskills=…` (or `loopguard=…`) tries again. `paid_model` needs
  `plugins.entries.tokeneconomizer.llm.allow_model_override`.

## Hermes limits

The `codex_app_server` runtime bypasses request middleware: no pruning there. Off:
`/te config set enabled=false --global`, or `hermes plugins disable tokeneconomizer` (run
`/te toolprune restore` first to remove the checkout's deny file).
