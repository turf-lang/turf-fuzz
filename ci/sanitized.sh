#!/usr/bin/env bash
# The weekly sanitized crash fuzz: tier 0 against the compiler built with
# ASan + UBSan (TURF_SANITIZE_COMPILER=ON, in build_san/), so a memory error
# in a compiler pass is a finding even when the plain build survives it by
# luck. Took over turflang's nightly 45-minute run (owner, 2026-10-12);
# turflang keeps a three-minute pass per push. Run by
# .github/workflows/weekly.yml; runs the same locally.
#
#   ci/sanitized.sh          (TURF_ROOT defaults to ../turflang)
#
# Environment (all optional):
#   TURF_ROOT          the turflang tree with build_san/turfc
#   SANITIZED_SECONDS  fuzzing budget                  (default 2700)
#   JOBS               parallel compiles               (default: nproc)
#   SEED               default: from the ISO week, as ci/process.sh
#   OUT                report directory                (default ci-out)
#
# New signatures (directories not yet under findings/) go to
# $OUT/new-findings.txt and $OUT/findings/; exit status 0 either way.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
ROOT=$(pwd)
export TURF_ROOT=${TURF_ROOT:-$ROOT/../turflang}
SANITIZED_SECONDS=${SANITIZED_SECONDS:-2700}
JOBS=${JOBS:-$(nproc)}
SEED=${SEED:-$(( $(date -u +%G) * 100000000 + 10#$(date -u +%V) * 1000000 + 500000 ))}
OUT=${OUT:-$ROOT/ci-out}
TURFC=$TURF_ROOT/build_san/turfc
rm -rf "$OUT" && mkdir -p "$OUT/findings"
[ -x "$TURFC" ] || { echo "no $TURFC (configure with -DTURF_SANITIZE_COMPILER=ON)" >&2; exit 2; }

echo "== tier 0 against the sanitized compiler, ${SANITIZED_SECONDS}s"
# crash_fuzz.py raises the stack limit itself for an ASan binary (its
# frames are larger than the parser's nesting cap allows for in 8 MB).
python3 -u tier0/crash_fuzz.py --seconds "$SANITIZED_SECONDS" --jobs "$JOBS" --opt random \
  --seed "$SEED" --compiler "$TURFC" > "$OUT/sanitized.log" 2>&1

git status --porcelain --untracked-files=all -- findings \
  | awk '$1 == "??" {print $2}' | xargs -r -n1 dirname | sort -u > "$OUT/new-findings.txt"
while read -r d; do
  mkdir -p "$OUT/findings/$(dirname "$d")" && cp -r "$d" "$OUT/findings/$d"
done < "$OUT/new-findings.txt"
n=$(wc -l < "$OUT/new-findings.txt")
{
  echo "## turf-fuzz weekly: sanitized crash fuzz"
  echo
  echo "- compiler: \`$(git -C "$TURF_ROOT" rev-parse --short HEAD)\`, ASan + UBSan build; first seed $SEED"
  grep -E "^fuzz: [0-9]+ compiles in" "$OUT/sanitized.log" | sed 's/^/- /'
  echo
  echo "**$n new signature(s).**"
  [ "$n" -gt 0 ] && { echo; sed 's/^/- `/; s/$/`/' "$OUT/new-findings.txt"; }
} > "$OUT/summary.md"
cat "$OUT/summary.md"
exit 0
