# Compiler hang (SIGSEGV under a stack limit): `void` as a field's type through a type argument

- class: signal (SIGSEGV with `-g --emit-obj`; a hang without the stack limit), in Codegen asking LLVM for the alignment of the instantiated struct
- found: nightly process tier (pinned), from `tests/38_monomorphization/generic_instantiation_only_in_signature.tr` (turf-fuzz #30, signature `signal/9c370d7576f2`)
- repro: `min.tr`; `fn f(b: Box<void>): int` or `mut b: Box<void> = Box<void>::default();` with `struct Box<T> { v: T }` does the same, and so does `T[4]` as the field's type
- cause: a field written `v: void` is E112 in the parser, but `Box<void>` makes the instantiated field `void` with nothing checking it
- fixed: turflang ee84345 (branch `roadmap/v2.3.0`): E112 at the use site, naming the instantiation and the field; `void` still fills a parameter the struct only points at (`p: T*`). Tests: `tests/38_monomorphization/fail_generic_void_field_type_arg.tr`, `fail_generic_void_array_field_type_arg.tr`, `generic_void_pointer_type_arg_ok.tr`
