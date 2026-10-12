# tier0 -- black-box crash fuzzer

Mutates turflang's own programs (`$TURF_ROOT/{tests,showcase,examples}`, about
1200 seeds) at token and line level, compiles each mutant with `turfc`, and
reports anything that is not a clean exit 0/1: sanitizer reports, LLVM
assertions / `LLVM ERROR` / internal errors, signals, hangs, other exit codes.
Port of turflang `scripts/fuzz/crash_fuzz.py`; self-contained (mutators are in
`mutate.py`, keywords are read from `$TURF_ROOT/src/Lexer.cpp` at start-up).
Python 3, stdlib only. Run from anywhere.

```
tier0/crash_fuzz.py --seconds 120 --jobs 8 --opt random
tier0/crash_fuzz.py --seconds 600 --jobs 8 --opt random --turfc $TURF_ROOT/build_san/turfc
tier0/crash_fuzz.py --replay findings/signal/<hash>/min.tr
tier0/crash_fuzz.py --replay min.tr --replay-beside $TURF_ROOT/tests/NN_x/seed.tr
```

| Option | Meaning |
|---|---|
| `--seconds N` | wall-clock budget (60) |
| `--jobs J` | parallel compiles (half the cores; keep it at 8 or less on the owner's box) |
| `--seed S` | RNG seed; printed, so a run is repeatable (compiler speed permitting) |
| `--opt random` | spread `-O0 -O1 -O2 -O3 -Os -g` over the run; or pass one flag (write `--opt=-g`, argparse reads a bare `-g` as an option) |
| `--turfc PATH` | compiler under test (default `$TURF_ROOT/build/turfc`) |
| `--out DIR` | findings root (default `turf-fuzz/findings`) |
| `--timeout SEC` | per-compile hang bound (20) |
| `--stack-mb N` | soft stack limit for the compiler. Default: 64 if the binary links ASan (its frames overflow 8 MB before the parser's nesting cap), unchanged otherwise |
| `--replay FILE` | compile one file and print its signature |
| `--replay-beside SEED` | with `--replay`: compile it in a mirror of SEED's directory, for mutants that import sibling modules |
| `--max-iterations N` | stop after N compiles |
| `--link` | link every program. Default: `--emit-obj` when the compiler has it (turfc interface 1.6+), so everything but the system linker runs |

`TURF_ROOT` defaults to `../turflang` relative to the turf-fuzz root.

## Findings and dedupe

Every failing run gets a **signature** (`triage/signature.py`): sanitizer report
type plus the top 3 in-project frames, or the assertion text, or the normalized
`LLVM ERROR` / internal-error message, or `hang`, or the signal name plus
frames, or `exit N: <first line>`. No addresses, line numbers or temp paths.
The signature's hash names a directory:

```
findings/<class>/<sig-hash>/
    input.tr    first input that produced it
    min.tr      line-minimized, same signature required
    report.md   signature, turfc command line, flags, compiler.lock commit,
                seed file, fuzz seed, `occurrences` counter, output tail
```

If the directory exists, the hit only bumps `occurrences` in `report.md` (and is
not minimized again). Exit status: 0 = no new signature, 1 = at least one new
signature, 2 = the harness failed. A confirmed finding becomes a pinned
`fail_*` test in turflang in the same change as its fix; never delete `min.tr`.

## False positives handled

* A hang the **unmutated seed** also has is a slow test (the E079 fan-out test
  takes ~20 s), not a finding.
* ASan stack: see `--stack-mb`. Run the sanitized build with it, or sanitizer
  "stack-overflow" findings are the harness's, not the compiler's.
* A reduced file that no longer reproduces on the final re-check is reported
  with `min.tr` = the unreduced input.
