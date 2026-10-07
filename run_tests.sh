#!/usr/bin/env bash
# Runs every check this repository has:
#   * Python unit + integration tests (unittest)
#   * JavaScript unit tests for the sidebar logic (node --test)
#   * shell and JavaScript syntax checks
#
# The Python integration tests need the websockify module and Node's `ws`
# package. Missing pieces are reported as skips, not failures.
set -uo pipefail

cd "$(dirname -- "${BASH_SOURCE[0]}")"
status=0

PYTHON=python3
if [[ -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
fi

printf '== Python tests (%s) ==\n' "$PYTHON"
"$PYTHON" -c 'import websockify.websocketproxy' 2>/dev/null \
  && printf 'websockify module: available\n' \
  || printf 'websockify module: MISSING (server integration tests will skip)\n'
"$PYTHON" -m unittest discover -s tests -p 'test_*.py' -v || status=1

printf '\n== JavaScript tests ==\n'
if command -v node >/dev/null 2>&1; then
  node --test tests/test_core.mjs || status=1
else
  printf 'node: MISSING (javascript tests skipped)\n'
fi

printf '\n== Syntax checks ==\n'
for script in *.sh; do
  bash -n "$script" && printf 'ok   %s\n' "$script" || { printf 'FAIL %s\n' "$script"; status=1; }
done
"$PYTHON" -m compileall -q automation >/dev/null && printf 'ok   automation/*.py\n' \
  || { printf 'FAIL automation/*.py\n'; status=1; }
if command -v node >/dev/null 2>&1; then
  tmp=$(mktemp --suffix=.mjs)
  cp automation/static/automation.js "$tmp"
  node --check "$tmp" && printf 'ok   automation/static/automation.js\n' \
    || { printf 'FAIL automation/static/automation.js\n'; status=1; }
  rm -f "$tmp"
fi

printf '\n%s\n' "$([[ $status -eq 0 ]] && echo 'ALL CHECKS PASSED' || echo 'SOME CHECKS FAILED')"
exit "$status"
