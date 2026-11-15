# Hermes patches to vendored TokenEconomizer

Upstream: `microsoft/TokenEconomizer` @ `7042ea89dd5e5ecbc67ad865ad6e256e61be8bf0` (Apache-2.0, `LICENSE`).
`UPSTREAM.sha256` = original bytes. `PINNED.sha256` = shipped bytes. Every patched line carries `[hermes patch Pn]`.

- **P1** `plugin/hook.py` — `run(..., context_cls=None)`: an in-process host passes its own `LiveContext` subclass.
- **P2** `plugin/context.py`, `plugin/fold.py`, `plugin/judge.py` — `LiveContext.side_runner()`; fold and judge call it instead of the process-wide `sidecall.runner_for`. Fixes: two profiles in one process sharing one optimizer route.
- **P3** `plugin/controller.py` — `_cmd` honours `host.command_prefix` (Hermes: `/te`).
- **P4** `plugin/fold.py` — a waiting auto-apply deny list is dropped, not written, when the plugin was switched off.
- **P5** `microskills/stage.py`, `microskills/distiller.py` — proposer cadence per distiller instance, not a rebound module global.
- **P6** `microskills/stage.py`, `microskills/distiller.py` — host admission check (`adapter.paid_fact_filter`) for paid facts on admission and on every render; a runner exception with `defer=True` re-raises (fold stays pending); other runner failures are a failed tick.
- **P7** `microskills/distiller.py` — contradiction regex compiled with `re.S` (blob is commands + `\n` + output).
- **P8** `microskills/stage.py` — combined free + paid block capped; paid lines about a subject whose free fact retired are dropped.
- **P9** `plugin/fold.py`, `plugin/accounting.py` — resumed folded session charges its later requests as `usage_delta` ledger records (no relearning).
- **P10** `loopguard/judge.py` — billed empty nudge-writer reply still charges recovered tokens.
- **P11** `plugin/fold.py` — host may supply `adapter_cls` and a per-session tool catalog.
- **P12** `plugin/cli.py` — `toolprune` show lists how long each tool has gone unused for host `hermes` too (upstream: Claude only; Hermes records the same per-tool streaks).
- **P13** `plugin/denylist.py` — settings writes go through a fresh exclusively created temporary file (`tempfile.mkstemp`), not the fixed `<file>.te.tmp`, which a checkout could commit as a symlink to another file and have truncated. Upstream has the same code at its 2026-11-12 head.
- **P14** `plugin/repomap.py` — the aider probe (`python -c`) runs from this plugin's directory, not the inherited working directory, so a checkout's `aider/` package is never imported. A relative interpreter path is resolved against the caller's directory first, as before. Upstream has the same code at its 2026-11-12 head.
- **P15** `plugin/fold.py`, `toolprune/stage.py` — host may supply a per-session tool mask (`session_mask`); a masked tool's unused streak is neither reset nor advanced. A Hermes session that moves to another repository (a later segment) keeps the deny list it started with, so its hidden tools are no evidence in the destination.

Re-vendoring: apply upstream, re-apply these, regenerate both hash files, rerun `tests/`.
For an overlapping fold rewrite, preserve the call sites as well as helper definitions: P9 must
charge from `fold_locked` and persist a usage snapshot; P11 must construct the host adapter
with the session catalog. `tests/test_engine_update.py` fails when either wiring is dropped.
Do not count retained helper bodies or a green unrelated suite as preservation proof.
