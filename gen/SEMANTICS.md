# Semantics encoded by the generator and evaluator

Every rule `gen/evaluate.py` and `gen/generate.py` rely on, with its source.
"probe" means a tiny program compiled with `build/turfc` and run (compiler
commit in `compiler.lock`, `turfc 2.0.0-dev`, LLVM 22.1.8, x86-64 Linux).
"doc" means the cited file under the turflang tree. Where the docs and the
compiler disagree, the compiler's behaviour is recorded here only if the docs
are silent; a disagreement is a finding, not a rule.

## Types and values

| Rule | Source |
|---|---|
| `int` is 64-bit signed. `bool` is a distinct type; `&&`, `\|\|`, `!` yield `bool`. | doc `docs/reference/variables_and_types.md`; `FREEZE.md` 6.2 |
| Generated types: `int`, `bool`, structs, sized arrays `T[N]` (also 2-D), `T[]` (parameters and `.copy()` locals), `List<T>`. `int32`, `double`, `char`, `string`, enums, pointers, generics other than `List` are out of subset. | scope |
| Every local is initialized at declaration, except `mut a: T[N];`, which the generator always follows at once with one element write (see Aggregates, definite assignment). | doc `FREEZE.md` F-INIT-1 |
| Declaration needs an explicit `: Type`; `mut`/`immut` forms are used. `immut` is only generated for a scalar with a non-literal-only initializer (see constant folding). Aggregates are always `mut`. | doc variables_and_types.md |

## Arithmetic

| Rule | Source |
|---|---|
| `+ - *` on `int` trap with panic text `Integer overflow` if the exact result is outside [-2^63, 2^63-1]. Never wrap, never UB. | doc `FREEZE.md` 6.2 row 6 and F-NUM-3; probe |
| Unary `-` traps (`Integer overflow`) on INT_MIN. | `tests/36_numeric_semantics/fail_runtime_overflow_negate.tr`; probe |
| `x++`, `x--`, `x op= e` trap `Integer overflow` exactly like the plain operators (same text, same line). | probe |
| `/` truncates toward zero; `%` takes the sign of the dividend: `-7/2 = -3`, `-7%2 = -1`, `7/-2 = -3`, `7%-2 = 1`. | doc `operators.md`; probe |
| `/` by zero traps `Division by zero`; `%` by zero traps `Modulo by zero`. In compound assignment the texts gain ` in compound assignment`. | probe; `src/CodegenExpr.cpp`, `src/CodegenVar.cpp` |
| `INT_MIN / -1` traps `Integer overflow` (checked after the zero check), also for `/=`. | `tests/36_numeric_semantics/fail_runtime_overflow_div_intmin_negone.tr` |
| `INT_MIN % -1` (and `%=`) is `0` at every level. | owner decision 2026-10-11; turflang 20385de; `tests/36_numeric_semantics/06_...` |
| `& \| ^` are two's-complement bitwise on `int` and never trap; `~x == -(x + 1)`. | doc `operators.md` |
| `a << s` and `a >> s` trap `Integer overflow` if `s` is outside [0, 63]. `<<` also traps if a set bit is shifted out past the sign, i.e. if `a * 2^s` is not representable. `>>` is arithmetic (sign-filling). Same text for `<<=` and `>>=`. | doc `operators.md`; `src/CodegenCore.cpp` EmitCheckedShl/EmitCheckedShr; owner decision 2026-10-11 for `>>` (turflang e9a0617) |
| Operands are evaluated left to right, including call arguments; the first trap in that order wins. | probe. Docs state L-to-R only for `for` ranges. |
| `&&` and `\|\|` short-circuit. | doc `operators.md`; probe |
| `(c give a otherwise b)` evaluates only the selected branch. It is always parenthesized by the generator. | doc `control_flow.md`; probe |
| `==`/`!=` compare two `int`s or two `bool`s; `< > <= >=` compare `int`s. Aggregates are never compared (no built-in struct `==`). | doc `operators.md`, `structs_and_enums.md` |
| `!x` is a `bool` operator, `-x` and `~x` `int` operators; all bind tighter than every binary operator. | doc `operators.md` |
| Precedence (loosest first): `\|\|` 15, `&&` 20, `== !=` 25, `< > <= >=` 30, `\|` 33, `^` 35, `&` 37, `<< >>` 38, `+ -` 40, `* / %` 50; all left associative. Bitwise binds tighter than comparison, unlike C. The generator prints minimal, random extra, or full parentheses, chosen per program. | doc `operators.md` precedence table |
| `- -x` and `a - -b` lex as two minus tokens when separated by a space; the renderer always inserts the space (`--x` is a decrement token). | doc `operators.md` ("tokenization trap") |

