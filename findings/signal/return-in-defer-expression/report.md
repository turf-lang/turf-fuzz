# Compiler crash (SIGSEGV, unbounded recursion): a `return` inside an expression in a `defer`

- class: signal (SIGSEGV @ recursion `AssignmentExprAST::codegen`, `FlushPendingDefers`, `ReturnExprAST::codegen`)
- found: nightly process tier (master), from `tests/39_nullable_references/fail_escape_defer_overwrite_does_not_hide_local.tr` (turf-fuzz #42, signature `signal/63fe3719a222`)
- repro: `min.tr` (`defer p = return p;`); `defer g(return p);` does the same
- cause: the check that a deferred statement never returns, breaks or continues out of itself walked statement shapes only, so a `return` on an assignment's right-hand side or in a call argument reached Codegen, where flushing the defers at that `return` ran the same deferred `return` again
- fixed: turflang c73a30f8 (branch `roadmap/v2.3.0`): the check sees every child of the deferred statement, and such a jump is E083. Tests: `tests/40_defer/fail_return_in_defer_assignment.tr`, `fail_return_in_defer_call_argument.tr`, `jump_in_defer_expression_rejected.tr`, `08_defer_wraps_do_while_and_for.tr`
