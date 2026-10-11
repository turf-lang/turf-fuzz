# Compiler abort: `give`/`otherwise` as an array literal's first element

- class: compiler crash (LLVM fatal error after the e2560c1 guard; a segfault before it), 33 hits in a 2-minute -O0 run
- found: generator, compiler e2560c1
- signature: `LLVM ERROR: Internal error: array literal element has no type` <- `ArrayLiteralExprAST::codegen`
- repro: `min.tr`, `f0([(c > 2 give 3 otherwise 4), 5])`. Related: `(c > 2 give [7, 2] otherwise [3, 4])[0]` aborted with "Could not determine type of array/list base"
- cause: the same gap as `length-in-array-literal`, wider: codegen's `getExprType()` cannot type every expression (no ternary case). The literal now takes its element type from the TypeChecker's `CachedType`, and `getExprType()` types a ternary as its first arm
- fixed: turflang 44406a5 (`tests/05_arrays_pointers_ffi/23_give_otherwise_inside_array_literal.tr`)
- note: other expression kinds `getExprType()` still does not handle include `CharExprAST`, `PostfixExprAST`, `SizeOfExprAST`, `TryExprAST`, `RecoverExprAST`; an array literal no longer depends on them, but other codegen callers might
