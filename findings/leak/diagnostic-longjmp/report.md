# PARTLY FIXED: every diagnostic raised through the query facade leaked its error object

- class: leak (LeakSanitizer, `fuzz_analyze` with ASan); the error object itself fixed in turflang e1dcd7a, the rest open
- found: the first libFuzzer run with leak detection on (item 5), compiler 2f49c7e. Any input with a compile error reproduces it; `min.tr` is turflang's own `tests/03_typechecker` E061 test
- size: about 170 bytes in 3 allocations per raised error (the error's message and hint strings)
- cause: `TurfError::raise()` (include/Errors.h) longjmps to the recovery point when one is active, as it always is under `turf::analyze()`. The jump skips the destructor of the temporary error object being raised (`InvalidAssignmentTargetError(...).raise()`, every `reportError` path the same), so its `std::string` members are never freed. The comment there already notes that longjmp skips destructors (for the parser's recursion guard), but not this.
- why it matters: turf-lsp runs the facade on every keystroke for hours; each diagnostic leaks. The batch compiler exits after one compile, so it is harmless there.
- fuzzing: `engine/run.sh` runs with `-detect_leaks=0` and `ASAN_OPTIONS=detect_leaks=0` (as `tier0` always has); otherwise the first invalid input ends the run.
- fixed (turflang e1dcd7a): `raise()` destroys the error object just before the jump, so its message, hint and related locations are freed. On `min.tr` the leak goes from 170 bytes in 3 allocations to 18 in 1. All 1169 engine seeds replay clean under ASan.
- still open: the jump also skips temporaries of the raising full-expression (`describeNonLValue(...)`, any message built with `+`) and locals of every frame between the raise and the `setjmp`. Removing those needs the recovery path to unwind: a C++ exception caught at the ~30 `setjmp(g_recoverJmp)` sites instead of `longjmp`. An engineering change to the error path, outside the fuzzing roadmap.
