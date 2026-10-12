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
| Generated types: `int`, `bool`, `string`, `double`, `int32`, `char`, enums (locals, struct fields, array elements; `int`, `string` and structs also as List elements), structs, sized arrays `T[N]` (also 2-D), `T[]` (parameters and `.copy()` locals), `List<T>` (locals; `List<int>` also as a helper parameter), `Stack`/`Queue`/`Set`/`Map` (locals). User generics: `fn<T>` helpers and one generic struct (locals). Safe references `&S` and `&?S` to structs (locals, parameters, struct fields, List elements; see References). Raw pointers, references to non-structs, error payloads and state machines are out of subset. | scope |
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
| A `List<T>` value is a handle to one header: `mut m: List<T> = l;`, `m = l;` and passing `l` to a `List<int>` parameter all share it, length included, so a callee's `push`/`pop`/`l[i] = v` is visible to the caller and `m.push(7)` grows `l`. `l.copy()` is an independent List with copies of the elements. The generator emits aliases, rebinding between `mut` List locals, List parameters of helpers (never assigned) and `.copy()` both as an initializer and as an argument. | owner decision 2026-10-12, turflang LEC-009 (FREEZE F-COLL-1, `docs/reference/stdlib/collections.md` "Shared by default"); probe (pp.tr). Before LEC-009 a List was a `{data, len, cap}` value and the generator never copied, assigned or passed one |
| `l[i]` reads by value; `l[i] = v` writes. A List element is never a place for a field write, a method call or a compound assignment (E098, E126). | doc `collections.md`; probe |
| `for v in a` iterates the elements of an array or slice live (a write by the body to a later element is seen), over the length at loop start. `for (i, v) in a` also binds the index. | probe (o3.tr) |
| `for i in lo..hi` runs `lo, lo+1, ..., hi-1`; `hi` is evaluated once. The generator uses literal bounds. | doc `control_flow.md`; probe (o3.tr) |

## Strings

All generated strings are ASCII from a small alphabet (`a b A Z 0 9`, space,
tab, newline, `.`, `-`), so byte order, code-point order and Python's `str`
agree.

