# Semantics encoded by the generator and evaluator

Every rule `gen/evaluate.py` and `gen/generate.py` rely on, with its source.
"probe" means a tiny program compiled with `build/turfc` and run (work/probe,
compiler commit in `compiler.lock`, `turfc 2.0.0-dev`, LLVM 22.1.8, x86-64
Linux). "doc" means the cited file under the turflang tree. Where the docs and
the compiler disagree, the compiler's behaviour is recorded here only if the
docs are silent; a disagreement is a finding, not a rule.

## Types and values

| Rule | Source |
|---|---|
| `int` is 64-bit signed. `bool` is a distinct type; `&&`, `||`, `!` yield `bool`. | doc `docs/reference/variables_and_types.md`; `FREEZE.md` 6.2 |
| Only `int` and `bool` are generated. `int32`, `double`, `char`, `string` are out of subset. | scope |
| Every local must be initialized at declaration (`mut x: int;` then a read is a definite-assignment error). The generator always writes `mut x: T = e;`. | doc variables_and_types.md, Mechanism A |
| Declaration needs an explicit `: Type`; `mut`/`immut` forms are used. `immut` is only generated with a non-literal-only initializer (see constant folding). | doc variables_and_types.md |

## Arithmetic

| Rule | Source |
|---|---|
| `+ - *` on `int` trap with panic text `Integer overflow` if the exact result is outside [-2^63, 2^63-1]. Never wrap, never UB. | doc `FREEZE.md` 6.2 row 6 and F-NUM-3; probe (`a + 1`, `a * 2`, `m - 1`) |
| Unary `-` traps (`Integer overflow`) on INT_MIN. | `tests/36_numeric_semantics/fail_runtime_overflow_negate.tr`; probe |
| `x++`, `x--`, `x += e`, `x -= e`, `x *= e` trap `Integer overflow` exactly like the plain operators (same text, same line). | probe (`c++`, `m--`, `c += 1`, `c *= 2`) |
| `/` truncates toward zero; `%` takes the sign of the dividend (C semantics): `-7/2 = -3`, `-7%2 = -1`, `7/-2 = -3`, `7%-2 = 1`, `-7%-2 = -1`. | probe (a.tr) |
| `/` by zero traps `Division by zero`; `%` by zero traps `Modulo by zero`. | doc `operators.md` ("panics with Division by zero" -- the doc names only the first text); probe and `src/CodegenExpr.cpp` (`"Modulo by zero"`) |
| In compound assignment the texts are `Division by zero in compound assignment` and `Modulo by zero in compound assignment`. | probe; `src/CodegenVar.cpp` |
| `INT_MIN / -1` traps `Integer overflow` (checked after the zero check). Same for `/=`. | `tests/36_numeric_semantics/fail_runtime_overflow_div_intmin_negone.tr`; probe |
| **`INT_MIN % -1` (and `%=`) is not encoded.** The evaluator raises `Undefined` and the generator skips the program. The compiler does not guard `srem` the way it guards `sdiv` (probe: `-O0` dies on SIGFPE, `-O1..-Os` print stack garbage). Expected semantics are unknown (mathematically 0, or the same trap as `/`); see the finding. Use `--allow-undefined` to let such programs through and compare configurations only. | probe; `src/CodegenCore.cpp` (guard doc comment says SDiv/SRem, `CodegenExpr.cpp` `%` path never calls it) |
| Operands are evaluated left to right, including call arguments; the first trap in that order wins. | probe (j.tr: `p(1) + p(2) * p(3)` and `p(10) / z + p(11) * m * m` print `1 2 3`, then `10`, then trap before `p(11)`). Docs state L-to-R only for `for` ranges. |
| `&&` and `||` short-circuit: the right operand is not evaluated when the left decides. | doc `operators.md`; probe (`z != 0 && 10 / z > 1` does not trap) |
| `(c give a otherwise b)` evaluates only the selected branch. It is always parenthesized by the generator. | doc `control_flow.md`; probe (m.tr) |
| `==`/`!=` compare two `int`s or two `bool`s; `< > <= >=` compare `int`s. The result is `bool`. | doc `operators.md`; probe |
| `!x` is a `bool` operator, `-x` an `int` operator; both bind tighter than every binary operator. | doc `operators.md` ("Unary operators bind tighter than every binary operator") |
| Precedence (loosest first): `\|\|` 15, `&&` 20, `== !=` 25, `< > <= >=` 30, `+ -` 40, `* / %` 50; all left associative. The generator prints minimal parentheses (`paren=min`), extra random ones (`rand`) or full ones (`full`), chosen per program. | doc `operators.md` precedence table; verified by the clean runs (a precedence bug in either would be a wrong-answer) |
| `- -x` and `a - -b` lex as two minus tokens when separated by a space; the renderer always inserts the space (a bare `--x` is a decrement token). | doc `operators.md` ("tokenization trap"); probe (`- -3` prints 3) |

## Literals and compile-time folding

