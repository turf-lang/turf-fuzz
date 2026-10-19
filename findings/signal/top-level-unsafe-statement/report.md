# Compiler crash: a statement inside a top-level `unsafe { }` block

- class: compiler crash (SIGSEGV at every -O level, in `VarDeclExprAST::codegen()`)
- found: tier 0, from `tests/05_arrays_pointers_ffi/fail_null_raw_pointer_read_faults_at_zero.tr`, nightly 2026-10-18 on turflang master ca41311 (turf-fuzz #15, signature `signal/ea7f3d44455a`)
- repro: `min.tr`, `unsafe { unsafe { mut p: int* = null; } }` at the top level. Without a nested block and with a `main`, `unsafe { printline(1); }` compiled and the call never ran
- cause: a top-level `unsafe { }` wraps declarations, and `AppendFlattenedTopLevelDecl()` spliced every child into the file's declaration list, statements included, so Codegen ran them with no enclosing function
- fixed: turflang PR #283, E118 for a non-declaration in a top-level `unsafe { }`. Test: `tests/05_arrays_pointers_ffi/fail_statement_in_top_level_unsafe_block.tr`
