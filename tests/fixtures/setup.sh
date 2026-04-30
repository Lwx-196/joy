#!/usr/bin/env bash
# Stage the fixture database at the project root and patch case 126's abs_path
# so it points at the in-repo fixture tree.
#
# Why patching is needed: case rows record absolute paths to user-machine case
# directories (cases.abs_path TEXT NOT NULL UNIQUE). The fixture DB was cloned
# from a developer machine where case 126 lives at a Chinese-named path under
# ~/Desktop. CI runners can't reproduce that path, so we rewrite it to a
# repo-relative location whose `.case-layout-output/fumei/tri-compare/render/`
# subtree is committed alongside this script.
#
# Idempotent: safe to re-run. The UPDATE is a no-op if abs_path already matches.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
FIXTURE_DB="${SCRIPT_DIR}/case-workbench.test.db"
TARGET_DB="${REPO_ROOT}/case-workbench.db"
CASE_126_FIXTURE="${SCRIPT_DIR}/cases/case-126"

if [[ ! -f "${FIXTURE_DB}" ]]; then
  echo "ERROR: fixture DB missing: ${FIXTURE_DB}" >&2
  exit 1
fi
if [[ ! -d "${CASE_126_FIXTURE}/.case-layout-output/fumei/tri-compare/render/.history" ]]; then
  echo "ERROR: case 126 fixture tree missing: ${CASE_126_FIXTURE}" >&2
  exit 1
fi

cp "${FIXTURE_DB}" "${TARGET_DB}"
sqlite3 "${TARGET_DB}" "UPDATE cases SET abs_path = '${CASE_126_FIXTURE}' WHERE id = 126;"

ROW_COUNT="$(sqlite3 "${TARGET_DB}" "SELECT COUNT(*) FROM cases WHERE id = 126 AND abs_path = '${CASE_126_FIXTURE}';")"
if [[ "${ROW_COUNT}" != "1" ]]; then
  echo "ERROR: case 126 abs_path patch did not apply (got ${ROW_COUNT} rows)" >&2
  exit 1
fi

echo "Fixture staged: ${TARGET_DB}"
echo "case 126 abs_path -> ${CASE_126_FIXTURE}"
