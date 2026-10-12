# Rejected valid: a `&?T` null check was lost at every loop

- class: rejected-valid (E081 on a program with no path to a null dereference)
- found: probing for reference-graph generation (turf-fuzz item 3, slice 1), compiler 813fb53; in the compiler since 879ff15 (2026-09-23, when NullabilityPass landed)
- repro: `min.tr`. Inside `if (q != null) { ... }`, a `while` that never mentions `q`, then `q.val`: E081. The same for a use inside a `while`, `for r in a..b` or for-each body, and after `if (p == null) { return; }`
- cause: NullabilityPass is a must-analysis (meet = intersection) but started each block's IN set empty, not full. A loop header met its unprocessed back edge, came out empty, and never recovered. DefiniteAssignmentPass, which it was modelled on, starts from the full set
- fixed: turflang d896209 (`tests/39_nullable_references/08_narrowing_survives_loops.tr`, `fail_narrowing_lost_when_loop_assigns.tr`)
- generator: without the fix no loop could be generated inside or after a null check, so this was fixed before slice 1 was written
