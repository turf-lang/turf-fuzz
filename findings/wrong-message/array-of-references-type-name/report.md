# FIXED: an array of references could not be written, and the error printed one type name for two types

- class: wrong message / language gap
- found: probing for reference-graph generation, compiler 813fb53
- repro: `min.tr`. `mut a: &N[2] = [mk(1), mk(2)];` was E066 "Cannot initialise '&N[2]' variable 'a' with value of type '&N[2]'": the declared type parses as a reference to `N[2]`, the literal is an array of two `&N`, and both printed as `&N[2]`. `(&N)[2]` was E109
- consequence: there was no spelling for "array of references" (`&?N[2]` is a nullable reference to an array too). The generator stores references in `List`s and struct fields only
- decision (owner, 2026-10-12; FREEZE §6.28 F-REFARR-1/2): `&` keeps taking the whole type, so `&N[2]` stays a reference to an array; `(&N)[2]`, `(&?N)[2]` and the slice parameter `(&N)[]` are arrays of references. A `(&N)[k]` declaration needs an initializer (E128); `(&?N)[k]` starts all-null
- fixed (turflang a7a881a): the parser accepts the parentheses; the two types print as written, `(&N)[2]` and `&(N[2])`; the E066 on `min.tr` now reads "Cannot initialise '&(N[2])' variable 'a' with value of type '(&N)[2]'" with a hint naming the parenthesised spelling. Storing a frame reference into an element of a `(&N)[]` parameter is E086. Tests: turflang `tests/52_arrays_of_references/`
- generator (2026-10-12): `(&S)[k]` and `(&?S)[k]` locals, with literals, bare nullable declarations, element reads, writes, copies and comparisons. Its first runs found that a literal of references did not widen to `&?S` on assignment or with a `null` element (fixed in turflang c89f429, seed 7180)
