# ICE: a `\` at the end of a line inside a string literal

- class: internal compiler error, `std::out_of_range` (`basic_string::compare: __pos (which is 4) > this->size() (which is 0)`), an abort
- found: nightly process tier (master), from `tests/compat/2.0/35_numeric_literals/09_dot_boundaries_still_lex.tr` (turf-fuzz #47, signature `ice/92a90aab0b38`)
- repro: `min.tr`; `"\` at the end of a line, then a top-level `mut` two lines later
- cause: the lexer read the newline after `\` as an escape without counting the line, so every later location was one line early; E087's fix-it then compared `mutable` at column 5 of an empty line, and `std::string::compare` threw
- fixed: turflang 593522c8 (branch `roadmap/v2.4.0`): a string or char literal stops at the newline (E099, unclosed), and every fix-it that compares the source at a column goes through `LineReadsAt()`, which is false past the end of the line. Tests: `tests/01_syntax/fail_string_backslash_at_line_end.tr`, `tests/17_char_type/fail_char_backslash_at_line_end.tr`
