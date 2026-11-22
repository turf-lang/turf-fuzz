# Compiler crash (SIGSEGV): `@deprecated` with no argument on a function whose body does not parse

- class: signal (SIGSEGV in `DeprecatedUseWarning`), and under UBSan "reference binding to null pointer" in `WarnIfDeprecated`
- found: nightly process tier (master), from `tests/49_annotations/fail_deprecated_without_argument.tr` (turf-fuzz #38, signature `signal/c13569396488`); the sanitized tier found the same crash as #43 (`sanitizer/b09d263d68b8`, `min_ubsan.tr`)
- repro: `min.tr` (`-O2 --emit-obj`), `min_ubsan.tr` (`-O1`)
- cause: a `@deprecated` without its string argument is E146, but the symbol kept the annotation, and when the body also failed to parse the deprecation lint read the first argument of an empty list
- fixed: turflang d7c37b59 (branch `roadmap/v2.3.0`): the lint says nothing for such an annotation. Test: `tests/49_annotations/fail_deprecated_without_argument_bad_body.tr`
