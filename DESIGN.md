# turf-fuzz design

How turf-fuzz is built, how to run each part, and how a finding gets from
here to a fix in turflang. `README.md` is the short version; the per-tier
detail is in `tier0/README.md`, `engine/README.md` and `gen/SEMANTICS.md`.

## 1. Decisions

Made by the owner; change them only with the owner.

- **A separate repository.** Fuzzing has a different cadence (hours, large
  corpora) and its own CI budget. turflang's CI never depends on this repo;
  turflang keeps only its three-minute per-push crash fuzz
  (`scripts/fuzz/crash_fuzz.py`, job `compiler_sanitizers`).
- **Python 3, stdlib only**, for the generator, oracles, triage and
  orchestration. The libFuzzer targets are C++ built with clang.
- **Findings live in this repo's tracker** (`findings/<class>/<name>/`) and are
  promoted to turflang **by hand** (section 6). Never discard a minimized
  reproducer.
- **One compiler commit under test**, `compiler.lock`. Every finding records
  it, so every finding replays.
- **CI once a week** (Mondays 00:00 IST). Bulk corpora live in CI artifacts,
  not in git.
- **Language questions are parked** in `OPEN_QUESTIONS.md` and reviewed with
  the owner at the end of the roadmap, not decided while fuzzing.
- Decisions found by fuzzing and already made (recorded in turflang's
  FREEZE.md or docs and in `gen/SEMANTICS.md`): `INT_MIN % -1` is 0; `>>` by
  an amount outside [0, 64) traps `Integer overflow` like `<<`; assignment is
  value-first everywhere (`a[i] = e`, `l[i] = e`, any `operator []=`, method
  arguments before the receiver); a `double -> int` of NaN, inf or an
  out-of-range value, and an int `**` overflow, trap `Integer overflow`; NaN
  always prints `nan`; collections are shared handles, `.copy()` gives an
  independent one (FREEZE §6.27); the libFuzzer tier analyses without the
  prelude by default (`AnalysisOptions::SkipPrelude`).

## 2. Layout

| Path | What |
|---|---|
| `compiler.lock` | the turflang commit under test (`commit = <sha>`) |
| `tier0/` | tier 0, black-box crash fuzzer: token/line mutants of turflang's own programs |
| `gen/` | tier 1: typed program generator (`generate.py`), its AST and renderer (`tast.py`), the reference evaluator (`evaluate.py`), the divergence localizer (`trace.py`), the program reducer (`reduce.py`), and `SEMANTICS.md`, every rule the generator and evaluator rely on, with its source |
| `oracles/` | tier 2 and 3: `differential.py` (one program, every configuration), `run.py` (the loop over seeds), `soundness.py` (the F-MEM-5 escape boundary) |
| `engine/` | tier 4: libFuzzer targets over `turf::analyze()`, the token mutator, the seed corpus script |
| `triage/` | `signature.py` (stable identity of a compiler failure), `minimize.py` (line delta debugging, same signature required), `findings.py` (one directory per signature) |
| `findings/` | the tracker, section 5 |
| `ci/` | the weekly jobs, runnable locally (section 7) |
| `.github/workflows/weekly.yml` | the weekly workflow |
| `corpus/` | reserved for curated seeds; empty. Tier 0 and the engine seed from the turflang tree |
| `OPEN_QUESTIONS.md` | language decisions found by fuzzing, waiting for the owner |
| `work/`, `ci-out/` | scratch output, git-ignored |

## 3. Requirements and setup

A built turflang checkout, by default `../turflang`; set `TURF_ROOT` to use
another. The tools use:

| Binary | Configure turflang with | Used by |
|---|---|---|
| `build/turfc` | the normal build | everything |
| `build_asan/turfc` | `-DTURF_RT_SANITIZE=ON` (the runtime and collector under ASan + UBSan) | the differential's ASan configurations and the soundness oracle, when present |
| `build_san/turfc` | `-DTURF_SANITIZE_COMPILER=ON` (the compiler itself under ASan + UBSan) | `ci/sanitized.sh`, tier 0 by `--turfc` |

Build `build_asan` before `build`: both write `lib/libturfrt.a`, and the
plain build's must be the one left (turflang AGENTS.md). `TURFC` and
`TURFC_ASAN` override the paths.

The engine needs clang with compiler-rt (`engine/README.md`; Arch needs
`engine/fix_libfuzzer.sh`, run by the build).

