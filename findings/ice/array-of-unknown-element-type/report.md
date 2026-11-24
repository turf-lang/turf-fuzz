# ICE: a fixed-size array of an unknown element type

- class: internal compiler error, `I was asked to access a field of a type that is not a struct.` (E121)
- found: nightly sanitized tier (pinned and master), from `tests/compat/2.0/10_default_init/fail_array_of_struct_use_before_write.tr` (turf-fuzz #46, signature `ice/922ed432a9d8`)
- repro: `min.tr`; `mut pts: Point[3];` with no `Point` declared, then `pts[0].x`
- cause: the Resolver resolved a fixed-size array's element type without checking it, so the unknown `Point` reached the TypeChecker as a field access on a non-struct type
- fixed: turflang fbfa14c9 (branch `roadmap/v2.4.0`): the element type is checked like a variable's declared type, E042 at the declaration. Test: `tests/05_arrays_pointers_ffi/fail_unknown_array_element_type.tr`
