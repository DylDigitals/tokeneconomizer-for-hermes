#!/usr/bin/env bash
# Matched arms, same tasks/model/baseline: harness/compare.sh <arm: on|off> [rep]
set -uo pipefail
W="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$W"
ARM=$1; REP=${2:-1}
if [ "$ARM" = on ]; then R=$(harness/new-root "cmp-on-$REP"); else R=$(harness/new-root "cmp-off-$REP" --no-plugin); fi
harness/make-repo "$R/repo" >/dev/null
sed -i 's/max_turns: 30/max_turns: 60/' "$R/home/.hermes/config.yaml"
if [ "$ARM" = on ]; then
  (cd "$R/repo" && HX_ROOT="$R" "$W/harness/hx" te config set microskills=on toolprune=auto toolprune_quiet_minutes=0 toolprune_sessions=3 toolprune_min_sessions=1 toolprune_evidence=1 --global >/dev/null)
fi
T="Run the project's tests the way the README says. If that command is not available here, run them with python3 -m unittest -q instead. Reply with the number of tests that passed, nothing else."
L="Team retry policy for the transient-timeout check: run exactly the command ./scripts/remote-check.sh (nothing appended, no pipes, no echo) once per tool call, and keep re-running it until it exits 0 or 25 attempts have been made. Do not read or modify the script. Then report the final status in one line."
harness/turn "$R" ctl "What is 2+2? Reply with the number only." | head -1
for i in 1 2 3 4 5 6; do harness/turn "$R" t$i "$T" | head -1; done
harness/turn "$R" loop "$L" | head -1
harness/turn "$R" t7 "$T" | head -1
echo "$R"
