# OPEN (minor): an array of references cannot be written, and the error prints one type name for two types

- class: wrong message / language gap, not fixed
- found: probing for reference-graph generation, compiler 813fb53
- repro: `min.tr`. `mut a: &N[2] = [mk(1), mk(2)];` is E066 "Cannot initialise '&N[2]' variable 'a' with value of type '&N[2]'": the declared type parses as a reference to `N[2]`, the literal is an array of two `&N`, and both print as `&N[2]`. `(&N)[2]` is E109
- consequence: there is no spelling for "array of references" (`&?N[2]` is a nullable reference to an array too: assigning `null` to `a[0]` reports the variable's type as `N[2]`). The generator stores references in `List`s and struct fields only
- not fixed: whether arrays of references should be expressible is a language question; the message alone could say `&(N[2])` vs `(&N)[2]`
