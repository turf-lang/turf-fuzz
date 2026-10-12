# Internal compiler error: `E::Variant(List::default())`

- class: llvm-error ("Internal compiler error: generated LLVM IR is invalid ... Cannot allocate unsized type %list.List")
- found: probing error payloads (turf-fuzz item 3, slice 2), compiler ec236f2; in the compiler since payloads landed (LEC-004)
- repro: `min.tr`. A default-constructed generic value (`List::default()`, `Map::default()`, a user `Box::default()`) as a payload argument of an error variant
- cause: `X::default()` takes its type arguments from context. The type checker recorded that context for a declaration, an assignment, a call argument and a `return` (turflang 4a0581e) but not for a payload argument, so the uninstantiated template reached Codegen
- fixed: turflang bfa72ba (`tests/25_errors/30_payload_generic_default_argument.tr`)
