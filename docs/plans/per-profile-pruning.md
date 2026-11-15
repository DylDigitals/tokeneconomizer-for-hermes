# Per-profile pruning lists

Status: implemented and reviewed; final revision `d16581f` passed review round 4 on 2026-11-14.
Merged into `main` and installed in one installation's named profile. Its 89 approved entries and ownership keys
were migrated to the named-profile file; a fresh request capture confirmed the denials.
Other profiles have not been rolled out. See [the verified checkpoint](../../RESULTS.md#verified-checkpoint-2026-11-14).
This document records the completed design and migration procedure, not pending build work.

## Problem

The pruning list was one file per checkout, `<checkout>/.tokeneconomizer/hermes-tools.local.json`,
read by every profile running the plugin there. That profile's home-folder list hides 89 tools,
including Firecrawl crawl/map and ComfyUI training; any profile enabled later in the home folder
would have lost them. A separate `state_root` does not help: the file lives in the checkout.

## Rule

- The root profile, and a home `hermes_constants.profile_name_for_home` does not recognise, keep
  `.tokeneconomizer/hermes-tools.local.json`.
- A named profile uses `.tokeneconomizer/hermes-<profile>-tools.local.json`.
- Each profile has its own state root (INSTALL rule).

## How

- One `contextvars.ContextVar` names the current file. It is created once and stored on the shared
  vendored module (`denylist._hermes_deny_var`), so every profile's plugin copy and a reloaded copy
  set the same variable. `denylist._SETTINGS` is wrapped once so `"hermes"` resolves through it;
  only `denylist.py` reads `_SETTINGS`.
- The runtime binds its file at the three places this adapter enters upstream code: `_call`
  (every hook event: session start notices, session end, exit finalize), `_run_job` (folds and
  judge jobs, including ones queued at exit), and `controls.run` for the Hermes drivers. Hook
  registrations stay unwrapped.
- Subprocesses (`te` commands, detached jobs) get `TE_HERMES_DENY_FILE` from `Runtime.env()`;
  the `<state root>/bin/te` launcher bakes it in. `te_entry.py` accepts only a path of the form
  `.tokeneconomizer/hermes*-tools.local.json`.
- `_deny_list` reads `join(checkout, runtime.deny_rel)`.

## Migration of an existing named-profile install

Quiesce first (no TE command running). Back up the deny file and the store's
`untool_applied.json`. Move `<checkout>/.tokeneconomizer/hermes-tools.local.json` to the profile's
name, and in the store move the `by_path` and `kept` entries from the old canonical path to the
new one (removing the old keys). For a git checkout, add the new name to `.git/info/exclude`.
`toolprune restore` + `apply` is not a substitute: restore marks tools kept, never proposed again.

## Tests

`tests/test_profiles.py` (12 failed before the change): two separately loaded plugin copies on one
checkout prune from their own files; both copies share one variable and upstream hook calls see
each profile's file; folds queued at session end run in their profile; the root profile keeps the
original name; subprocess env and launcher carry the file; `--help` runs nothing; `--sessions=N`
and `--model=M` forms work; bad options are refused before any session.
