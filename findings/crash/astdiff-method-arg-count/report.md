# Front-end crash: `if` branches that call methods with different argument counts

- class: crash in `turf::analyze()` (libFuzzer `fuzz_analyze_tok`, `std::vector::operator[]` assertion; an out-of-range read without assertions)
- found: engine tier, nightly 2026-10-17, inputs `crash-a26c54cc…` (`input.tr`) and `crash-ede891a1…` (turf-fuzz #14)
- signature: `vector::operator[]` <- `CollectChangedNodeIDsInternal` (`ASTDiff.h`, MethodCallExprAST) <- `LintIf`
- repro: `min.tr`, `if c { acc.add2(1, 2); } else { acc.add(1); }`
- cause: the identical-branch lint diffs the branches; for two method calls with different argument counts it marked them changed and then still paired their arguments, reading past the shorter list
- fixed: turflang, the diff stops at a count mismatch like every other list in `ASTDiff.h`. Test: `tests/14_cfg_analysis/branches_differ_in_method_argument_count.tr`
