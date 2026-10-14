#!/usr/bin/env bash
# Files the nightly run's findings as GitHub issues: one per signature, never
# the same one twice. Run by .github/workflows/nightly.yml after each job.
#
#   ci/file-issues.sh SCOPE ITEMS ARTIFACT
#
#   SCOPE      what ran, for the title: process/pinned, process/master, sanitized
#   ITEMS      a file with one signature per line: a findings/ directory name
#              (crash/84e0cb6e67; its files are under $OUT/findings/<name>), or
#              any other text (a soundness failure line), filed as it stands
#   ARTIFACT   the run artifact that holds the reports
#
# Environment: GH_TOKEN, GITHUB_REPOSITORY, GITHUB_RUN_ID and GITHUB_SERVER_URL
# (set by Actions); COMPILER_SHA (the turflang commit under test); OUT (the
# report directory, default ci-out); MAX_NEW (default 10).
#
# The title is "Nightly fuzz: SIGNATURE", without SCOPE, so the pinned,
# master and sanitized jobs share one issue per signature. A signature with an OPEN issue
# of exactly that title gets a comment (date, run, compiler) instead of a new
# issue, so a finding not yet committed under findings/ is not filed again
# every night. At most MAX_NEW issues are created per call; the signatures
# past that go into one overflow issue that lists them. Labels (ci/labels.sh):
# fuzz-finding, kind:<class> and compiler:<scope>; a signature seen again by
# another job gains that job's compiler label. A create that fails with the
# labels is retried without them. Exit status is 0.
set -uo pipefail
scope=$1; items=$2; artifact=$3
OUT=${OUT:-ci-out}
MAX_NEW=${MAX_NEW:-10}
run="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
today=$(date -u +%F)
sha=${COMPILER_SHA:-unknown}
[ -s "$items" ] || exit 0
. "$(dirname "$0")/labels.sh"

create() { # TITLE BODYFILE LABELS
  gh issue create --repo "$GITHUB_REPOSITORY" --title "$1" --body-file "$2" --label "$3" \
    || gh issue create --repo "$GITHUB_REPOSITORY" --title "$1" --body-file "$2"
}

# The number of the open issue titled exactly $1, if any.
open_issue() {
  gh issue list --repo "$GITHUB_REPOSITORY" --state open --limit 100 --search "\"${1//\"/}\" in:title" \
    --json number,title 2>/dev/null | jq -r --arg t "$1" 'map(select(.title == $t)) | .[0].number // empty'
}

body() { # SIGNATURE: the issue body
  local sig=$1 d="$OUT/findings/$1" f
  echo "Nightly fuzz (**$scope**) found a new signature: \`$sig\`."
  echo
  echo "- run: $run (artifact \`$artifact\`, 30 days)"
  echo "- compiler: turflang \`$sha\`, $today"
  echo
  echo "Presume a generator or evaluator bug first for a wrong answer (\`gen/trace.py SEED\`). A real compiler bug gets a fix and a pinned test in turflang, and its directory committed under findings/."
  if [ -d "$d" ]; then
    [ -f "$d/report.md" ] && { echo; head -c 2000 "$d/report.md"; echo; }
    [ -f "$d/meta.json" ] && { echo '```json'; head -c 1500 "$d/meta.json"; echo; echo '```'; }
    f="$d/min.tr"; [ -f "$f" ] || f="$d/prog.tr"; [ -f "$f" ] || f="$d/input.tr"
    [ -f "$f" ] && { echo '```turf'; head -c 3000 "$f"; echo; echo '```'; }
  fi
}

created=0; : > "$OUT/overflow.txt"
while IFS= read -r sig; do
  [ -n "$sig" ] || continue
  title="Nightly fuzz: $sig"
  n=$(open_issue "$title")
  if [ -n "$n" ]; then
    gh issue comment "$n" --repo "$GITHUB_REPOSITORY" \
      --body "Seen again $today by **$scope** in $run (turflang \`$sha\`, artifact \`$artifact\`)."
    l=$(compiler_label "$scope"); ensure_label "$l"
    gh issue edit "$n" --repo "$GITHUB_REPOSITORY" --add-label "$l" >/dev/null || true
  elif [ "$created" -lt "$MAX_NEW" ]; then
    body "$sig" > "$OUT/issue-body.md"
    create "$title" "$OUT/issue-body.md" "$(labels_for "$sig" "$scope")" && created=$((created + 1))
  else
    echo "$sig" >> "$OUT/overflow.txt"
  fi
done < "$items"

if [ -s "$OUT/overflow.txt" ]; then
  {
    echo "Nightly fuzz (**$scope**) found more new signatures than the $MAX_NEW issues a night allows (run $run, turflang \`$sha\`, artifact \`$artifact\`). Their reports and reproducers are in the artifact; each gets its own issue on a later night once it is not among the first $MAX_NEW, or when the others are committed under findings/."
    echo
    sed 's/^/- `/; s/$/`/' "$OUT/overflow.txt"
  } > "$OUT/issue-body.md"
  create "Nightly fuzz ($scope): $(wc -l < "$OUT/overflow.txt") more new signature(s) ($today)" "$OUT/issue-body.md" \
    "$(ensure_label fuzz-finding; l=$(compiler_label "$scope"); ensure_label "$l"; echo "fuzz-finding,$l")"
fi
exit 0
