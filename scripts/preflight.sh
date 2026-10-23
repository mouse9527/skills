#!/bin/bash
# Gate before anything becomes public (push to this public repository). Run in a trusted environment only.
#   scripts/preflight.sh [BASE]        BASE defaults to origin/main (what is already public)
# 1. working tree clean (what is checked = what is committed)  2. scripts/validate.py  3. tests
# 4. scripts/pii_scan.py: final tree + every commit in BASE..HEAD with the generic rules and the PRIVATE term list
#    ($SKILLS_PII_TERMS, default ~/.config/assistant-config/pii-terms.txt; 0600; outside every repository).
#    Missing / too open / empty term list = failure. Nothing here prints a matched text or a term.
# On success records HEAD in .git/preflight-ok; the pre-push hook (scripts/hooks/pre-push) refuses anything else.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
PY=${PYTHON:-python3}
BASE=${1:-origin/main}
TERMS=${SKILLS_PII_TERMS:-$HOME/.config/assistant-config/pii-terms.txt}

git fetch -q origin
if [ -n "$(git status --porcelain)" ]; then
  echo "preflight: working tree not clean (commit or stash first)" >&2; exit 1
fi
"$PY" scripts/validate.py
"$PY" -m unittest discover -s tests -q 2>&1 | tail -3
"$PY" scripts/pii_scan.py --terms "$TERMS" --range "$BASE..HEAD"
git rev-parse HEAD > "$(git rev-parse --git-dir)/preflight-ok"
echo "preflight ok: $(git rev-parse HEAD)"
