# FIXED: every diagnostic raised through the query facade leaked

- class: leak (LeakSanitizer, `fuzz_analyze` with ASan)
- found: the first libFuzzer run with leak detection on (item 5), compiler 2f49c7e. Any input with a compile error reproduces it; `min.tr` is turflang's own `tests/03_typechecker` E061 test
- size: about 170 bytes in 3 allocations per raised error at first, 18 bytes in 1 after the first fix
- cause: `TurfError::raise()` (include/Errors.h) longjmped to the recovery point, as it always does under `turf::analyze()`. The jump skipped every destructor between the raise and the `setjmp`: the error object, the temporaries that built its message, and the locals of every abandoned frame.
- why it matters: turf-lsp runs the facade on every keystroke for hours; each diagnostic leaked. The batch compiler exits after one compile, so it was harmless there.
- partial fix (turflang e1dcd7a): `raise()` destroyed the error object by hand before the jump. 170 bytes to 18 on `min.tr`.
- fixed (turflang b48c297, 2026-10-12): recovery is a C++ exception (`RecoverUnwind`, include/Lexer.h) caught at each former `setjmp` site, so a recovered error unwinds. `min.tr`: no leak. All 640 of turflang's `fail_*` tests outside the nesting-limit (E111) ones replay through `fuzz_analyze` with `detect_leaks=1`: no leak. The E111 tests still lose the over-deep tree on purpose (`AppendFlattenedTopLevelDecl()` releases it: freeing it would recurse as deep as the passes it protects against).
- fuzzing: `engine/run.sh` and `ci/engine.sh` still pass `-detect_leaks=0`; they can drop it once the E111 release has a suppression or an iterative destructor.
