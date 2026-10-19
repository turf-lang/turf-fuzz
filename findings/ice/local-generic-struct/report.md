# ICE: a generic struct declared inside a function

- class: internal compiler error, `generated LLVM IR is invalid` (`Cannot allocate unsized type %Box`)
- found: turflang's own sanitized CI crash fuzz (run 38820775761, push of ca41311), from `tests/07_functions_modules/fail_type_identity_generic_without_type_args.tr`
- repro: `min.tr`. Written `Box<int>` instead, the program was E148 "'Box' is not generic"
- cause: a local struct is registered in the Resolver's second pass, after the generic structs are known and outside the monomorphizer's templates, so a local generic was neither instantiable nor rejected when written bare
- fixed: turflang, E151 (a struct declared inside a function cannot have type parameters). Test: `tests/06_structs/fail_local_generic_struct.tr`