Keep jobs at half the cores, 8 at most on the owner's machine; `oracles/run.py`
caps `--jobs` at 8.

## 4. The tiers

Each tier finds what the ones before it cannot. Throughputs are local, 8 jobs
unless stated, measured 2026-10-12.

### Tier 0: black-box crash fuzzer (`tier0/`)

Mutates turflang's own programs (`tests/`, `showcase/`, `examples/`, about
1200 seeds) at token and line level, compiles each mutant, and reports any
outcome other than a clean exit 0 or 1: a sanitizer report, an assertion,
`LLVM ERROR`, an internal compiler error, a signal, a hang (unless the
unmutated seed hangs too), any other exit status. It compiles with
`--emit-obj` (no link) by default. About 15 compiles/s.

    tier0/crash_fuzz.py --seconds 600 --jobs 8 --opt random
    tier0/crash_fuzz.py --seconds 600 --jobs 8 --opt random --turfc $TURF_ROOT/build_san/turfc
    tier0/crash_fuzz.py --replay findings/<class>/<hash>/min.tr

Options and false positives: `tier0/README.md`.

### Tier 1: typed generator and reference evaluator (`gen/`)

`generate.py SEED` writes a valid, terminating program, deterministically per
seed. `evaluate.py` runs the generator's AST, independently of the compiler,
and predicts the exact stdout, exit status or trap text and line. The
evaluator encodes only the rules in `gen/SEMANTICS.md`, each with its source (a
doc or a probe); a disagreement between the docs and the compiler is a
finding, not a rule.

What it generates (the module docstring of `generate.py` is the full list):
int/bool/`double`/`int32`/`char`/`string` and conversions, every arithmetic,
bitwise and comparison operator, `give`/`otherwise`, if/while/for-range/
for-each, break/continue, helpers with early return and bounded recursion,
structs with methods, `T[N]` (also 2-D), `T[]` write-through parameters,
`.copy()`, `List`/`Stack`/`Queue`/`Set`/`Map` (shared, copied), 15
`strings::` functions, errors with payloads, `recover produce` / `recover run`
/ `recover as err run`, `try`, `defer`, enums and `compare`, `fn<T>` helpers
and a generic struct, and reference graphs (`&S`/`&?S`, `heap()`, null checks,
walks, rings, references in fields, Lists and arrays of references
`(&S)[k]`/`(&?S)[k]`).

    python3 gen/generate.py SEED -o prog.tr --expected expected.txt
    python3 gen/trace.py SEED      # first statement where compiled and evaluated traces diverge
    python3 gen/reduce.py SEED -o min.tr --max-seconds 300

`trace.py` settles "compiler bug or generator bug" in one step: it instruments
the program to print every scalar after every statement, on both sides.
`reduce.py` shrinks a finding on the AST (128-459 lines down to 16-30 on 11
real findings), keeping the verdict's class and signature; every candidate is
well-formed Turf and the evaluator recomputes its expected result. Text
inputs (tier 0, libFuzzer) use `triage/minimize.py` instead.

### Tier 2: differential oracle (`oracles/differential.py`, `oracles/run.py`)

For one generated program: compile at -O0, -O1, -O2, -O3, -Os and -g, run
each binary, run the -O0 binary again under `TURF_GC_STRESS=1`,
`TURF_GC_VALIDATE=1` and both, and, with `build_asan/turfc`, compile at -O0
and -O2 against the ASan runtime and run plain and under GC stress. Compare
every configuration with every other (F-OPT-1 / INV-OPT-1) and with the
evaluator. About 1.0 program/s at 8 jobs with the full generator.

| Class | Meaning |
|---|---|
| `miscompile` | configurations disagree |
| `wrong-answer` | configurations agree, the evaluator does not. Presume a generator or evaluator bug first |
| `rejected-valid` | the compiler refused a program the generator considers valid |
| `crash` | the compiler crashed, or a binary died on a signal other than a panic's abort, hung, or GC validation reported |
| `sanitizer` | the ASan/UBSan runtime reported |

    python3 -u oracles/run.py --seconds 300 --jobs 8 --seed 1000 --reduce 150
    python3 oracles/differential.py SEED [--keep DIR] [--quick] [--no-asan]

