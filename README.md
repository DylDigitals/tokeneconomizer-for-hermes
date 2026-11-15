# TokenEconomizer for Hermes Agent

A Hermes Agent plugin that cuts repeated token waste. It runs Microsoft's
[TokenEconomizer](https://github.com/microsoft/TokenEconomizer) inside Hermes through Hermes's
plugin hooks:

- **Learned facts:** short facts from earlier sessions in a folder (which command works, where
  files live) are given to later sessions there.
- **Tool pruning:** tools a folder never uses are left out of its requests.
- **Loop guard:** a turn that keeps failing the same way gets one redirect. It is never stopped.
- **Repository map:** an optional compact map of a code project, added once per session.

`PRODUCT.md` explains each feature in plain words.

## Status

Implementation `d16581f` is reviewed and in daily use on one installation's Discord gateway: the
offline tests and five installed-plugin checks passed on 2026-11-14. Those results do not prove every profile or
provider. [RESULTS.md](RESULTS.md#verified-checkpoint-2026-11-14) owns the verified checkpoint,
measurement limits and remaining rollout work.

- Tested on Hermes Agent 0.22.2 on Linux only. Stock Hermes: no Hermes source edits, no extra
  packages.
- TokenEconomizer's engine is pinned at `7042ea8` (upstream head checked 2026-11-12), with
  15 host/safety patches listed in `plugin/tokeneconomizer/_vendor/PATCHES.md`. Updates are
  reviewed and tested explicitly; this copy never follows upstream automatically.
- Upstream's historical-session import is not supported by this adapter. Existing session
  learning continues normally; no old history is imported automatically.
- Not affiliated with or endorsed by Microsoft or Nous Research.

### Upstream's own Hermes plugin

Upstream ships an experimental Hermes plugin (`plugin/hermes` in its repository). It covers learned
facts and the loop guard on the classic CLI and one-shot runs; its README says it was tested on
Hermes 0.22.5 only. Hermes plugin hooks also run in the gateway, but upstream's plugin does not
follow gateway sessions, sub-agents or scheduled jobs, and it does not prune tools.

This plugin uses the rest of Hermes's plugin interface. It adds tool pruning, the repository map and
a `/te` command inside chats, and it covers gateway sessions (Discord, API server) and sub-agents.
It also follows scheduled jobs, which are not tested yet. The gap is reported upstream in
[microsoft/TokenEconomizer#232](https://github.com/microsoft/TokenEconomizer/issues/232). If
upstream's coverage is enough for you, use upstream's: it is maintained with the rest of
TokenEconomizer.

## Install

```bash
git clone https://github.com/DylDigitals/tokeneconomizer-for-hermes.git && cd tokeneconomizer-for-hermes
mkdir -p ~/.hermes/plugins && cp -r plugin/tokeneconomizer ~/.hermes/plugins/
```

Then do **First settings** in `INSTALL.md` before `hermes plugins enable`: enabling loads the
plugin into a running gateway at once, and three defaults are worth changing first. Tool pruning
applies by itself, the state folder is shared with other agents' TokenEconomizer, and the
plugin's own model calls go to your main chat model.

## Documents

- `PRODUCT.md`: what each feature does, what shows up in chat, the controls, the limits.
- `INSTALL.md`: install, first settings, running with other agents, turning it off, removal.
- `MATRIX.md`: every upstream capability, how it maps to Hermes, and how it was checked.
- `RESULTS.md`: what was tested, on which versions, with which results.

## Tests

```bash
harness/te-test                 # real code, fake model: recent runs 12-15 s, no model calls
harness/te-test live [profile]  # health check of an installed copy (default: root profile), changes no settings: ~10 s, free
harness/te-test list            # the features the real-session test covers
harness/te-test real [feature…] # real Hermes sessions in throwaway sandboxes: 30-45 min for all
```

All of them use the Hermes install at `~/.hermes/hermes-agent`; set `HERMES_SRC` if its source
directory is elsewhere.
The first one needs `pytest` in the Hermes Python environment (Hermes's `dev` extra).

`real` makes model calls. Point it at any OpenAI-compatible endpoint with a cheap model. Take
the key from your secret store for that one command; never type it, paste it into a chat or save
it in a file yourself. Use a short-lived key scoped to the test model.

```bash
TE_TEST_BASE_URL=https://…/v1 TE_TEST_MODEL=<model> \
  TE_TEST_API_KEY="$(<command that prints the key>)" harness/te-test real
# Optional: TE_TEST_PROVIDER=<configured-provider-name> (default: te-test)
```

It builds each sandbox under `sandbox/` in this checkout with an empty home folder, copies only
that key into it, and never writes to your real Hermes home. The key stays in each sandbox's
`.env` (mode 600) after the run; delete `sandbox/` when you are done. The checks were tuned on
`deepseek-v4.1-flash`; with another model a failure can be the model's behaviour, so read the
log it prints before treating one as a bug. Two features need extra setup and are skipped
without it: `repomap` (`TE_REPOMAP_PYTHON`: a Python with `aider-chat` 0.86.2) and `chisle`
(`TE_CHISLE_PLUGIN`: an installed Chisle Hermes plugin directory, plus `node`).

## Layout

- `plugin/tokeneconomizer/`: the plugin. `_vendor/` holds upstream's code, its license, the
  patch list and checksums of both the original and the shipped bytes.
- `tests/`: offline tests (real vendored code, fake model).
- `harness/`: the test entry point `te-test` and the sandbox scripts behind `real`.

## License

Apache-2.0 (`LICENSE`), the same license as upstream TokenEconomizer. `NOTICE` names the
upstream code this repository includes.
