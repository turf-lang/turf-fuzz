#!/usr/bin/env bash
# The nightly libFuzzer run (engine/, structure-aware target), continuing from
# last night's corpus when there is one. Run by .github/workflows/nightly.yml.
#
#   ci/engine.sh             (needs engine/build from cmake, see engine/README.md)
#
# Environment (all optional):
#   ENGINE_SECONDS   fuzzing budget                         (default 10800)
#   FORKS            libFuzzer -fork                         (default: nproc)
#   CORPUS_IN        last night's corpus directory, if restored (default: none,
#                    start from engine/seed_corpus.sh's merged seeds)
#   OUT              report directory                         (default ci-out)
#
# Writes $OUT/engine-corpus (the grown corpus, merged down to what adds
# coverage: tomorrow's CORPUS_IN), $OUT/engine-artifacts (any crash, leak
# excluded, timeout or OOM input) and appends to $OUT/summary.md.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
ROOT=$(pwd)
ENGINE_SECONDS=${ENGINE_SECONDS:-10800}
FORKS=${FORKS:-$(nproc)}
OUT=${OUT:-$ROOT/ci-out}
BIN=$ROOT/engine/build/fuzz_analyze_tok
DICT=$ROOT/engine/build/tokens/turf.dict
mkdir -p "$OUT"
[ -x "$BIN" ] || { echo "no $BIN: build engine/ first" >&2; exit 2; }
ulimit -s 65536 || true
# Every raised diagnostic still leaks a few bytes through the recovery
# longjmp (findings/leak/diagnostic-longjmp); leak reports would end the run.
export ASAN_OPTIONS=detect_leaks=0:${ASAN_OPTIONS:-}

work=$(mktemp -d)
mkdir -p "$work/corpus" "$work/artifacts"
if [ -n "${CORPUS_IN:-}" ] && [ -d "$CORPUS_IN" ] && [ -n "$(ls -A "$CORPUS_IN")" ]; then
  cp "$CORPUS_IN"/* "$work/corpus/"
  from="last night's corpus ($(ls "$CORPUS_IN" | wc -l) files)"
else
  engine/seed_corpus.sh "${TURF_ROOT:-$ROOT/../turflang}" "$ROOT/engine/build/fuzz_analyze" > "$OUT/engine-seeds.log" 2>&1
  cp work/engine/seeds/* "$work/corpus/"
  from="the merged seeds ($(ls work/engine/seeds | wc -l) files)"
fi

echo "== libFuzzer, ${ENGINE_SECONDS}s, $FORKS forks, from $from"
"$BIN" -fork="$FORKS" -dict="$DICT" -max_len=16384 -max_total_time="$ENGINE_SECONDS" \
  -timeout=20 -rss_limit_mb=4096 -detect_leaks=0 \
  -ignore_crashes=1 -ignore_timeouts=1 -ignore_ooms=1 \
  -artifact_prefix="$work/artifacts/" "$work/corpus" > "$OUT/engine.log" 2>&1
last=$(grep -oE 'cov: [0-9]+ ft: [0-9]+ corp: [0-9]+' "$OUT/engine.log" | tail -1)

# Keep tomorrow's input small: only the files that add coverage.
rm -rf "$OUT/engine-corpus" && mkdir -p "$OUT/engine-corpus"
"$BIN" -merge=1 -max_len=16384 -detect_leaks=0 -artifact_prefix="$work/artifacts/" \
  "$OUT/engine-corpus" "$work/corpus" > "$OUT/engine-merge.log" 2>&1
rm -rf "$OUT/engine-artifacts" && mkdir -p "$OUT/engine-artifacts"
cp "$work/artifacts"/* "$OUT/engine-artifacts/" 2>/dev/null || true
n=$(ls "$OUT/engine-artifacts" | wc -l)
{
  echo
  echo "## turf-fuzz nightly: libFuzzer (fuzz_analyze_tok)"
  echo
  echo "- from $from; ${ENGINE_SECONDS}s, $FORKS forks: $last"
  echo "- corpus kept for tomorrow: $(ls "$OUT/engine-corpus" | wc -l) files"
  echo "- **$n crash/timeout/OOM input(s)**$([ "$n" -gt 0 ] && echo ": $(ls "$OUT/engine-artifacts" | tr '\n' ' ')")"
} >> "$OUT/summary.md"
echo "$n" > "$OUT/engine-artifact-count.txt"
rm -rf "$work"
tail -6 "$OUT/summary.md"
exit 0
