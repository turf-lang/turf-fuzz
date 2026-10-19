# ICE: `Generic::default()` as a method receiver

- class: internal compiler error, `monomorphized instantiation of 'map::Map.get' failed to re-resolve`, plus an E148 inside `lib/std/collections/map.tr`
- found: tier 0, from `tests/07_functions_modules/fail_map_get_missing_key.tr` (turf-fuzz #8)
- repro: `min.tr`; `Box::default().get()` and `Box::default().v` on a user `struct Box<T>` too
- cause: `X::default()` takes its instantiation from where its value goes (a declared variable, a parameter, a field, a `return`). A receiver gives none, so the bare template was instantiated
- fixed: turflang, E138 ("'Map::default()' needs type arguments here") at the call. Tests: `tests/38_monomorphization/fail_generic_default_as_method_receiver.tr`, `fail_generic_default_as_field_base.tr`
