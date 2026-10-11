# Compiler crash: `a.length()` as an array literal's first element

- class: compiler crash (SIGSEGV), 73 hits in the first 2-minute -O0 run of the aggregate generator, one signature
- found: generator seed 5038, compiler a89bd67
- signature: `CastToType` <- `ArrayLiteralExprAST::codegen` <- `FuncCallExprAST::codegen`
- repro: `min.tr`, `f0([a1.length(), 5])`. Also `b = [a1.length(), 5]` and `[a1.copy().length(), 5]`; a declaration's initializer took another path and worked
- cause: codegen's own `getExprType()` had no case for the array builtin `length()`, so the literal's element type was null and `CastToType` dereferenced it
- fixed: turflang e2560c1 (`tests/05_arrays_pointers_ffi/22_length_inside_array_literal.tr`)
