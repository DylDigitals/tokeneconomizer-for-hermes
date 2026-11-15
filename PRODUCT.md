# TokenEconomizer on Hermes: what it does

A Hermes plugin built around Microsoft's TokenEconomizer engine, pinned with documented host/safety patches. The four mechanisms below come from upstream. Our adapter connects them to Hermes chats, adds project grouping as work moves between repositories, and isolates each profile's hidden-tool list. Microsoft maintains the upstream engine; this repository maintains the Hermes adapter and its patches.

Think of it as a notebook and a tidy-up routine for the agent. The notebook holds what earlier sessions had to rediscover. The tidy-up removes tools nobody uses and taps the agent on the shoulder when it repeats a failing step.

## How it works

**Where the data lives.** Local state in one configured directory per profile. Without a `state_root` setting, the plugin falls back to `~/.tokeneconomizer` (or `TOKENECONOMIZER_HOME`), shared with other agents. `INSTALL.md` explains how to separate it. Settings, learned facts and usage records live there; each checkout holds that profile's hidden-tool list. The background model receives redacted inputs, and injected facts also reach the chat model. Redaction does not classify private data.

**What it watches.** Every session in the Hermes profile where it is enabled: terminal sessions, gateway chats such as Discord, scheduled jobs and one-shot commands. For each session it logs the commands run, the tools used, whether each call failed, and the tokens spent. Sub-agent work counts toward the session that started it.

**How it groups sessions.** By project, which it calls a "repository". A chat starts in the notebook of its start folder (for gateway chats, the home folder). When the agent's commands move into a git checkout, the chat continues in that project's notebook from that point, so what it learns there is filed under the project. Moving into a plain folder (not a git checkout) never switches notebooks, and clones and worktrees of one repository share one.

**When it learns.** After a session ends, in the background. The live session is never paused for it.

**Its own model calls.** Two jobs need a model: proposing facts and judging whether a turn is stuck. Secrets are redacted first, and their cost is included in the plugin's own report. They go to the model named in Hermes's `auxiliary.tokeneconomizer_optimizer` setting; with none set, to your main chat model, so set a cheap one (`INSTALL.md`). "The cheap model" below means that model.

**If it breaks.** A failure inside the plugin is logged and the session carries on as if the plugin were not installed.

## The four features

Each heading gives the setting out of the box. `INSTALL.md` recommends facts on and pruning as proposals only.

### 1. Learned facts (default: off)
- **Problem:** each new session rediscovers the same things: which command works here, where files live.
- **How a fact gets in:** two sources. A failure that repeats across sessions (free, no model). And every 3 sessions, the cheap model reviews recent sessions and proposes facts. A fact is used only after 3 sessions confirm it.
- **How it reaches the agent:** a short labelled block at the start of the first message of each session, marked as hints, not instructions. If Hermes compacts the chat and drops the block, it is put back once.
- **How a fact leaves:** later sessions that contradict it retire it automatically.
- **Guard rail:** a fact written by the model passes three word checks before it is used. It must be one line of at most 200 characters. It is rejected if it contains `<`, `>`, `{`, `}`, a code fence, a web address, or a word from a fixed list (such as ignore, override, send, upload, curl, password, token, secret, sudo). At least one of its words must also appear in the commands run in the sessions it cites. These checks match text; they do not understand it. Other markup and other wording pass, and a wrong detail picked up from command output can still become a fact. `/te facts` shows every fact in use.

### 2. Tool pruning (default: automatic)
- **Problem:** every tool's description is sent with every request, used or not.
- **How it works:** a tool unused over 10 prompts across at least 2 sessions, or over 5 sessions, is proposed for removal. Core tools (terminal, files, skills, memory, delegation, web, tool search) are never proposed.
- **Modes:** `auto` (the default) applies the list by itself once the folder's sessions have been idle for an hour. `propose` lists candidates and removes nothing until `/te toolprune apply` is run.
- **Effect of applying:** new sessions in that checkout and profile no longer see those tools. Root/default uses `.tokeneconomizer/hermes-tools.local.json`; named profiles use `.tokeneconomizer/hermes-<profile>-tools.local.json`. A running session keeps its tool list, even when work moves to another repository. Hidden tools are also left out of tool search, and calls to them are refused.
- **Never-hide list:** `toolprune_protect` adds tool names or patterns to the pruning protection set. This protects against TE's pruning, not other compression or a disconnected service. It does not restore an already hidden tool.
- **Undo:** `/te toolprune restore <tool>`, then `/new`. Restored tools are never proposed again for that checkout/profile.

