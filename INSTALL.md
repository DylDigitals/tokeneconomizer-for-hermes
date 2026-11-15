# Install, use, turn off

## Requirements

- Hermes Agent 0.22.2 (the only version tested), stock, on Linux.
- Nothing else: the plugin uses its vendored copy of TokenEconomizer (standard library only) and
  the Hermes Python environment.

## Install (one profile)

Copy, settle the settings, then enable. `hermes plugins enable` loads the plugin into a running
gateway at once (no restart), so every setting below is in place before any session sees it.

Bind every `hermes` invocation below to the intended home and profile. For root/default, use
`HERMES_HOME=~/.hermes hermes --profile default` in place of `hermes`. For a named profile, use
`HERMES_HOME=~/.hermes/profiles/<name> hermes --profile <name>` and its plugin path. Set `P`
and `S` below for that same profile; the direct Python command uses those paths, not Hermes's
profile flag. Do not reuse another profile's state directory.

```bash
# 0. Copy (for a named profile, use ~/.hermes/profiles/<name>/plugins/;
#    bind every Hermes invocation as described above)
mkdir -p ~/.hermes/plugins && cp -r plugin/tokeneconomizer ~/.hermes/plugins/
```

### First settings

Out of the box the plugin behaves as upstream TokenEconomizer does. Three of those defaults
surprise people.

1. **Tool pruning applies by itself.** Default `toolprune=auto`: once a folder's sessions have
   been idle for 60 minutes, unused tools are left out of new sessions there. `propose` only
   lists them until you run `te toolprune apply`. Learned facts default to off.
2. **The state folder is shared.** Default `~/.tokeneconomizer`, the same folder Claude Code's
   and other agents' TokenEconomizer use, including one global settings file for all of them.
   Give Hermes its own; with several profiles, give each profile its own.
3. **The plugin's model calls go to your main chat model.** Proposing facts and judging a stuck
   turn each need a model call. Route them to a cheap model.

```bash
P=~/.hermes/plugins/tokeneconomizer    # a named profile: ~/.hermes/profiles/<name>/plugins/tokeneconomizer
S=~/.tokeneconomizer-hermes            # with several profiles: one folder per profile
# 2: Hermes's own state folder (read when the plugin loads)
hermes config set plugins.entries.tokeneconomizer.settings.state_root $S
# 3: a cheap model for the plugin's own calls (any provider configured in Hermes)
hermes config set auxiliary.tokeneconomizer_optimizer.provider <provider>
hermes config set auxiliary.tokeneconomizer_optimizer.model <model>
# 1: facts on, pruning as proposals only, written into that folder before the plugin loads
TOKENECONOMIZER_HOME=$S ~/.hermes/hermes-agent/venv/bin/python $P/te_entry.py config set \
  microskills=on toolprune=propose loopguard=nudge --global --host hermes
```

Hermes prints "not a recognized config key" for the two `auxiliary` lines. They are saved and
the plugin reads them.

### Enable

```bash
hermes plugins enable tokeneconomizer     # answer "no" to tool override: none is needed
hermes plugins doctor tokeneconomizer     # expect: OK ... registration passed
hermes te status                          # shows the state folder and settings in use
```

`plugins enable` prints Hermes's general "Restart the gateway" hint. A gateway that is already
running loads the plugin for new sessions without one.

`hermes te doctor` shows the route the plugin's model calls take. `hermes plugins disable` does
not unload the plugin from a running gateway: a plugin reload or a restart does.

Several profiles in one process are fine, provided each copy is byte-identical to the pin
(`_vendor/PINNED.sha256`).

Optional:
- Repository map: `hermes te config set repomap=on repomap_python=<python with aider-chat 0.86.2> --global`
- `paid_model` (upstream's model override for the fact proposer) also needs
  `plugins.entries.tokeneconomizer.llm.allow_model_override: true`.

## Use

`/te status|facts|config|toolprune|repos|repomap|doctor|fold` in a session, or `hermes te …` in a
shell. The bundled skill (`skill_view tokeneconomizer:tokeneconomizer`, or
`hermes -s tokeneconomizer:tokeneconomizer`) documents the controls.

Sessions are grouped by project: a chat starts in its start folder's notebook and moves to a git
checkout's notebook when its commands move there (`PRODUCT.md`). The pruning list itself is a
file inside each checkout, one per profile: `.tokeneconomizer/hermes-tools.local.json` for the
root profile, `.tokeneconomizer/hermes-<profile>-tools.local.json` for a named one. A profile
never hides tools another profile pruned.

**`/te` on messaging platforms.** When one gateway serves several profiles, its slash-command
picker lists the commands of the default profile's plugins only. With the plugin enabled only in
another profile, `/te` is missing from the picker; `hermes -p <profile> te …` in a shell still
works.

## With Chisle

Both plugins run together; tested in sandbox Hermes (`harness/te-test real chisle`). Chisle's
rules and the facts block both reach every request. On a long output Chisle compresses, Hermes
keeps Chisle's rewrite and drops the plugin's loop redirect from that result; the plugin re-adds
it in the outgoing request, so the model sees both. Learning reads the uncompressed result.

## Shared state with other agents

Without a `state_root` of its own, state lives in upstream's shared root `~/.tokeneconomizer`
(`TOKENECONOMIZER_HOME`), with Hermes as host `hermes` beside Claude Code and the others:

- `~/.tokeneconomizer/config.json` is global for **every** host. If another agent's install
  wrote it with everything off, Hermes starts inert. Without `--global`, `te config set` writes
  the repository's file (`repos/<repo>/config.json`), which the other hosts also read for that
  repository; `--global` changes every repository for every host.
- `~/.tokeneconomizer/bin/te` is rewritten at each host's session start, so sharing it makes the
  current launcher owner depend on the last session. Use separate roots and the profile-bound
  `hermes te` entry point. The Hermes launcher refuses `probe`, `toolprune measure` and
  `toolprune refresh-catalog`; these require `hermes te`, not the standalone launcher.
- Learned facts and tool streaks are per host (`repos/<repo>/hermes/`); other hosts' are
  untouched.

## Off and rollback

| Goal | Command | Effect |
|---|---|---|
| Pause for new sessions | `hermes te config set enabled=false --global` | No hooks, no state, full tool list; running sessions keep their frozen list |
| Give tools back | `hermes te toolprune restore` (per checkout) | Removes only the plugin's own deny entries; they are then kept, never re-proposed |
| Unload | `hermes plugins disable tokeneconomizer` | Next process loads nothing. Deny files stay in checkouts (inert); restore first to remove them |
| Remove | disable, then delete the plugin directory; optionally the `state_root` directory (or, on the shared root, `hosts/hermes` and `repos/*/hermes`) | Hermes's learned state gone; other hosts' state kept |

Unload also stops queued work: worker jobs are dropped, detached `apply-pending` waiters are
terminated, and a model call already in flight cannot be cancelled. The plugin asks Hermes for a 180 s timeout
on it, but Hermes can retry or fall back to another provider, so it can run longer. Its result
is discarded.