Seeds run SEED, SEED+1, ...; a finding replays with `differential.py SEED`
against the same compiler. `--reduce` runs `gen/reduce.py` on each new
finding after the loop and writes `min.tr` beside `prog.tr`. At most `--cap`
(20) new directories per class per run. Use `python3 -u` when redirecting,
or the log is buffered.

### Tier 3: GC and ownership

Two parts. The generator's reference graphs, error payloads holding heap
values, and the ASan-runtime and GC-stress configurations of tier 2 exercise
the collector's root finding and the runtime's memory safety. The soundness
oracle checks the compiler's static side:

    python3 oracles/soundness.py --jobs 8 [--only SUBSTR] [--keep]

It generates 2662 small programs at the F-MEM-5 escape boundary (a reference
reaching a sink: a return, a long-lived field or array element, a collection,
`heap()`, directly or inside a struct or array value), each with a pinned
verdict: from this frame's
storage, rejected with E086 and the sink's wording; from a safe source,
accepted and printing exactly `42` at -O0, -O2 and under the ASan runtime with
a collection on every allocation. A `clobber()` call between the escape and
the read makes a dangling read print garbage. Failures: `unsound-accepted`,
`wrong-reason`, `rejected-valid`, `safe-wrong`, `crash`. Five single shapes
that were owner questions (a merge of a frame path with a heap path, a mixed
`give`, an error payload, a store through a call result, a `defer` after the
return value) are decided (2026-10-12) and run with the generated cases as
`pinned/...`, each with its verdict. A future open shape goes there once
decided.

### Tier 4: libFuzzer over the front end (`engine/`)

Coverage-guided, in-process, under ASan + UBSan, over `turf::analyze()`
(lex, parse, resolve, type check, lint, flow passes; not codegen). Targets:
`fuzz_analyze` (libFuzzer's byte mutation), `fuzz_analyze_tok` (a token/line
custom mutator and cross-over), and `reentrancy_check`, the gate that must
pass before anything the fuzzers report is trusted (every test file analysed
in two orders, identical results). The token tables and dictionary are
generated from `src/Lexer.cpp` at build time. About 7 exec/s per worker on
whole-program seeds; 28k edges after 20 minutes.

    CC=clang CXX=clang++ cmake -S engine -B engine/build -G Ninja
    cmake --build engine/build -j8 --target fuzz_analyze fuzz_analyze_tok reentrancy_check
    engine/seed_corpus.sh
    engine/run.sh tok 1200

Details, environment switches and measurements: `engine/README.md`.

## 5. The findings tracker

`findings/<class>/<name>/`, one directory per distinct bug. **The directory is
the dedupe**: a hit whose directory exists only bumps its counter and is not
minimized again, and nothing in it is overwritten. **A committed directory is
a known finding**; CI reports only untracked ones as new.

Three writers, three shapes:

| Writer | Name | Files |
|---|---|---|
| tier 0 (`triage/findings.py`) | the signature's 12-hex hash; class = the signature kind (`sanitizer`, `assert`, `llvm-error`, `ice`, `hang`, `signal`, `exit`) | `input.tr`, `min.tr`, `report.md` (signature, command line, flags, lock commit, seed file, fuzz seed, `occurrences`, output tail) |
| tier 2 (`oracles/run.py`) | a short hash of the verdict's signature; class from the table in section 4 | `prog.tr`, `expected.txt`, `out_<config>.txt`, `meta.json` (signature, summary, seed, `count`, lock commit, tree head, `turfc --version`), `min.tr` after `--reduce` |
| by hand (probing, the soundness oracle, an old finding renamed) | a descriptive kebab-case name | `min.tr` (or several `.tr` files) and `report.md`: class, how it was found and on which commit, repro, observed, cause, decision, and `fixed: turflang <sha>` with the pinned tests, or what is open |

A signature never contains an address, a line number, a temporary path or a
bare number, so the same bug gets the same directory on every run. Deep
recursions are keyed by the set of repeating functions, not the top frames.

Classes used so far by hand: `crash`, `doc-drift`, `hang`, `ice`, `leak`,
`llvm-error`, `miscompile`, `rejected-valid`, `target-exited` (the analysis
library exited the host process), `unsound`, `wrong-message`.

## 6. From a finding to a fix

1. **Reproduce** it against the lock: `tier0/crash_fuzz.py --replay min.tr`,
   `oracles/differential.py SEED`, `oracles/soundness.py --only NAME`.
