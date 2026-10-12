# Rejected: `(En::COUNT to double)`; and `(En::COUNT to string)` was "<unknown enum>"

- class: rejected-valid (E134), 3 hits in a 3-minute run; the `to string` form is a silent wrong answer
- found: generator (enums + doubles, turf-fuzz item 2), compiler 402eb39
- signature: `rejected E134 Cannot explicitly cast 'En0' to 'double'`
- repro: `min.tr`; `input.tr` is the generated program
- cause: Codegen's `getExprType()` typed every enum `::` reference as the enum, `COUNT` included, though the TypeChecker types it `int` and arithmetic on it worked
- fixed: turflang 94c78ae (`tests/15_enums/23_enum_count_is_an_int.tr`)
