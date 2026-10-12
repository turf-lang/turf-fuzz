# OPEN (owner decision): `to` makes a safe reference out of an int, a string or another reference type

- class: accepted-but-unsound, not fixed
- found: reading `TypeChecker::visitCast()` while fixing `nullable-deref-not-a-variable`, compiler 813fb53 + d896209 + ec236f2
- repro, all in safe code with no `unsafe`:
  - `min.tr`: `mut p: &N = (0 to &N); p.val` compiles; garbage at the default level, SIGSEGV at -O0
  - `min_string.tr`: `(s to &N)` from a `string`, and `(16 to &?N)` which then passes its null check and faults at address 0x10
  - `min_retype.tr`: `(n to &M)` from a `&N` to an unrelated struct `M` with more fields, then a write past the end of the `N`
- cause: cast legality (`visitCast()`, the `Legal` table) allows `int -> any pointer type` and `pointer-like -> pointer-like`, where "pointer" includes `&T` and `&?T`. The `unsafe` gate (`checkUnsafeGate`) only looks at raw `T*`/`ptr`
- what the docs say: `variables_and_types.md` lists `int <-> pointer` ("reinterprets the integer as an address") and `pointer <-> pointer` as legal conversions and says raw-pointer casts need `unsafe`; FREEZE 6.6 says "`&T -> &?T -> T*` widens implicitly; nothing widens back without an explicit `unsafe`-gated `to`-cast" and that `&T` is "compile-time non-null". Neither says whether `int to &T` or `&A to &B` is meant to exist outside `unsafe`
- not fixed because the fix chooses a rule the spec does not state: reject these casts everywhere, or allow them inside `unsafe { }` only (as `T* -> &T` already is). Existing tests use `malloc(...) to &int` inside `unsafe` (`tests/08_e2e_stress/05_double_evaluation.tr`), which the second rule keeps
