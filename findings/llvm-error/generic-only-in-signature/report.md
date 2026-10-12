# Internal error: a generic type named only by a function signature has no layout

- class: compiler internal error (main.cpp's opaque-instantiation scan), seed 247 of a local `--quick` check right after LEC-009
- found: generator (`List<int>` helper parameters, turf-fuzz eaed7e5), compiler 462df65
- signature: `Internal error: monomorphized struct instantiation 'list.List_gen1_int' was never given a real layout`
- repro: `min.tr`, `fn f0(p0: Box<int>): int { return 7; }` with no other `Box<int>` anywhere. `input.tr` is generator seed 247 (helper `f1(..., p2: List<int>)`, no `List<int>` local)
- cause: only locals, struct fields, calls and error payloads recorded struct instantiation requests; a parameter or return type never did. Predates LEC-009 (same failure on 9d3a9b6)
- fixed: turflang 6858502 (`tests/38_monomorphization/generic_instantiation_only_in_signature.tr`)
