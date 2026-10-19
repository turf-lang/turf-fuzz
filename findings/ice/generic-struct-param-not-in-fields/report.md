# ICE: a method call on a generic struct whose fields do not name its parameter

- class: internal compiler error, `LLVM function 'turf_Box.sizeOfT_Box_ptr' not found after successful name resolution`
- found: tier 0, from `tests/38_monomorphization/07_sizeof_t_in_struct_method.tr` (turf-fuzz #7)
- repro: `min.tr`. The body's missing `return` is incidental: `struct Box<T> {}` (or any struct whose fields never mention `T`) with `fn<T> Box.m(): int { return 1; }` fails the same way
- cause: the TypeChecker paired a `Box<int>`'s type arguments with the parameters its *fields* mention, in field order, so a parameter no field uses got nothing and the method call was never instantiated. The same pairing typed `p.a` as `string` on a `struct P<A, B> { b: B, a: A }` instantiated `P<int, string>`
- fixed: turflang, the struct type records its declared type parameters and every site (field access, struct patterns, method calls, Codegen's type queries) pairs by declaration order (`TypeWalk::structTypeArgSubst`). Now E009 names `'Box.sizeOfT'`. Tests: `tests/38_monomorphization/generic_struct_param_not_in_fields.tr`, `generic_struct_fields_out_of_param_order.tr`, `fail_generic_method_missing_return.tr`
