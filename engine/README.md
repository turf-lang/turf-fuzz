# engine/: libFuzzer over the compiler front end

In-process, coverage-guided fuzzing of `turf::analyze()` (the query facade,
`include/turf/Analysis.h` in turflang): lex, parse, resolve, type check, lint
and the flow passes, with ASan + UBSan. Codegen and linking are not reachable
from here; the process tiers (`tier0/`, `oracles/`) cover them.

## Targets

| Binary | Mutation |
|---|---|
| `fuzz_analyze` | libFuzzer's own (bytes, `-dict`, traced comparisons) |
| `fuzz_analyze_tok` | `mutator.cpp`: `LLVMFuzzerCustomMutator` does 1-3 token or line operations (the `tier0/mutate.py` set, plus inserting whole constructs and wrapping a run in a block), a quarter of the time libFuzzer's own mutation instead; `LLVMFuzzerCustomCrossOver` splices two inputs at token boundaries or moves a run of lines between them |
| `reentrancy_check` | not a fuzzer: analyses a file list in two orders and requires identical results, the gate for trusting anything the fuzzers report |

The token tables and the dictionary (`build/tokens/turf_tokens.inc`,
`build/tokens/turf.dict`) are generated at build time by `gen_tokens.py` from
the lexer's keyword table in `src/Lexer.cpp`, so a new keyword is fuzzed with
no edit here.

## Build and run

    CC=clang CXX=clang++ cmake -S engine -B engine/build -G Ninja
    cmake --build engine/build -j8 --target fuzz_analyze fuzz_analyze_tok reentrancy_check
    engine/seed_corpus.sh               # tests/, lib/std, showcase -> work/engine/seeds (merged)
    engine/run.sh tok 1200              # or byte; 4 forks by default

Arch's compiler-rt needs `fix_libfuzzer.sh` (done by the build). Runs use
`-detect_leaks=0`: every diagnostic leaks its error object
(`findings/leak/diagnostic-longjmp`), so leak detection would stop at the
first invalid input. Environment: `TURF_FUZZ_PRELUDE=1` analyses the prelude
too (about 15x slower); `TURF_FUZZ_CHECK_DETERMINISM=1` analyses every input
twice and aborts on a difference.

## Measured (2026-10-12, turflang 2f49c7e)

Both targets side by side, 20 minutes each, 4 forks each, the same 1,164
merged seeds (26,447 edges on their own) and the same `-dict`:

| | edges | new over the seeds | features | corpus | exec/s |
|---|---|---|---|---|---|
| `fuzz_analyze` (byte) | 27,824 | +1,377 | 119,010 | 2,467 | 7 |
| `fuzz_analyze_tok` (token) | 28,012 | +1,565 | 119,605 | 2,797 | 7 |

The token mutator finds about 14% more new edges and grows the corpus 13%
more. Both pass the earlier 27.3k byte-mutation plateau, but most of that is
the seeds: they alone reach 26.4k. The limit is throughput: about 7
executions per second per worker, because the seeds are whole test programs
(about 0.1 s each to analyse under ASan). Smaller seeds, or a no-flow-analysis
mode for most inputs, would raise it; not tried yet.
