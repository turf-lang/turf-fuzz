# ICE: a method whose receiver is its own type parameter

- class: internal compiler error, `Cannot determine the type of this method call's target -- the expression before '.length' has no known type` (E121)
- found: nightly process tier, from `tests/38_monomorphization/22_nested_generic_struct_over_stdlib_list.tr` (turf-fuzz #20, signature e0a05a38552e)
- repro: `min.tr`; `fn<T> T.count(): int { return self.x; }` fails the same way, with or without a `struct Holder` in the file
- cause: `fn<Holder> Holder.count()` names the method's own type parameter as its receiver, which shadows any struct of that name. The Resolver turned `self`'s pointee into the type parameter, so `self` had no fields or methods, and the body's first `self.items` had no type for the TypeChecker to report against
- fixed: turflang a39e31a, E131 at the declaration ("'Holder' is a type parameter of 'Holder.count', not a struct, so it cannot have methods"). Test: `tests/38_monomorphization/fail_method_on_own_type_parameter.tr`