| Rule | Source |
|---|---|
| A `string` is an immutable value: assignment, parameters, fields and elements may share its bytes, which is never observable because the generator never writes `s[i]`. `S::default()` gives a `string` field (also nested, also in a fixed array field) `""`. | doc `variables_and_types.md` default table; turflang 2cd5a20 (nested defaults were null before); probe (z2.tr) |
| `a + b` concatenates (strings.tr `operator +`); `s += t` is `s = s + t`. | doc `operators.md`, `FREEZE.md` 6.7 (compound assignment desugars); probe |
| `== != < > <= >=` compare with `strcmp`, i.e. byte order. | `lib/std/core/strings.tr` operators |
| `(n to string)` is the decimal form, `-` for negatives, `INT_MIN` included. | probe (s1.tr) |
| `s[i]` is a `char`; `(s[i] to int)` is its byte. Valid for `-n <= i < n`, a negative index counting from the end once; outside, `panic: runtime check: String index out of bounds (file:line)`. The generator reads `s[e % n]` under an `n > 0` guard, or `s[opaque(e)]`. | doc `operators.md` ("A negative index counts back from the end"); probe (s2.tr). `FREEZE.md` §6.14 F-STR-1 (amended 2026-10-12 to match #256) |
| `strings::length`, `index_of` (`""` found at 0; else Python `find`), `last_index_of` (`""` at `length`; else `rfind`), `count_char(s, code)`, `contains`, `starts_with`, `ends_with`. | `strings.tr` |
| `repeat(s, n)` is `""` for `n <= 0`; the generator uses `n` in -1..3. `replace(s, "", t)` is `s`; otherwise non-overlapping, left to right (Python `replace`). `to_upper`/`to_lower` map ASCII letters only. `trim`, `trim_start`, `trim_end` remove only space, tab, newline and carriage return. | `strings.tr` |
| `substring(s, a, b)` is bytes `a..b`; `a < 0`, `b < a` or `b > length` panics `prog.tr:<line>: strings.substring: index out of bounds` with the caller's line (F-API-3). The generator guards it, except for a few deliberate raw ranges. | `strings.tr`; probe (s3.tr) |
| `io::printline(string)` prints the bytes and `\n`. | doc `io_and_net.md` |
| The evaluator gives up on a program (regenerates it) once a string passes 4096 bytes, since `s = s + s` in nested loops grows without bound. | scope |
| `mut a: string[N];` (or any zero-filled array whose element holds a string): an element read before it is written is `""`. An array whose element carries a non-null `&U` (`H[2]`, `H { own: &U }`) needs an initializer, E128. The generator does not emit either yet. | FREEZE §6.28 F-REFARR-2 (amended 2026-10-13); owner decision 2026-10-12, turflang 12cde3f (before: a null pointer, `a[1].length()` segfaulted; probe z1.tr) |

## Errors, `defer`, recursion

| Rule | Source |
|---|---|
| One `error E0 { A, B, C }` (1-3 variants) per program. A `!int` helper returns either an `int` or `E0::V` / `E0::V(msg)` (a `string` message). | doc `error_handling.md` |
| `f() recover produce e` is the success value, or `e`; **`e` is evaluated only on failure**. Always parenthesized by the generator. | doc `error_handling.md`; probe (e2.tr) |
| `mut v: int = f() recover run { ... };` runs the block only on failure; `recover as err run { ... }` also binds `err`. the block ends in `return` (from the enclosing function), `break` or `continue`. `err == E0::V` compares the variant (true whatever the message); `err.message` is the message, `""` if none. | doc `error_handling.md`; probe (e1.tr) |
| `try f()` (only in a `!int` helper, never in a `defer` or a recover block: E095) is the success value, or returns the error unchanged from the enclosing function after its pending defers. The generator parenthesizes it inside any operator. | doc `error_handling.md`; probe (e1.tr, e2.tr) |
| `defer s;` runs `s` when the enclosing `{ }` block exits: normally, or by `return`, `break`, `continue` or `try`; LIFO; once per loop iteration; **never on a panic**. A `return`'s value is computed before the defers run. A panic inside a deferred statement reports the `defer`'s own line. `s` is a print, a `chk` mix or a scalar assignment. | doc `control_flow.md`; probe (d1.tr) |
| `defer { ... }` is not accepted (E103), so a deferred print renders as `defer io::flush();` then `defer io::printline(e);` (LIFO: print, then flush). | probe |
| A recursive helper takes the depth `d` first, returns at `d <= 0` before anything else, and calls itself once per level at the top of its body with `d - 1`; callers pass a literal 0..6. 100000-deep recursion works (probe), so depth is never the limit. | probe (d1.tr) |

## Error payloads

A second error type, `error E1 { P0(x0: T, ...), ..., Q }`, whose variants
carry 1-3 fields: `string`, `int`, an enum, `double`, a struct (also a node
or holder struct, with its references), `List<int>` / `List<string>`, `&N`,
`&?N`. "Thrower" helpers fail with variant `i` when `k % (n + 1) == i`, and a
sweep loop calls one for every `k`, so each variant is built, carried out
(through `try` when the caller is fallible) and matched in one run.

| Rule | Source |
|---|---|
| `E1::P(a0, a1)` takes one argument per field, in order, evaluated left to right; one more trailing `string` is the message. A variant with fields is never written bare as a value. Any `!int` function may return either error type. | doc `error_handling.md` ("Payloads: data on a variant"); probe (p2/a1.tr) |
| A payload field holds its argument as a location of that type would: a struct is copied, a `List` and a reference are shared with every other holder (a `List` pushed to before the `return` and one aliased after the match are the caller's own list). | probe (p2/a1.tr, b1.tr); FREEZE F-COLL-1 for the List |
| `E1::P(List::default())` builds a `List` of the field's declared type. | turflang bfa72ba (before it: an internal compiler error; `findings/llvm-error/generic-default-as-payload`) |
| A payload is read only by `compare (err) { matches E1::P(b0, _, 3) if g: ... done ... otherwise: ... }` on the `err` of `recover as err run`. Arms are tried in order; an arm runs if the variant is the same, every literal sub-pattern equals its field, and the guard (which sees the bindings) is true; a failed guard goes on to the next arm. A name binds, `_` skips, `..` ends a pattern that lists fewer fields; a variant with no fields has no parentheses. `otherwise` is always present (`err` is an open union: E093). | doc `error_handling.md` ("Reading a payload"), `control_flow.md`; probe (p2/a1.tr, b1.tr) |
| A binding is immutable throughout: no field write, no mutating method, no `push`, no write through it even when it is a reference (all E060). It may be read, passed to a function (one that mutates through it included), and copied to a `mut` variable that then writes. The generator reads bindings, passes them and aliases them. | probe (p2/a1.tr: E060 on `r.val = 4` and `items.push(5)`; b1.tr, b2.tr) |
| `err == E1::P` names a variant with fields bare and tests which variant it is, never its fields or message; `err.message` is the trailing message, `""` if none. | doc `error_handling.md`; probe (p2/a1.tr) |
| A variant gets one arm, or a refutable arm (literal or guard) followed by a catch-all arm for the same variant. | scope |
| Not generated: `compare` on a value of a declared error type, nested struct patterns inside a payload pattern, string literal sub-patterns, payloads of arrays, collections other than `List`, generic structs. | scope |

## User generics

| Rule | Source |
|---|---|
| `fn<T> gN(c: int, a: T, b: T): T`: `T` is inferred from `a` and `b` at each call, and every distinct `T` is its own instantiation (F-GEN-1). The body is ordinary generated code over `c` and its own locals; `T` has no operations (F-GEN-3), so the only `T` expressions are `a` and `b`, returned. Arguments pass by value. Called at `int`, `bool`, `string`, `double` and enum types. | doc `generics_and_operators.md`, `FREEZE.md` 6.1; probe (g1.tr) |
| `struct Gb0<T> { v: T, n: int }` with `fn<T> Gb0.put(x: T): void { self.v = x; self.n = self.n + 1; }` and `fn<T> Gb0.get(): T { return self.v; }`. `Gb0<X>` is a struct value like any other: copied by assignment, fields read and written, `Gb0::default()` where a declaration or assignment gives the type. Only as a local. | doc `structs_and_enums.md` ("Generic structs"); probe (g1.tr) |
| A generic call's result has the type of its own instantiation. | turflang 813fb53 (before it, with `g<double>` and `g<int32>` both instantiated, a call on int32 arguments was typed double and a following `to int` was an internal compiler error) |

## References (`&S`, `&?S`, `heap()`)

Node structs `N0`, `N1` hold `val: int`, up to two plain fields and 1-3
`&?Self` fields (one edge: a list; more: a tree or any graph); `N1` may also
hold a `&?N0` and a `&N0`. A holder `H0` has `head: &?N`, and optionally
`own: &N`, a node by value and an array of nodes. Programs build chains with
`heap()` in loops, tie them into rings, walk them under null checks
iteratively and recursively, and keep references in struct fields and Lists.
Compiler under test: turflang ec236f2 (the two fixes below are in it).

| Rule | Source |
|---|---|
| `heap(e)` copies the struct value `e` to the GC heap and yields a `&S`; later writes to `e` do not reach the copy. `e` is a local, a field, an array element or `S::default()`. | doc `docs/architecture/pointers_and_references.md` §1.3; probe (a1.tr) |
| A reference is a pointer to one struct: `=`, passing, returning and copying a struct that holds one all share the pointee. Fields are read and written and methods called through a `&S` with no dereference syntax (`p.val`, `p.val += 1`, `p.m0()`), and a write is visible through every other reference to it. A struct cannot be read by value out of a reference (`mut s: S = p;` is E066), only its fields. | doc §1.3 ("Auto-deref"); probe (a1.tr, a2.tr, c1.tr, d1.tr) |
| `null` is a `&?S` only. `&S` widens to `&?S` implicitly (initializer, assignment, argument, `push`); never back. `==`/`!=` compare identity between any two references to the same struct, `&S` and `&?S` mixed, and a `&?S` with `null`; a `&S` is never compared with `null`. | doc §1.3, §1.8; probe (a1.tr, i1.tr) |
| A `&?S` is used (field, method) only where the compiler has proven it non-null, and only a **variable** compared directly with `null` is ever proven: `if (q != null) { }`, `q != null && ...`, the `else` of `if (q == null)`, after `if (q == null) { return ...; }`, and a `while (q != null && ...)` body. The generator hides the variable for the extent of its check, so nothing assigns it there; the one exception is the last statement of a walk, `w = w.rJ;`. | doc §1.8, `structs_and_enums.md` ("Self-referential structs"); probe (a5.tr, e1.tr, f1-f9.tr) |
| A null check holds across a loop that does not assign the variable, inside it and after it. | turflang d896209 (before it, every loop dropped every null check: E081 on valid programs; `findings/rejected-valid/null-check-lost-at-loop`) |
| A `&?S` that is not a variable (`p.r0`, a call result, `l[i]`) is never dereferenced, and a method call needs the same check as a field read: both are E081, the cast `(p.r0 to &S)` E082. Such a value is only compared with null, stored and passed. | turflang d896209, ec236f2 (before them these compiled and a null faulted or read garbage; `findings/unsound/nullable-deref-not-a-variable`); FREEZE 6.6 |
| A checked `&?S` variable is still a `&?S` wherever a `&S` is required as a whole value; `(q to &S)` converts it, legal only inside the check (E082). | doc §1.8; probe (a6.tr, e1.tr) |
| The two branches of `give` must have the same static type: `&S` and `&?S` do not mix, and `null` has no type there. `l[i] = e` on a `List<&?S>` takes exactly a `&?S` (not a `&S`, not `null`): operators never widen. `l.push(e)` is a method and does widen. | probe (e1.tr, j1.tr); FREEZE F-OP-1 for the operator |
| `S::default()` gives a `&?S` field null and a `&S` field a fresh `S::default()` on the heap, one per field, also inside nested structs and fixed arrays of them. A zero-filled `mut a: T[N];` leaves a `&S` field null (the array-element hole), so it is never generated for such a `T`. | doc `structs_and_enums.md`, FREEZE F-COLL-2; probe (c5.tr, e3.tr) |
| `&S` fields only point at earlier node types, so no type reaches itself through `&` (E147). | doc `docs/errors/codes/E147.txt` |
| Assignment through a reference is value-first like every other: `mk(2).val = tr(3)` and `l[tr(0)].val = tr(3)` evaluate the right side, then the place. A `List<&S>` element is written through (`l[i].val = e`), since the element is a reference, not a temporary struct. | probe (d3.tr, i1.tr); FREEZE 6.19 ("It stops at a pointer or reference") |
| `&s` on a struct local is a `&S` into the current frame. It is generated only as a call argument for a parameter the callee was generated never to keep (it reads and writes through it, passes it on to the same kind of parameter, and nothing else), so no address of a frame outlives the frame. Everything nearer the escape boundary belongs to the soundness oracle. | doc §1.9, FREEZE §6.13 F-MEM-5; probe (d1.tr, i1.tr) |
| A `defer` runs when its block exits: one that reads through a `&?S` variable which a later statement of the block may set to null is E081, so the generator puts no `defer` in a walk body (whose last statement moves `w`). | turflang 2f49c7e (before it this compiled and dereferenced null; generator seed found by `gen/reduce.py`'s validity sweep, `findings/unsound/deferred-nullable-read`) |
| Walks are `while (w != null && c < K) { c++; ...; w = w.rJ; }` with a literal `K`: the counter ends a walk on a cycle. A recursive walker takes the depth `d` first, returns on `t == null`, then on `d <= 0`, and calls itself on 1-3 edges; callers pass a literal depth small enough for about 40 calls. | scope |
| An array of references is spelled `(&S)[k]` (`&S[k]` is a reference to an array). It is a value: copying the array copies the references, the nodes are shared. `(&S)[k]` is always declared with a literal (no initializer is E128); `(&?S)[k]` may be declared bare, every element null until written. An element is read through like any reference (`a[i].val`), and an element of `(&?S)[k]` is a non-variable `&?S` (never dereferenced). | doc `arrays.md` "Arrays of references", FREEZE §6.28 F-REFARR-1/2; turflang a7a881a |
| An array literal of `&S` and `null` (or `&?S`) is a `(&?S)[k]`, and a literal of `&S` may be assigned to a `(&?S)[k]`; a variable of `(&S)[k]` is not assignable to `(&?S)[k]`, and a `(&?S)[]` parameter does not take a `(&S)[k]`. | FREEZE §6.28 (literals widen, views never); turflang 2026-10-12 (found by this generator: seed 7180's `a1 = [nb0(..), heap(..)]` into a `(&?N0)[3]` was E066) |
| Not generated: `heap()` of a non-struct, references to scalars (`&int` has no read syntax that `printline` accepts), references as generic arguments or in `Stack`/`Queue`/`Set`/`Map`, `immut` references (no write through one: E060). | probe (a3.tr, c5.tr, e3.tr) |

## `Stack`, `Queue`, `Set`, `Map`

`Stack<int>`, `Queue<int>`, `Set<int>`, `Map<int, int>` and `Map<string, int>`
locals. Like a List, each is a handle (F-COLL-1): `mut d: Map<..> = c;` and
`d = c;` share one collection, `c.copy()` is independent. Keys and members
come from a small domain (`e % 4..16`, short literals) so lookups hit.

| Rule | Source |
|---|---|
| `Stack`: `push`, `pop`, `peek` (the last pushed), `length`. `Queue`: `enqueue`, `dequeue`, `peek` (the oldest), `length`. On an empty one: `prog.tr:<line>: Stack.pop: pop on empty stack`, `Stack.peek: peek on empty stack`, `Queue.dequeue: dequeue on empty queue`, `Queue.peek: peek on empty queue`. | doc `collections.md`; `stack.tr`, `queue.tr`; probe (k.tr) |
| `Set`: `add` (no-op if present), `contains`, `index_of` (dense position or -1), `length`, `remove` (a `bool`), and `s[i]`, the i-th member in dense order for `0 <= i < length` only (**no negative index**; otherwise `Set[]: index out of bounds`). Members stay in insertion order until a `remove`, which moves the last member into the hole. | doc `collections.md`; `set.tr`; probe |
| `Map`: `put` and `m[k] = v` overwrite in place or append; `get` and `m[k]` panic `Map.get: no such key` on a miss, with the caller's line for both; `contains`, `index_of`, `length`; `remove` (a statement) moves the last entry into the hole; `(m.try_get(k) recover produce d)` evaluates `d` only on a miss; `keys()` / `values()` are fresh `List`s in dense order. `m[k] = v` evaluates `v` before `k`. | doc `collections.md`, `operators.md` (value first); `map.tr`; turflang 1c7b790 (before it `m[k]` named a line of map.tr, against F-API-3) |
| `copy()` keeps the dense order (a Queue's from its head). | `LEC-009.md`; turflang `tests/51_shared_collections/` |

## `double`, `int32`, `char` and conversions

| Rule | Source |
|---|---|
| `double` is IEEE binary64. `+ - *` never trap: overflow is `inf`, and `inf - inf`, `0 * inf` are NaN. `/` and `%` by zero (also `-0.0`) trap `Division by zero` / `Modulo by zero` (with ` in compound assignment` for `/=`, `%=`), which is why a NaN is only reachable through an overflow. `%` is C `fmod` (sign of the dividend). Unary `-` never traps. | doc `FREEZE.md` 6.2 (row 6 and the division bullet), `operators.md`; probe (f1.tr, c1.tr) |
| Comparisons are IEEE: every ordered comparison with a NaN is false, `!=` is true; `-0.0 == 0.0`. | `src/CodegenExpr.cpp` (OEQ/UNE/OLT...); probe |
| A double prints, and converts `to string`, as C `%g` (6 significant digits: `0.333333`, `1e+21`, `-0`, `inf`); **a NaN is always `nan`**. Python's `%g` is the same function. | doc `io_and_net.md`; owner decision 2026-10-12, turflang 402eb39 / e0c6ea0 (before: `-nan` at -O0, `nan` at -O2) |
| `(i to double)` rounds to nearest. `(d to int)` truncates toward zero and **traps `Integer overflow`** for a NaN, ±inf or a value outside [-2^63, 2^63); `(d to int32)` likewise outside (-2^31 - 1, 2^31). | doc `FREEZE.md` 6.2 row 5, `variables_and_types.md` ("Legal conversions"); owner decision 2026-10-12, turflang e0c6ea0 (before: LLVM poison, INT_MIN at -O0 and garbage at -O2) |
| `(i to int32)` keeps the low 32 bits (signed); `(x to int)` of an int32 sign-extends. **`int32 + int32` is an `int`**, so the generator does no arithmetic on int32: it converts in, stores, and converts out. | doc `variables_and_types.md`; probe (c1.tr: `mut s: int32 = b + b;` is E066) |
| `char` is an unsigned byte: `(i to char)` keeps the low 8 bits, `(c to int)` is 0..255, literals are `'a'`; `== != < > <= >=` compare unsigned. Chars are printed only as `(c to int)`. | doc `variables_and_types.md` ("char"); turflang c851cac (before: ordering was signed, so a byte above 127 sorted below ASCII) |
| Converting a literal behaves like converting a value, at run time: `(5000000000 to int32)` is 705032704, `(353 to char)` is 97, `(1e300 to int)` panics; none is a compile error. | probe |
| Double literals are written with Python's `repr` (`1.5`, `1e+300`, `5e-324`); literal-only double arithmetic folds without error (`1e300 * 1e10` is `inf`). | doc `FREEZE.md` 6.2 row 1; probe (f1.tr) |

## Enums and `compare`

| Rule | Source |
|---|---|
| 1-2 enums of 2-4 variants, as locals, struct fields and array elements. A zero-filled or defaulted enum is the first variant. `(v to int)` is the 0-based ordinal, `(v to string)` the variant's name, `En::COUNT` the number of variants (an `int`, also under a cast: turflang 94c78ae); `==`/`!=` compare the same enum type; ordering operators are not defined. | doc `structs_and_enums.md` ("Enums"); probe (n1.tr) |
| `(e to En)` from a run-time int is checked: outside `0 <= e < n` it panics `Runtime Error: Invalid int to enum conversion.` The generator only casts `((e % n) + n) % n`. | doc `structs_and_enums.md`; owner decision 2026-10-12, turflang 0794134 (before: unchecked, a value named `<unknown enum>`) |
| `compare (s) { matches p: [matches q:] body done ... otherwise: body }`: the subject is evaluated once, the first arm with an equal pattern runs, else `otherwise`, else nothing. Patterns in one `compare` are distinct. An enum `compare` covers every variant or has `otherwise`; an int one need not. An arm body is never empty (E058) and is a block for `defer`. `break`/`continue` in an arm target the enclosing loop. Negative literal patterns (`matches -1:`) are fine. | doc `control_flow.md`, `structs_and_enums.md` ("Exhaustiveness"); probe (n1.tr, n2.tr) |

## Indexing

| Rule | Source |
|---|---|
| An index `i` into `N` elements is valid for `-N <= i < N`; a negative index counts from the end, once (no modular wrap). Same rule for `T[N]`, `T[]` and `List`. | doc `arrays.md` ("Negative indices"), `collections.md` |
| A provably out-of-range constant index into a `T[N]` is a compile error, so the generator indexes a `T[N]` only with a literal in range, `e % N` (always in range, sign of the dividend), or `opaque(e)`, a helper the range analysis cannot see through. A slice is indexed with `0`, `-1` (never empty), `e % s.length()` or `opaque(e)`; a List with `e % l.length()` under a `l.length() > 0` guard, or `opaque(e)`. | doc `arrays.md` ("Bounds checking"); probe (a2.tr) |
| An out-of-range array index traps `Array index out of bounds for '<name>'`, where `<name>` is the indexed variable, or `array` for any other base (a field, an element). Line: the statement's. | `src/CodegenArray.cpp` ArrayDisplayName; turflang a89bd67 (before it, every `T[N]` said `array`) |
| An out-of-range List read prints `prog.tr:<line>: List[]: index out of bounds`, a write `prog.tr:<line>: List[]=: index out of bounds` (turflang 8467172; before it, no location); `pop` on an empty List prints `prog.tr:<line>: List.pop: pop on empty list`. All abort. | probe (l2.tr, l3.tr, l4.tr) |

## Evaluation order of places

| Construct | Order | Source |
|---|---|---|
| `a[i] = e`, `s.f[i] = e`, nested places | `e` first, then the indices outer to inner, then the bounds check | probe (o1.tr, o3.tr) |
| `a[i] op= e` | `e`, then indices, then read-modify-write | probe |
| `l[i] = e` on a List (any `operator []=`) | `e`, then `i`, then the bounds check, like an array | owner decision 2026-10-11 (value first everywhere); doc `operators.md` Assignment. Before, `i` came first |
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

| `TURFC_ASAN` (oracle) | the compiler of a tree configured `-DTURF_RT_SANITIZE=ON` (default `$TURF_ROOT/build_asan/turfc`): every program it compiles links `libturfrt_asan`, the runtime and collector under ASan + UBSan. Run with `ASAN_OPTIONS=detect_leaks=0`, plain and under `TURF_GC_STRESS=1`, at `-O0` and `-O2`. The program's own code is not instrumented: a report means the runtime touched freed or foreign memory (a live block collected, then scanned, copied or printed). | `AGENTS.md` ("An ASan runtime"), `CMakeLists.txt` `TURF_RT_SANITIZE`; verified with a planted double free |

Both are runtime variables of the compiled program (`runtime/turfrt_gc.c`),
so the oracle sets them when it runs the `-O0` binary. `List` growth and
`.copy()` allocate on the GC heap, with Lists, views and arrays of structs
as roots in locals, so these runs exercise root finding.

## Open questions

* Evaluation order is left to right by probe only for operators and call
  arguments. (Assignment's value-first order is documented: `operators.md`.)
* List panics use the stdlib's `file:line: message` shape, not the
  compiler's `panic: runtime check:` one; that is FREEZE F-API-3, not a gap.
* `-g` alone uses the default optimization level, not `-O0`.

## Escape boundary (F-MEM-5): the soundness oracle

`oracles/soundness.py` builds every combination of a source, a sink and a
control-flow wrapper (1166 programs) and pins each verdict. Compiler under
test: turflang 1de8105.

| Rule | Source |
|---|---|
| A reference into the current frame (`&n` of a local or by-value parameter; a variable holding one; a `give` both of whose branches are one) is rejected with E086 when it flows into a `return`, a field reached through a parameter, `self` or a `heap()` reference, a `List`/`Map`/`Stack`/`Queue` mutator (`push`, `put`, `enqueue`) or `[]=`, or `heap()`'s operand. The message names the sink: "can't be returned", "can't be stored into a field", "can't be pushed into a collection". | doc `FREEZE.md` §6.13 F-MEM-5, `pointers_and_references.md` §1.9; probe (`findings/unsound/escape-boundary`) |
| The verdict does not depend on control flow that leaves the source alone: a loop before the sink, the sink inside a `while`, `for`, nested loops, `if`, `else`, a block or a `compare` arm. | turflang 9398f38 (before it, any loop made every provenance Unknown and the escape compiled) |
| `(c give &a otherwise &b)` is a frame reference. | turflang 1de8105 (before it a `give` was always Unknown) |
| `heap(n)`, a variable holding one, a `give` of two, and a `&N` the caller passed in are accepted at every sink, and the value read back after the frame is gone (and the stack overwritten by `clobber()`) is the one stored: exactly `42`, at -O0, -O2 and under the ASan runtime with `TURF_GC_STRESS=1`. | F-MEM-5 ("Param- and Heap-provenance values are always allowed to flow anywhere"); probe |
| `l[0] = v` on a `List<&N>` takes exactly a `&N`: the oracle never puts a `&?N` there (F-OP-1). | probe (j1.tr) |
| Also a frame reference, rejected the same way (per-field tracking, owner 2026-10-12): `id(&n)` for an `id` that returns its reference parameter unchanged, and `&a[0]` of a local `N[2]`. Also sinks: a struct holding a frame reference (`h.head = &n`) returned, copied then returned, pushed onto a `List<H>`, or a node holding one boxed with `heap(m)` (the message adds "inside a struct or array value"); an array literal of one returned as `(&N)[1]`; a field two hops through a parameter (`h.own.next`), or through a by-value parameter's `&` field; an element of a `(&N)[]` parameter ("can't be stored into an array"). `id(heap(n))` is accepted. | doc `FREEZE.md` §6.13 (amended 2026-10-12), §6.28; turflang 8e16021, a7a881a |
| Decided 2026-10-12, run as `pinned/...`: a merge of a frame path with any other path (`if`, a loop that may not run, a mixed `give`) is E086 at a sink; an error payload is a sink; a field store through a call none of whose arguments reach the frame (`mk()`), or through a collection element (`l[0]`, `l.get(0)`), is E086; `p = heap(n); defer p = &n; return p;` is accepted (the value is computed before the defer runs) and `p = &n; defer p = heap(n); return p;` is E086. | FREEZE §6.13 (amended 2026-10-13); turflang 9afd843, d5c7825, 1d210f3, b7741c0; `findings/unsound/escape-boundary/report.md` |
