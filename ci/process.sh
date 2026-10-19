#!/usr/bin/env bash
# The nightly process-tier run: the differential oracle (with reduction), the
# soundness oracle and the tier-0 crash fuzzer, against an already built
# turflang tree. Run by .github/workflows/nightly.yml; runs the same locally.
#
#   ci/process.sh            (TURF_ROOT defaults to ../turflang)
#
# Environment (all optional):
#   TURF_ROOT        the built turflang tree (build/turfc, build_asan/turfc)
#   DIFF_SECONDS     differential oracle budget            (default 7200)
#   TIER0_SECONDS    tier-0 crash fuzzer budget            (default 2400)
#   OVERFLOW_SECONDS overflow oracle budget                (default 900)
#   REDUCE_SECONDS   per new differential finding          (default 150)
#   JOBS             parallel jobs                         (default: nproc)
#   SEED             first generator seed (default: from the UTC date, so a
#                    night's run is reproducible and no two nights overlap)
#   OUT              where the report goes                 (default ci-out)
#
# Only NEW signatures count. A finding whose directory already exists under
# findings/ (committed: known, maybe fixed) only bumps its counter; a new
# directory is new. Those are listed in $OUT/new-findings.txt and copied to
# $OUT/findings/, and the soundness oracle's failures, which have no
# signature directory, go to $OUT/soundness.txt. The exit status is 0 either
# way: the workflow decides from those files, so one failing tier never hides
# the others' results.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
ROOT=$(pwd)
export TURF_ROOT=${TURF_ROOT:-$ROOT/../turflang}
DIFF_SECONDS=${DIFF_SECONDS:-7200}
TIER0_SECONDS=${TIER0_SECONDS:-2400}
OVERFLOW_SECONDS=${OVERFLOW_SECONDS:-900}
REDUCE_SECONDS=${REDUCE_SECONDS:-150}
JOBS=${JOBS:-$(nproc)}
SEED=${SEED:-$(( $(date -u +%Y) * 1000000000 + 10#$(date -u +%j) * 1000000 ))}
OUT=${OUT:-$ROOT/ci-out}
rm -rf "$OUT" && mkdir -p "$OUT/findings"

[ -x "$TURF_ROOT/build/turfc" ] || { echo "no $TURF_ROOT/build/turfc" >&2; exit 2; }
# ASan frames are larger than the parser's nesting cap allows for in 8 MB.
ulimit -s 65536 || true
# A panicking program aborts; no core, so the host's crash reporter never
# holds it past the run timeout.
ulimit -c 0 || true

{
  echo "## turf-fuzz nightly: process tier"
  echo
  echo "- compiler: \`$(git -C "$TURF_ROOT" rev-parse --short HEAD)\` ($("$TURF_ROOT/build/turfc" --version | head -1)); lock \`$(sed -n 's/^commit = //p' compiler.lock | cut -c1-10)\`"
  echo "- ASan runtime: $([ -x "$TURF_ROOT/build_asan/turfc" ] && echo yes || echo 'no (build_asan/turfc missing)')"
  echo "- jobs $JOBS, first seed $SEED"
  echo
} > "$OUT/summary.md"

echo "== differential oracle, ${DIFF_SECONDS}s"
python3 -u oracles/run.py --seconds "$DIFF_SECONDS" --jobs "$JOBS" --seed "$SEED" \
  --reduce "$REDUCE_SECONDS" > "$OUT/differential.log" 2>&1
grep -E "^(programs|results):" "$OUT/differential.log" | sed 's/^/- differential /' >> "$OUT/summary.md"

echo "== soundness oracle"
python3 -u oracles/soundness.py --jobs "$JOBS" > "$OUT/soundness.log" 2>&1
grep -E "^ok:" "$OUT/soundness.log" | sed 's/^/- soundness /' >> "$OUT/summary.md"
grep -E "^  (unsound-accepted|wrong-reason|rejected-valid|safe-wrong|crash) " "$OUT/soundness.log" \
  > "$OUT/soundness.txt" || true

echo "== overflow oracle, ${OVERFLOW_SECONDS}s"
python3 -u oracles/overflow.py --seconds "$OVERFLOW_SECONDS" --jobs "$JOBS" --seed "$SEED" \
  > "$OUT/overflow.log" 2>&1
grep -E "^overflow: " "$OUT/overflow.log" | sed 's/^/- /' >> "$OUT/summary.md"

echo "== tier 0 crash fuzzer, ${TIER0_SECONDS}s"
python3 -u tier0/crash_fuzz.py --seconds "$TIER0_SECONDS" --jobs "$JOBS" --opt random \
  --seed "$SEED" > "$OUT/tier0.log" 2>&1
grep -E "^fuzz: [0-9]+ compiles in" "$OUT/tier0.log" | sed 's/^/- tier 0 /' >> "$OUT/summary.md"

# New signature directories: untracked under findings/.
git status --porcelain --untracked-files=all -- findings \
  | awk '$1 == "??" {print $2}' | xargs -r -n1 dirname | sort -u > "$OUT/new-findings.txt"
while read -r d; do
  mkdir -p "$OUT/findings/$(dirname "$d")" && cp -r "$d" "$OUT/findings/$d"
done < "$OUT/new-findings.txt"

{
  echo
  n=$(wc -l < "$OUT/new-findings.txt"); s=$(wc -l < "$OUT/soundness.txt")
  echo "**$n new signature(s), $s soundness failure(s).**"
  [ "$n" -gt 0 ] && { echo; sed 's/^/- `/; s/$/`/' "$OUT/new-findings.txt"; }
  [ "$s" -gt 0 ] && { echo; echo '```'; cat "$OUT/soundness.txt"; echo '```'; }
} >> "$OUT/summary.md"
cat "$OUT/summary.md"
exit 0