| Rule | Source |
|---|---|
| Integer literals are exact until typed; one outside int64 is a hard error (E076). `-9223372036854775808` is writable. | doc `FREEZE.md` 6.2 rows 2-3 and bullets |
| Arithmetic on literal-only operands (`+ - *`, unary `-`) is folded at compile time and rejected (E076) if the result leaves int64. That is a rejected program, not a runtime trap, so the generator never emits one (`check_foldable`). `/` and `%` of literals are not folded (a literal `5 / 0` is a runtime trap plus a warning; `(INT_MIN) / (-1)` of literals traps at run time). | doc `FREEZE.md` 6.2 row 6; probe (e.tr, d.tr, f.tr) |
| `immut` locals whose initializer is literal-only are compile-time constants and take part in that folding; the generator therefore declares `immut` only with a non-constant initializer. | `tests/36_numeric_semantics/04_immut_constant_reference_folds_at_compile_time.tr` name, `fail_immut_constant_reference_arithmetic_overflow.tr` |
| **Quirk, skipped by default (`--probe-quirks` to include it):** the literal `-9223372036854775808` as the *left* operand of `+ - *` (even `-9223372036854775808 + 0`) is rejected with E076; as a right operand, or left of `== < /`, it is accepted. The generator does not emit INT_MIN as a left operand. See the finding. | probe (t.tr, 2026-10-11), `tests/36_numeric_semantics/03_...` uses it left of `==` |

## Statements

| Rule | Source |
|---|---|
| `if c { } elseif c { } else { }` with mandatory braces; `else if` is not the sanctioned spelling, so `elseif` is generated. Empty bodies are legal. | doc `control_flow.md` |
| `while cond { }`; the generator uses `mut cN: int = 0; while cN < K { <incr>; ... }` with a literal K in 0..8 and the increment (`cN = cN + 1;`, `cN += 1;` or `cN++;`) as the first body statement, so `continue` never skips it. Loop counters are never assigned elsewhere. `break`/`continue` appear only inside loops, as the only statement of an `if` body. | doc `control_flow.md` |
| A function ends with `return e;`. Early `return` appears only as the only statement of an `if` body in a helper. Helpers call only lower-numbered helpers (no recursion). | scope |
| Parameters and loop counters are never assigned; `chk` is only assigned by the checksum statement. | scope (keeps the evaluator trivial) |
| Variable names are unique per function (`v1`, `c1`, `p0`, `chk`), so scoping needs no modelling. A name declared inside a block is not used after it. | scope |

## Output and process outcome

| Rule | Source |
|---|---|
| `io::printline(int)` prints `%lld` and `\n`; `io::printline(bool)` prints `1` or `0` (not `true`/`false`). | doc `docs/reference/stdlib/io_and_net.md`; probe |
| stdout is buffered. When stdout is a pipe and the program traps, unflushed output is lost (the panic calls `abort()`; probe: `before` never appeared). So the generator emits `io::flush();` after every `printline`, making stdout at the trap point deterministic. `io::flush()` is the stdlib function in `lib/std/core/io.tr`. | probe (c.tr); `runtime/turfrt_panic.c` |
| A trap writes `panic: runtime check: <text> (<file>:<line>)` to stderr and `abort()`s: exit status is SIGABRT (134 in a shell, -6 in Python). `<line>` is the line of the statement (or `elseif` branch) containing the faulting expression, in the generated file; a trap inside a helper reports the line in the helper. The oracle compares `<text>` and `<line>` (the evaluator predicts the line), the file name is `prog.tr`. | probe; `runtime/turfrt_panic.c`; the evaluator's line tracking is validated by clean runs |
| `main` returning `v` exits with `v & 0xFF` (`return -1` gives 255, `return 300` gives 44). Normal exit prints nothing to stderr. | probe (i.tr, j.tr, l.tr) |
| Compiler warnings (unused variable, "division by zero" literal) go to the compiler's output, never to the program's, and do not change the exit status. | probe |

## Environment variables for the differential oracle

| Name | Meaning | Source |
|---|---|---|
| `TURF_GC_STRESS=1` | collect on every allocation | `AGENTS.md` (turflang); `.github/workflows/test.yml` |
| `TURF_GC_VALIDATE=1` | independent mark phase, panics `GC root missed`, prints `TURF_GC_VALIDATE:` lines on stderr | `AGENTS.md`; `src/TestRunner.cpp` (matches the `TURF_GC_VALIDATE:` prefix) |

Both are **runtime** variables of the compiled program (`runtime/turfrt_gc.c`),
not compiler flags, so the oracle sets them when it runs the `-O0` binary
(stress, validate, both) rather than recompiling. Programs in this subset never
allocate, so these runs mostly check that the runtime start-up path is quiet;
they become meaningful when the generator grows heap values.

## Open questions

* `INT_MIN % -1` and `INT_MIN %= -1`: undefined here; the compiler is
  inconsistent between optimization levels (finding `miscompile`).
* INT_MIN literal as left operand of `+ - *` (E076): bug or the OVH-014
  "leaf narrowing"? The `+ c` shape is not literal-only arithmetic, so the
  recorded narrowing does not obviously cover it.
* The docs name only `Division by zero` for `%` too (`operators.md`); the
  compiler says `Modulo by zero`. The evaluator follows the compiler, the doc
  is thin; a doc fix, not a compiler bug.
* Evaluation order is left to right by probe only; the docs do not promise it
  for operators or call arguments. If a future optimization reorders, the
  differential oracle will report it as a miscompile and this row decides
  whether it is a bug.
* `-g` alone appears to use the default optimization level, not `-O0`
  (it matched the optimized configurations in the `INT_MIN % -1` probe).