2. **Decide whose bug it is.** A wrong answer, or the first finding of a new
   generator feature, is presumed a generator or evaluator bug until
   `gen/trace.py SEED` shows otherwise. A generator bug is fixed in `gen/` and
   is not a finding: delete the directory. A harness artefact (ASan stack
   size, a slow seed) is fixed in the harness.
3. **A language question** (the spec is silent, or a frozen decision would
   change) goes to `OPEN_QUESTIONS.md` and the finding stays open. Once the
   owner decides, the rule goes into turflang (FREEZE.md or the docs) and into
   `gen/SEMANTICS.md` with its source.
4. **Minimize**, if the tool did not: `gen/reduce.py` for generated programs,
   `triage/minimize.py` (through `crash_fuzz.py`) for text, by hand for the
   last few lines. Keep `input.tr`/`prog.tr` beside it.
5. **Fix it in turflang** with a pinned test **in the same change**: a
   `fail_*` test with `// EXPECT-DIAG:` for a diagnostic or a runtime panic,
   or a run test with its expected output. The turflang commit message names
   the finding.
6. **Record it here**: write or complete `report.md` (cause, decision,
   `fixed: turflang <sha>` and the test paths), and commit the directory
   (`test(findings): <what> (fixed in turflang <sha>)`). Bump `compiler.lock`
   to a commit with the fix and check the reproducer no longer reproduces.
   Never delete a minimized reproducer, fixed or not.
7. **Open** findings are committed too, with what is open in `report.md`, so
   CI stops reporting them as new.

## 7. CI

`.github/workflows/weekly.yml`, Mondays 00:00 IST (cron `30 18 * * 0`, UTC),
and on demand (`workflow_dispatch`, with each budget as an input). The logic
is in `ci/*.sh`, which run the same locally.

| Job | Script | What |
|---|---|---|
| `process` (pinned, master) | `ci/process.sh` | differential + reduction (2400 s), soundness, tier 0 (900 s), against the lock and against turflang master |
| `sanitized` | `ci/sanitized.sh` | tier 0 against the ASan + UBSan compiler from master (2700 s); turflang's former nightly crash fuzz |
| `engine` | `ci/engine.sh` | `fuzz_analyze_tok` (3600 s), continuing from last week's `engine-corpus` artifact (90 days) |

The first seed comes from the ISO week, so a week's run replays and no two
weeks overlap. Each job writes `ci-out/` (summary, logs, new findings) as an
artifact and files at most one issue in this repo for new signatures (label
`fuzz-finding` if it exists). turflang is checked out with the workflow's own
token, or `secrets.TURFLANG_TOKEN` if set; the locked commit must be pushed
to turflang.

## 8. Traps

- Never edit turflang's `lib/std` or rebuild `build/turfc` while a run is
  going: the running compiler reads them live, and every compile turns into a
  finding.
- `oracles/run.py` buffers its log when redirected: use `python3 -u`.
- An ASan-built compiler needs a larger stack for the parser's nesting cap:
  tier 0 raises it (`--stack-mb`), the CI scripts set `ulimit -s 65536`.
- Codegen's private `getExprType()` cannot type every expression kind; a new
  crash with a null type is likely that.
- The engine runs with `detect_leaks=0`. Diagnostics no longer leak (turflang
  b48c297), but an over-deep tree (E111) is released on purpose, so leak
  detection would stop at the first such input
  (`findings/leak/diagnostic-longjmp`).

## 9. What it has found

All fixed in turflang, each with a pinned test (the `findings/` directory or
the turflang commit has the details).

