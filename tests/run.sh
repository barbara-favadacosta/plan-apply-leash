#!/usr/bin/env bash
# run.sh — the whole test suite for the plan-apply-leash harness, one command.
# Needs pyyaml + jsonschema (pip3 install --user pyyaml jsonschema). It runs:
#
#   1. The adversarial validate_plan.py suite (the bulk of this file): every
#      plan under tests/plans/ is fed through the validator, asserting both that
#      bad plans are rejected AND that each is rejected for its SPECIFIC reason:
#        exit 1 → schema failure
#        exit 2 → heuristic failure (suspicious content / injection / bidi / cross-ref)
#      Filenames decide the expected exit code, which catches the case where a
#      "heuristic" plan accidentally trips schema first and we never actually
#      test the heuristic:
#        tests/plans/bad/schema-*.yaml    → expect exit 1 (schema)
#        anything else under bad/          → expect exit 2 (heuristic)
#   2. The Python unit tests (tests/test_*.py), run via `python3 -m unittest`.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMA="${REPO_ROOT}/app/plans/schema.json"
VALIDATOR="${REPO_ROOT}/app/hooks/validate_plan.py"
GOOD_DIR="${REPO_ROOT}/tests/plans/good"
BAD_DIR="${REPO_ROOT}/tests/plans/bad"

PASS=0
FAIL=0
FAIL_NAMES=()

expected_rc_for_bad() {
  local name="$1"
  case "${name}" in
    schema-*) echo 1 ;;
    *)        echo 2 ;;
  esac
}

run_one() {
  local plan="$1"
  local expect="$2"
  local name
  name="$(basename "${plan}")"

  local tmp
  tmp="$(mktemp -t validate-test.XXXXXX)"

  python3 "${VALIDATOR}" \
    --plan "${plan}" \
    --schema "${SCHEMA}" \
    --compile-to "${tmp}" \
    >/dev/null 2>"${tmp}.err"
  local rc=$?
  rm -f "${tmp}" "${tmp}.err" 2>/dev/null || true

  if [ "${expect}" = "good" ]; then
    if [ ${rc} -eq 0 ]; then
      echo "  ✓ ${name} (exit 0, expected)"
      PASS=$((PASS+1))
    else
      echo "  ✗ ${name} expected exit 0, got ${rc}"
      FAIL=$((FAIL+1))
      FAIL_NAMES+=("good/${name}")
    fi
  else
    local want
    want="$(expected_rc_for_bad "${name}")"
    if [ ${rc} -eq "${want}" ]; then
      local label
      [ "${want}" = "1" ] && label="schema" || label="heuristic"
      echo "  ✓ ${name} (exit ${rc} = ${label}, expected)"
      PASS=$((PASS+1))
    else
      echo "  ✗ ${name} expected exit ${want}, got ${rc}"
      FAIL=$((FAIL+1))
      FAIL_NAMES+=("bad/${name}")
    fi
  fi
}

echo "→ good plans (should validate):"
for plan in "${GOOD_DIR}"/*.yaml; do
  [ -f "${plan}" ] || continue
  run_one "${plan}" "good"
done

echo
echo "→ bad plans (rejected for specific reasons):"
for plan in "${BAD_DIR}"/*.yaml; do
  [ -f "${plan}" ] || continue
  run_one "${plan}" "bad"
done

# repos.yaml `apply:` allowlist gate. These plans are schema- and heuristic-clean;
# only --apply-repos decides them, so they're tested separately from the loops.
ALLOWLIST_FIXTURE="${REPO_ROOT}/tests/fixtures/repos-allowlist.yaml"
ALLOWLIST_DIR="${REPO_ROOT}/tests/plans/allowlist"

run_allowlist_one() {
  local plan="$1"; local want="$2"; local label="$3"
  local name; name="$(basename "${plan}")"
  local tmp; tmp="$(mktemp -t validate-allow.XXXXXX)"
  python3 "${VALIDATOR}" \
    --plan "${plan}" \
    --schema "${SCHEMA}" \
    --compile-to "${tmp}" \
    --apply-repos "${ALLOWLIST_FIXTURE}" \
    >/dev/null 2>"${tmp}.err"
  local rc=$?
  rm -f "${tmp}" "${tmp}.err" 2>/dev/null || true
  if [ ${rc} -eq "${want}" ]; then
    echo "  ✓ ${name} (exit ${rc} = ${label}, expected)"
    PASS=$((PASS+1))
  else
    echo "  ✗ ${name} expected exit ${want}, got ${rc}"
    FAIL=$((FAIL+1))
    FAIL_NAMES+=("allowlist/${name}")
  fi
}

echo
echo "→ repos.yaml apply-allowlist gate (--apply-repos):"
run_allowlist_one "${ALLOWLIST_DIR}/in-allowlist.yaml"        0 "allowed"
run_allowlist_one "${ALLOWLIST_DIR}/repo-not-in-allowlist.yaml" 2 "blocked"

# Python unit tests. Each file is one tally entry: it runs via its own __main__
# (unittest.main), its assertions print on failure, and a non-zero exit (any
# failed assertion, or a missing dep) fails it.
run_unittest() {
  local file="$1"
  local name; name="$(basename "${file}")"
  local out
  if out="$(python3 "${file}" 2>&1)"; then
    echo "  ✓ ${name}"
    PASS=$((PASS+1))
  else
    echo "  ✗ ${name}"
    echo "${out}" | sed 's/^/      /'
    FAIL=$((FAIL+1))
    FAIL_NAMES+=("unit/${name}")
  fi
}

echo
echo "→ python unit tests:"
for t in "${REPO_ROOT}"/tests/test_*.py; do
  [ -f "${t}" ] || continue
  run_unittest "${t}"
done

echo
echo "──────────────────────────────────────────────"
echo "  ${PASS} passed, ${FAIL} failed"
if [ ${FAIL} -gt 0 ]; then
  printf '  failed: %s\n' "${FAIL_NAMES[@]}"
  exit 1
fi
echo "  all good — heuristics still fire on the right plans."
