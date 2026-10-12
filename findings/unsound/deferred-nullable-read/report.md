# Accepted and faulted: a deferred read of a `&?T` that the block later sets to null

- class: accepted-but-unsound (null dereference in safe code: SIGSEGV at address 0x8)
- found: while checking `gen/reduce.py`'s validity rules over 1500 generated programs, one failed in the generator itself: the evaluator's "cast of null to &T" assertion fired in a `defer` inside a list walk. Compiler 1de8105; there since LANG-003 (defer)
- repro: `min.tr` (`while (w != null && c < 3) { c++; defer io::printline(w.val); w = w.next; }`), `min_if.tr` (`if (w != null) { defer io::printline(w.val); w = null; }`, garbage instead of a fault)
- cause: NullabilityPass checks a deferred statement where it is written, inside the null check; it runs when the block exits, after the assignment
- fixed: turflang 2f49c7e, E081 (E082 for a cast) when a later statement of the block may set the variable to null; `tests/40_defer/fail_defer_reads_nullable_assigned_later.tr`, `defer_reads_nullable_ok.tr`
- generator: no `defer` in a walk body any more (it was a generator bug as well: the program it emitted was never valid)