### 3. Loop guard (default: on)
- **Problem:** a turn that keeps failing the same way burns tokens.
- **How it works:** the plugin first learns how long a normal turn is in this folder (5 turns that ended well). When a turn runs past that and keeps hitting the same failure, the cheap model judges whether it is stuck. At 70% confidence or more, the agent gets one short redirect attached to a tool result, at most once per turn.
- **If still stuck:** a notice is added to the reply. The plugin never stops a turn.

### 4. Repository map (default: off)
- A compact map of a code project's files, added once per session. Needs an extra Python package (aider). It only helps coding sessions inside a project folder.

## What shows up in chat

- A `<tokeneconomizer kind="session-start">` block at the start of a session: the learned facts.
- One line at the end of a reply, prefixed `TokenEconomizer`, when something happened (facts added, tools proposed, still stuck).
- Nothing else. Most turns show no trace of it.

## Controls

`/te <command>` in a chat, or `hermes te <command>` in a terminal (`hermes -p <profile> te …` for a named profile).

- `status`: settings, sessions learned from, tokens seen, the plugin's own overhead, facts, pruning, guard.
- `facts`: every learned fact and its state.
- `toolprune`: the pruning proposal. `toolprune apply` and `toolprune restore` change it.
- `config set <name>=<value>`: turn a feature on or off (`microskills`, `toolprune`, `loopguard`, `repomap`).
- `doctor`: health check.
- Pause everything for new sessions: `/te config set enabled=false --global`.

## What is gained, and what is not proven

- **Historical sandbox measurement:** prompt tokens 20% lower and total tokens 10% lower with the plugin on, after counting its own model calls. Runs without the plugin differed by 13%, so 10% is a small margin. Hiding 2 tools saved 376 tokens per request in that setup.
- **Installed-profile request measurement:** the 2026-11-14 Opus comparison saved 3,154 input tokens per request (4.5%) with 89 approved hidden tools. This measures request size, not completed-task savings.
- **Not yet established:** controlled net savings on real work, faster task completion, savings on other models, or the same percentage on other profiles. `status` reports overhead, not a counterfactual comparison.
- **Cost:** the background model calls and the facts block also consume tokens. See [RESULTS.md](RESULTS.md#verified-checkpoint-2026-11-14) for the checkpoint and measurement limits.

## Limits

- On the `codex_app_server` runtime Hermes bypasses the request hook the plugin needs: no pruning and no repair of dropped facts there. Other features still work.
- Hermes tells the model not to obey web, browser and MCP results, so a redirect decided on one waits for the next ordinary tool result in the same turn. If the turn has none, that redirect is dropped.
- The loop guard reads the first 20,000 characters of a tool result (Microsoft's code, the same on Claude Code). A failure whose error text comes after that is not seen.
- When Chisle compresses the same tool result, Hermes keeps Chisle's version; the plugin re-adds its redirect on the way to the model.
- Microsoft's code here is a pinned copy of its head on 2026-11-12 (`7042ea8`), with 15 Hermes patches (`plugin/tokeneconomizer/_vendor/PATCHES.md`). Microsoft now also ships its own experimental Hermes plugin with a smaller scope (`README.md`).
- Tested on Hermes Agent 0.22.2 on Linux only.

## Tests

Run from the checkout.

1. `harness/te-test`: the plugin's real code against a fake model. Recent recorded runs took 12 to 15 seconds, with no model calls. Shows that the tested logic is intact.
2. `harness/te-test live`: check of the installed plugin that changes no settings or learned facts: enabled, identical to the checkout's code, no errors logged, model calls succeeding, every recent Discord chat followed. About 10 seconds, free. Shows what is true on the running system, which no sandbox can.
3. `harness/te-test real [feature…]`: real Hermes sessions in throwaway sandboxes on a cheap test model you name (`README.md`, Tests). Proves each feature end to end with a real model. One feature takes 5 to 15 minutes, all of them 30 to 45. `harness/te-test list` names the features. A failure here can be the test model's behaviour rather than the plugin: read the evidence file it prints before treating it as a bug.

Technical detail: `MATRIX.md` (every capability and its proof), `INSTALL.md` (install, settings, removal), `RESULTS.md` (what was tested and the results).