## Literals and compile-time folding

| Rule | Source |
|---|---|
| Integer literals are exact until typed; one outside int64 is a hard error (E076). `-9223372036854775808` is writable in every operand position. | doc `FREEZE.md` 6.2 rows 2-3 and bullets; turflang ad94564 |
| `+ - *` and unary `-` on literal-only operands fold at compile time and are rejected (E076) if the result leaves int64, so the generator never emits one (`check_foldable`). `/` and `%` of literals are not folded. | doc `FREEZE.md` 6.2 row 6; probe |
| Bitwise operators on literals are not folded: `1 << 63` traps at run time, and `(1 << 2) + 9223372036854775807` traps at run time rather than being E076, so a bitwise result is not a literal for folding. | probe |
| `immut` locals whose initializer is literal-only are compile-time constants and take part in that folding; the generator declares `immut` only with a non-constant initializer. | `tests/36_numeric_semantics/04_...`, `fail_immut_constant_reference_arithmetic_overflow.tr` |

## Aggregates

| Rule | Source |
|---|---|
| A struct and a sized array `T[N]` are values: assignment, a by-value parameter (`p: S`, `p: int[4]`), an array literal element, `push`, and a for-each variable each receive a copy. | doc `arrays.md` ("Arrays are value types"), `structs_and_enums.md`; probe (s1.tr, a1.tr) |
| A variable's storage never moves. Assigning a whole struct or array overwrites it in place, so a for-each over an array that its own body reassigns sees the new elements. | trace of seed 8191 (`gen/trace.py`) |
| `S::default()` zeroes every field recursively (int 0, bool false, arrays of zeros). | doc `structs_and_enums.md` |
| `mut a: T[N];` is zero-filled, but definite assignment counts the whole array unassigned until any one element is written; after that every element reads as 0. Reading it first is E008. Structs have no bare form in the generator. | doc `FREEZE.md` F-INIT-1/2, `docs/development/changes/SEM-004.md` (whole-array granularity); probe |
| A `T[]` parameter shares the caller's array: element writes are visible to the caller. Only writable arrays (MUT locals, fields of them, `self` fields, other `T[]` parameters and `.copy()` views) are passed to one, so E141 never applies. | doc `arrays.md` ("A T[] parameter shares the caller's array") |
| `a.copy()` is a fresh `T[]` on the GC heap; writes to it never reach `a`. | doc `arrays.md` ("Copying") |
| `a.length()` is the element count; for a `T[N]` it is `N`. | doc `arrays.md` |
| In `fn S.m(...)`, `self` is a pointer to the receiver: field writes are visible to the caller. Methods return `void`; only methods of structs declared earlier, or earlier methods of the same struct, are called, so there is no recursion. | doc `structs_and_enums.md` ("Instance methods and self") |
| A struct value is never read as `self` itself (only its fields), and struct parameters are never written or used as method receivers. | scope |
| `List<T>` is a `{data, len, cap}` value whose buffer is shared by copies: a callee's `push` on a by-value List parameter is invisible to the caller, while `m = l; m[0] = 50` shows through `l`. The generator never copies, assigns or passes a List. | probe (l1.tr) |
| `l[i]` reads by value; `l[i] = v` writes. A List element is never a place for a field write, a method call or a compound assignment (E098, E126). | doc `collections.md`; probe |
| `for v in a` iterates the elements of an array or slice live (a write by the body to a later element is seen), over the length at loop start. `for (i, v) in a` also binds the index. | probe (o3.tr) |
| `for i in lo..hi` runs `lo, lo+1, ..., hi-1`; `hi` is evaluated once. The generator uses literal bounds. | doc `control_flow.md`; probe (o3.tr) |

## Indexing