| turflang | Bug | Found by |
|---|---|---|
| 20385de | `INT_MIN % -1`: SIGFPE at -O0, garbage at -O1+ | generator |
| cba079f | unused-variable warnings in address order (non-deterministic in turf-lsp) | re-entrancy gate |
| ef495d1 | `sizeof(void)` hung the compiler | tier 0 |
| cab244a | read through a setter-only `operator []`: LLVM abort | tier 0 |
| ad94564 | INT_MIN literal rejected in some positions; `c + 9223372036854775808` silently miscompiled | generator |
| 479a3bc | facade `exit()` on a first-token lex error (would kill turf-lsp) | libFuzzer |
| e9a0617 | `>>` by >= 64 or < 0 was poison (varied by -O) | generator probe |
| a89bd67 | `T[N]` bounds panic said `'array'`, not the name | generator probe |
| e2560c1 | `a.length()` in an array literal: segfault | generator |
| 44406a5 | `give`/`otherwise` in an array literal, or indexed: abort | generator |
| 49e654b | variables_and_types.md described the removed "Mechanism B" | generator |
| 8467172 | `l[i] = e` evaluated the index first; List/Set `[]` panics had no location | generator |
| f6ddd39 | target data layout set after codegen: `.copy()` of padded structs overflowed the heap | GC differential |
| 2cd5a20, db69298, 462df65 | collections shared, `.copy()` on all five; `::default()` left a `&U` field null; generic fields of a struct clone never instantiated; E129 missed `&Inner<T>` | generator model of List |
| 6858502 | generic type named only by a function signature: ICE | generator |
| 4a0581e | `G::default()` as an argument or a `return`: ICE | probing |
| 79e089b | `fn main(): void`: ICE | probing |
| e0c6ea0 | `double -> int` of NaN/inf/out of range, and int `**` overflow: poison | probing |
| 402eb39 | NaN printed `-nan` or `nan` depending on -O | probing |
| 94c78ae | `En::COUNT` typed as the enum under a cast | generator |
| c851cac | `char` ordering compared signed bytes | generator |
| 1c7b790 | `m[k]` on a missing key named map.tr's line | probing |
| 813fb53 | generic call typed by a merely compatible instantiation: ICE on `to int` | probing |
| d896209, ec236f2 | null checks lost at loops; unchecked `&?T` dereferences | generator |
| bfa72ba | `Variant(List::default())`: ICE | generator |
| 9398f38, 1de8105 | EscapePass lost provenance at loops and in `give` | soundness oracle |
| 2f49c7e | a deferred `&?T` read after the block reassigns it | reducer |
| e1dcd7a | every diagnostic's error object leaked under the facade | libFuzzer |
| 900f330 | `x.a.a...a` on a non-struct: exponential compile time, then an ICE (now E068) | first local CI dry run |
| b48c297 | every diagnostic leaked its temporaries and the abandoned frames' locals (recovery is an exception now, not `longjmp`); a function that failed to parse made each call of it E041 | libFuzzer; generator |
| 02759f8 | a `&?T` dereference inside a `recover` block was never checked (E081) | known bug, re-found |
| a7a881a | no way to write an array of references, and `&(N[2])`/`(&N)[2]` both printed `&N[2]`; now `(&N)[k]` (owner decision, FREEZE §6.28) | probing |
| c89f429 | an array literal of references with `null` (`[r, null]`) was E004, and `a = [r1, r2]` into a `(&?N)[2]` E066, though a declaration took both | generator (seed 7180) |
| 8e16021 | F-MEM-5's eight documented residuals (struct copies, `heap(m)`, Lists of structs, two hops, by-value parameter fields, `&a[i]`, `return id(&n)`) compiled and dangled; per-field tracking (owner decision). Also a `continue` inside a `recover` block in a loop was E007 since 02759f8 | soundness oracle; differential (seed 424234) |

## 10. Open work

**Coverage the generator does not have yet.** Error payloads on `!void` and
plain `recover run` on it; collections and generic structs as parameters,
fields or elements; `Set<string>`; generics with more than one type
parameter, over arrays or Lists of `T`, or at `T` = struct, `int32`, `char`;
`compare` on strings, `_`, destructuring and guards; `printline(char)`;
`StringBuilder`, `split`/`join`, `pad_*`, `parse_int`, `for c in s`,
`s[i] = c`; references in `Stack`/`Queue`/`Set`/`Map` or as generic
arguments; `(&S)[]` slice parameters; inlining a helper into its
caller in the reducer. The generator never emits a read of a never-written
`string` or `&U` array element (an open owner question).

**Throughput.** The full generator runs about 1.0 program/s, down from 1.7
before generics, not investigated. The engine is limited to about 7 exec/s
by whole-program seeds; smaller seeds or a no-flow-analysis mode would raise
it.

**Open findings.** `findings/unsound/escape-boundary` (its four owner
questions), `findings/unsound/int-to-reference-cast` and the rest of
`OPEN_QUESTIONS.md`.

**CI.** Not yet run on GitHub: both repos must be pushed, the locked commit
included. The `fuzz-finding` label does not exist yet.
