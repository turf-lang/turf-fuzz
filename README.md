# turf-fuzz

Fuzzing for the TurfLang compiler, kept out of the compiler repo: different
cadence (hours, large corpora), its own CI budget, findings triaged on their
own track. turflang's CI never depends on this repo.

**`DESIGN.md`** is the full description: decisions, every tier and how to run
it, the findings tracker, how a finding is promoted to turflang, CI, traps,
and what has been found so far.

A confirmed finding becomes a minimized test **in turflang** (a `fail_*` test
pinned with `// EXPECT-DIAG:`, or a run test), in the same change as its fix.
Never discard a minimized reproducer.

## Layout

| Dir | What |
|---|---|
| `compiler.lock` | turflang commit under test; every finding records it |
| `tier0/` | black-box token/line mutation crash fuzzer (port of turflang `scripts/dev/fuzz/crash_fuzz.py`); see `tier0/README.md` |
| `gen/` | typed program generator + reference evaluator (knows the expected result); every rule it relies on is in `gen/SEMANTICS.md`. `gen/trace.py SEED` finds the first statement where a wrong answer diverges; `gen/reduce.py SEED` shrinks a finding on the AST while its verdict stays the same |
| `oracles/` | differential (`-O` levels, `-g`, GC stress/validate, the ASan runtime; `run.py` loops over seeds), soundness (`soundness.py`: the F-MEM-5 escape boundary) |
| `engine/` | libFuzzer in-process targets over `include/turf/Analysis.h` (C++, clang); see `engine/README.md` |
| `triage/` | signature bucketing, line minimization, findings directories |
| `findings/` | one directory per bug: reproducer, minimized input, report. Committed = known |
| `ci/` | the weekly CI run (`.github/workflows/weekly.yml`, Mondays 00:00 IST): `process.sh`, `sanitized.sh`, `engine.sh` |
| `OPEN_QUESTIONS.md` | language decisions found by fuzzing, parked for the owner |
| `corpus/` | reserved for curated seeds (empty: seeds come from the turflang tree; bulk corpora are CI artifacts) |

## Quick start

    export TURF_ROOT=../turflang                  # a built turflang tree (the default)
    python3 -u oracles/run.py --seconds 300 --jobs 8 --seed 1000 --reduce 150
    python3 oracles/soundness.py --jobs 8
    tier0/crash_fuzz.py --seconds 300 --jobs 8 --opt random

## Requirements

A built turflang checkout at `TURF_ROOT` (default `../turflang`), using
`$TURF_ROOT/build/turfc`, and optionally `build_asan/turfc` (runtime under
ASan) and `build_san/turfc` (compiler under ASan). Python 3, stdlib only.
clang with compiler-rt for `engine/`.
