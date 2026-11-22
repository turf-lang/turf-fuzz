# ICE: a method call on a field of an open type parameter

- class: internal compiler error, E121 "I could not work out the type of the expression before `.get`" with "Internal compiler error!"
- found: nightly process tier (master, then sanitized), from `tests/compat/2.0/38_monomorphization/operations_on_t_checked_per_instantiation.tr` (turf-fuzz #34, signature `ice/e0a05a38552e`)
- repro: `min.tr`, minimized to a long `a.a.a….get()` chain; `fn<T> f(a: T): int { return a.inner.get(); }` does the same
- cause: whether `T` has the field is known only per instantiation, so the field's type stays open, and the method call took the open type for a compiler bug
- fixed: turflang a0e59e50 (branch `roadmap/v2.3.0`): a call whose receiver reaches an open type parameter through fields, calls or indexes is left to the instantiation, which reports a missing field as E068. Tests: `tests/38_monomorphization/generic_field_chain_on_type_param.tr`, `fail_generic_field_chain_missing_field.tr`
