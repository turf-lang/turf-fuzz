# Compiler crash: `give`/`otherwise` arms that differ in an `INT_MIN` literal

- class: compiler crash (SIGSEGV at `-O0`, in the lint pass)
- found: differential oracle, seed 2026288000245, at the lock 09118c8 (2.0.0); seen nightly on the pinned lane until the lock moves
- repro: `min.tr`, `chk = (false give -9223372036854775808 % 0 otherwise -2 + -3290145192257976335);`
- cause: the same as `crash/lint-branch-compare-int-min-literal`: the identical-branch check compares the two arms' literals with `APInt ==`, and `-9223372036854775808` is stored wider than the other literal. A `give`/`otherwise` is an `if` expression to the lint
- fixed: turflang fac8831 (`APSInt::isSameValue`), on master since 2.1.0; does not reproduce there. Bump `compiler.lock` past it
