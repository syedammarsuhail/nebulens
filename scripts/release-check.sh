#!/usr/bin/env bash
# Oscilyx release gate: run before every push or release. Stops at the first failing check and exits non-zero.
#
#   bash scripts/release-check.sh            # from the package / repository root
#
# Checks:
#   1. tests          the full pytest suite (SQLite; set OSCILYX_TEST_PG_URL to also prove Postgres)
#   2. dependencies   pip-audit against requirements.lock (the exact versions the image installs): no known CVEs
#   3. code scan      bandit on the Python code, medium severity and above. B608 (SQL built with f-strings) is skipped because
#                     every interpolated value was reviewed by hand: they are table / column names from fixed lists and
#                     `?` placeholders, never request data. Any other finding fails; a reviewed one needs a "# nosec <id> - reason".
#   4. secrets        no private keys, cloud keys, tokens, license secrets or known demo tokens anywhere in the tree
#   5. paid code      no proprietary enterprise/ module or key generator in the tree
#   6. helm           lint, and every values overlay renders (with a real token)
#   7. ui             every UI script parses
#
# Needs: python 3.10+, helm, node. pip-audit and bandit are installed into a throwaway virtualenv under .release-check/.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# Use a Python that can run the tests (python3 and python can be different installs).
PY=""
for c in ${PYTHON:-} python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import pytest, fastapi" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "No Python with pytest and the app's requirements found: pip install -r requirements-dev.txt (or set PYTHON=...)" >&2; exit 1; }
step() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nRELEASE CHECK FAILED: %s\n' "$*" >&2; exit 1; }

step "1/7 tests"
OSCILYX_CACHE_TTL=0 "$PY" -m pytest tests -q -p no:cacheprovider || fail "tests"

step "2/7 dependencies (pip-audit)"
VENV=.release-check/venv
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
VPY="$VENV/bin/python"; [ -x "$VPY" ] || VPY="$VENV/Scripts/python.exe"
"$VPY" -m pip install -q --disable-pip-version-check pip-audit bandit >/dev/null
"$VPY" -m pip_audit -r requirements.lock --progress-spinner off || fail "a dependency has a known vulnerability"

step "3/7 code scan (bandit, medium+)"
"$VPY" -m bandit -q -r server.py integrations.py scripts demo --severity-level medium --skip B608 || fail "bandit finding"

step "4/7 secrets"
# Only what ships: tracked files in a git checkout, otherwise every file except local logs. This script is skipped because it
# contains the patterns it looks for.
PATTERN='AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|github_pat_|sk-ant-[A-Za-z0-9_-]{10,}|xox[abp]-[0-9A-Za-z-]{10,}|_LICENSE_SECRET|demo-token-2024'
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  FILES=$(git ls-files | grep -v '^scripts/release-check.sh$' || true)
else
  FILES=$(find . -type f -not -path './.release-check/*' -not -name '*.log' -not -path './scripts/release-check.sh' | sed 's#^[.]/##')
fi
HITS=$(printf '%s\n' "$FILES" | tr '\n' '\0' | xargs -0 grep -IlE "$PATTERN" 2>/dev/null || true)
[ -z "$HITS" ] || { printf '%s\n' "$HITS"; fail "possible secret in the files listed above"; }
echo "no secrets found"

step "5/7 paid code"
if [ -d enterprise ] || find . -path ./.git -prune -o -name 'keygen.py' -print | grep -q .; then fail "proprietary enterprise module present"; fi
echo "none present"

step "6/7 helm"
helm lint helm/oscilyx --set auth.token=release-check-token-0123456789abcdef >/dev/null || fail "helm lint"
for f in helm/oscilyx/values-*.yaml; do
  helm template oscilyx helm/oscilyx -n observability -f "$f" --set auth.token=release-check-token-0123456789abcdef >/dev/null || fail "helm template $f"
done
echo "chart lints and every overlay renders"

step "7/7 ui"
for f in static/*.js; do node --check "$f" || fail "$f does not parse"; done
echo "all UI scripts parse"

printf '\nRELEASE CHECK PASSED\n'
