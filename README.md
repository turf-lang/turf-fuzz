# turf-fuzz

Fuzzing for the TurfLang compiler, kept out of the compiler repo: different
cadence (hours, large corpora), its own CI budget, findings triaged on their
own track. turflang's CI never depends on this repo.

A confirmed finding becomes a minimized `fail_*` (or run) test **in turflang**,
pinned with `// EXPECT-DIAG:`, in the same change as its fix. Never discard a
minimized reproducer.

## Layout

| Dir | What |
|---|---|
| `compiler.lock` | turflang commit under test; every finding records it |
| `tier0/` | black-box token/line mutation crash fuzzer (port of turflang `scripts/fuzz/crash_fuzz.py`) |
| `gen/` | typed program generator + reference evaluator (knows the expected result): ints, bools, bitwise ops, structs with methods, arrays, slices, collections, strings, errors, enums, generics, reference graphs on the GC heap; every rule it relies on is in `gen/SEMANTICS.md`. `gen/trace.py SEED` finds the first statement where a wrong answer diverges; `gen/reduce.py SEED` shrinks a finding on the AST while its verdict stays the same (`oracles/run.py --reduce SECONDS` does it for every new finding after a run) |
| `oracles/` | differential (`-O` levels, `-g`, GC stress/validate, the ASan runtime), soundness (`soundness.py`: the F-MEM-5 escape boundary) |
| `engine/` | libFuzzer in-process target over `include/turf/Analysis.h` (C++, clang) |
| `triage/` | signature bucketing, minimization, issue text |
| `corpus/` | small curated seeds; bulk lives in CI artifacts |
| `findings/` | one directory per signature: reproducer, minimized input, report |
| `ci/` | the weekly CI run (`.github/workflows/weekly.yml`, Sundays): `process.sh` (differential + reduction, soundness, tier 0; against the pinned and the latest compiler), `sanitized.sh` (tier 0 against the ASan + UBSan compiler from master, turflang's former nightly fuzz), `engine.sh` (libFuzzer, corpus carried week to week as an artifact). New signatures only become an issue |

## Finding classes

1. compiler crash on invalid input
2. accepted-but-unsound
3. rejected-valid
4. miscompile (differential mismatch or wrong answer vs evaluator)
5. fault in a generated program (generator bug, runtime defect, or miscompile -- triage decides)

Findings are deduped by **signature** (top frames / assertion text / diagnostic
code), not by input hash.

## Requirements

A built turflang checkout. Point `TURF_ROOT` at it (default `../turflang`);
tools use `$TURF_ROOT/build/turfc`. Python 3, stdlib only.
