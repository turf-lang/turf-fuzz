#!/usr/bin/env bash
# Seed corpus for the libFuzzer targets: every .tr file in turflang's tests/,
# lib/std/ and showcase/, merged down (libFuzzer -merge=1) to the files that
# add coverage. Written to work/engine/seeds (git-ignored: it is rebuilt from
# the compiler tree, never edited by hand).
#
#   engine/seed_corpus.sh [TURF_ROOT] [FUZZER]
#
# FUZZER defaults to engine/build/fuzz_analyze; the merge keeps whatever that
# binary's coverage says is new, so rebuild the seeds after a compiler bump.
set -euo pipefail
# ASan's frames are larger: the parser's 2000-level nesting cap needs more
# than the default 8 MB stack (turflang AGENTS.md, the fail_deeply_nested_*
# tests); without this they overflow and look like crashes.
ulimit -s 65536
here=$(cd "$(dirname "$0")" && pwd)
root=${1:-$here/../../turflang}
fuzzer=${2:-$here/build/fuzz_analyze}
out=$here/../work/engine/seeds
raw=$(mktemp -d)
trap 'rm -rf "$raw"' EXIT
n=0
while IFS= read -r -d '' f; do
  # One flat directory; the name keeps the source path for a human reader.
  rel=${f#"$root"/}
  cp "$f" "$raw/$(echo "$rel" | tr '/' '_')"
  n=$((n + 1))
done < <(find "$root/tests" "$root/lib/std" "$root/showcase" -name '*.tr' \
           -not -path '*/custom/*' -size -64k -print0)
rm -rf "$out"
mkdir -p "$out"
mkdir -p "$out.artifacts"
ASAN_OPTIONS=detect_leaks=0 "$fuzzer" -merge=1 -max_len=65536 -detect_leaks=0 \
  -artifact_prefix="$out.artifacts/" "$out" "$raw" 2>&1 | grep -E "MERGE-OUTER: [0-9]+ new files" || true
echo "seed_corpus: $n files in, $(find "$out" -type f | wc -l) kept in $out"
