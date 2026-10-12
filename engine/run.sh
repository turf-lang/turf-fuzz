#!/usr/bin/env bash
# Run one libFuzzer target over the merged seeds, with the settings used to
# compare byte and structure-aware mutation (item 5).
#
#   engine/run.sh byte|tok SECONDS [FORKS]
#
# byte: engine/build/fuzz_analyze (libFuzzer's own mutation)
# tok:  engine/build/fuzz_analyze_tok (token/line mutator, mutator.cpp)
# Both get the same -dict (generated from the lexer) and a fresh copy of
# work/engine/seeds (engine/seed_corpus.sh). FORKS defaults to 4, so the two
# can run side by side within the half-the-cores budget. Output, corpus and
# artifacts go to work/engine/<mode>-<time>/; the last cov: line is the
# edge count to compare.
#
# -detect_leaks=0: every diagnostic the front end raises leaks its error
# object (raise() longjmps past its destructor;
# findings/leak/diagnostic-longjmp), so with leak detection on, the first
# invalid input ends the run.
set -euo pipefail
# ASan's frames are larger: the parser's 2000-level nesting cap needs more
# than the default 8 MB stack (turflang AGENTS.md, the fail_deeply_nested_*
# tests); without this they overflow and look like crashes.
ulimit -s 65536
here=$(cd "$(dirname "$0")" && pwd)
mode=$1 secs=$2 forks=${3:-4}
case $mode in
  byte) bin=$here/build/fuzz_analyze ;;
  tok)  bin=$here/build/fuzz_analyze_tok ;;
  *) echo "mode is byte or tok" >&2; exit 2 ;;
esac
seeds=$here/../work/engine/seeds
[ -d "$seeds" ] || { echo "no seeds: run engine/seed_corpus.sh first" >&2; exit 2; }
out=$here/../work/engine/$mode-$(date +%Y%m%d-%H%M%S)
mkdir -p "$out/corpus" "$out/artifacts"
cp "$seeds"/* "$out/corpus/"
cd "$out"
ASAN_OPTIONS=detect_leaks=0:${ASAN_OPTIONS:-} "$bin" -fork="$forks" -dict="$here/build/tokens/turf.dict" -max_len=16384 \
  -max_total_time="$secs" -timeout=10 -rss_limit_mb=4096 -detect_leaks=0 \
  -ignore_crashes=1 -ignore_timeouts=1 -ignore_ooms=1 \
  -artifact_prefix="$out/artifacts/" corpus > log.txt 2>&1 || true
echo "$mode: $(grep -oE 'cov: [0-9]+ ft: [0-9]+ corp: [0-9]+' log.txt | tail -1)," \
  "artifacts: $(find artifacts -type f | wc -l), dir: $out"
