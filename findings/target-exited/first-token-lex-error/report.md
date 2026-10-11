# libFuzzer: fuzz target exited

- class: compiler crash on invalid input (host process killed)
- engine: `engine/fuzz_analyze`, 20 min, -fork=4, no prelude, compiler cba079f
- signature: `exit` from `TurfError::raise()` <- `gettokScan()` <- `parseEntryFile()` first `getNextToken()`
- cause: the facade lexed a file's first token before arming the recovery guard; a lexer error there called `exit(1)` (would kill turf-lsp)
- fixed: turflang 479a3bc (+ turf-api-tests `testLexErrorInFirstToken`)