| Rule | Source |
|---|---|
| An index `i` into `N` elements is valid for `-N <= i < N`; a negative index counts from the end, once (no modular wrap). Same rule for `T[N]`, `T[]` and `List`. | doc `arrays.md` ("Negative indices"), `collections.md` |
| A provably out-of-range constant index into a `T[N]` is a compile error, so the generator indexes a `T[N]` only with a literal in range, `e % N` (always in range, sign of the dividend), or `opaque(e)`, a helper the range analysis cannot see through. A slice is indexed with `0`, `-1` (never empty), `e % s.length()` or `opaque(e)`; a List with `e % l.length()` under a `l.length() > 0` guard, or `opaque(e)`. | doc `arrays.md` ("Bounds checking"); probe (a2.tr) |
| An out-of-range array index traps `Array index out of bounds for '<name>'`, where `<name>` is the indexed variable, or `array` for any other base (a field, an element). Line: the statement's. | `src/CodegenArray.cpp` ArrayDisplayName; turflang a89bd67 (before it, every `T[N]` said `array`) |
| An out-of-range List read prints `List[]: index out of bounds`, a write `List[]=: index out of bounds`, both with no location; `pop` on an empty List prints `prog.tr:<line>: List.pop: pop on empty list`. All abort. | probe (l2.tr, l3.tr, l4.tr) |

## Evaluation order of places

| Construct | Order | Source |
|---|---|---|
| `a[i] = e`, `s.f[i] = e`, nested places | `e` first, then the indices outer to inner, then the bounds check | probe (o1.tr, o3.tr) |
| `a[i] op= e` | `e`, then indices, then read-modify-write | probe |
| `l[i] = e` on a List | `i`, then `e`, then the bounds check (it is the call `[]=(l, i, e)`) | probe (o3.tr) |
| `recv.m(args)` | arguments, then the receiver's indices | probe (o3.tr) |
| array literal `[e1, e2, ...]` | left to right | probe (o1.tr) |

## Statements

| Rule | Source |
|---|---|
| `if c { } elseif c { } else { }` with mandatory braces. Empty bodies are legal. | doc `control_flow.md` |
| `while cN < K { <incr>; ... }` with a literal K in 0..8 and the increment as the first body statement, so `continue` never skips it. Loop counters, `for` variables and for-each variables are never assigned. `break`/`continue` appear only as the only statement of an `if` body inside a loop. | doc `control_flow.md`; scope |
| A helper ends with `return e;` and returns `int`. Early `return` appears only as the only statement of an `if` body in a helper. Helpers call only lower-numbered helpers. | scope |
| Parameters are never assigned (elements of a `T[]` parameter are); `chk` is only assigned by the checksum statement. | scope |
| Variable names are unique per function, so scoping needs no modelling. A name declared inside a block is not used after it. | scope |

## Output and process outcome

| Rule | Source |
|---|---|
| `io::printline(int)` prints `%lld` and `\n`; `io::printline(bool)` prints `1` or `0`. | doc `docs/reference/stdlib/io_and_net.md`; probe |
| stdout is buffered and a trap `abort()`s, losing unflushed output, so the generator emits `io::flush();` after every `printline`. | probe; `runtime/turfrt_panic.c` |
| A runtime check writes `panic: runtime check: <text> (<file>:<line>)` to stderr and aborts (SIGABRT). `<line>` is the line of the statement (or `elseif` branch) containing the faulting expression; a trap inside a helper or method reports the line there. The stdlib panics above have their own shapes; `oracles/differential.py` parses both. | probe; `runtime/turfrt_panic.c` |
| `main` returning `v` exits with `v & 0xFF`. | probe |
| Compiler warnings never change the program's output or exit status. | probe |

## Environment variables for the differential oracle

| Name | Meaning | Source |
|---|---|---|
| `TURF_GC_STRESS=1` | collect on every allocation | `AGENTS.md` (turflang) |
| `TURF_GC_VALIDATE=1` | independent mark phase, panics `GC root missed`, prints `TURF_GC_VALIDATE:` lines on stderr | `AGENTS.md`; `src/TestRunner.cpp` |

Both are runtime variables of the compiled program (`runtime/turfrt_gc.c`),
so the oracle sets them when it runs the `-O0` binary. `List` growth and
`.copy()` allocate on the GC heap, with Lists, views and arrays of structs
as roots in locals, so these runs exercise root finding.

## Open questions

* Element assignment evaluates the value before the index (`a[i] = e`), but
  a List's `l[i] = e` evaluates the index first, and a method call evaluates
  its arguments before its receiver. No doc states any of these; they are
  encoded as probed, and the oracle reports a configuration that differs.
* List panics have no `panic: runtime check:` prefix, and `l[i]` names no
  location at all, unlike every compiler-emitted check.
* Evaluation order is left to right by probe only for operators and call
  arguments.
* The docs name only `Division by zero` for `%` (`operators.md`); the
  compiler says `Modulo by zero`. A doc fix, not a compiler bug.
* `-g` alone uses the default optimization level, not `-O0`.
