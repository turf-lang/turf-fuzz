# libFuzzer: fuzz target exited

- class: compiler crash on invalid input (host process killed)
- engine: `engine/fuzz_analyze_tok`, nightly (turf-fuzz #19), compiler 9667743
- signature: `exit` from `TurfError::raise()` <- `gettokScan()` <- `parseEntryFile()`'s top-level `;` step
- repro: `min.tr`, a top-level `;` followed by `.0.5` (E139, a number with a second decimal point)
- cause: the facade's entry-file loop stepped over a top-level `;` before arming the recovery guard, so a lexer error in the next token called `exit(1)` (would kill turf-lsp). Same class as `first-token-lex-error`
- fixed: turflang 8d145fd, the `;` step runs under the guard (+ turf-api-tests `testLexErrorOutsideAStatement`)
