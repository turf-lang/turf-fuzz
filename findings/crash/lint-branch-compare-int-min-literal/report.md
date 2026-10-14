# Compiler crash: an `if`/`else` whose branches differ in an `INT_MIN` literal

- class: compiler crash (SIGSEGV in `-O0` and every other level; a segfault in the lint pass, no user error)
- found: differential oracle on seed 1072 (`input.tr`, the generator's program) in the first minute against turflang 09118c8 (2.0.0); not checked at the previous lock 35c039f (turflang f229e9b, "identical `if`/`else` bodies are found for any statement", is the likely cause)
- signature: `llvm::APInt::equalSlowCase` <- `CollectChangedNodeIDsInternal` <- `LintIf` <- `LintExpression` <- `FuncDefExprAST::codegen`
- repro: `min.tr`, `if x == 0 { x = -9223372036854775808; } else { x = -7; }`. The same with `printline(38 <= -9223372036854775808)` against `... <= -7`. `x = 9223372036854775807 / -1` against `3 / -1` compiles
- cause: not investigated beyond the stack. The lint pass compares the two branches' literal nodes with `APInt ==` and the two values have different bit widths
- open: not yet reported to turflang. Once fixed, bump `compiler.lock` and pin it with a `fail_`/run test in turflang
