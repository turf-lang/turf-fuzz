# Compiler hang (SEGV under a stack limit): a type argument that makes a parameter `void`

- class: sanitizer (`AddressSanitizer: SEGV` with `-O2 --emit-obj`); a hang without the stack limit
- found: nightly sanitized tier, from `tests/07_functions_modules/visibility/visibility_pub_access.tr` (turf-fuzz #39, signature `sanitizer/6de2ecf321f0`); compile `min.tr` beside that directory's `visibility_lib_module.tr` (`--replay-beside`)
- repro: `min.tr`; `fn<T> w(x: T)` called as `w(continue)`, `w(break)` or `w(v())` with `v(): void` does the same in one file
- cause: the call infers `T = void`, and nothing checked the instantiated parameter (the parser's E112 sees only a parameter written `x: void`), so Codegen asked LLVM for the alignment of a `void` slot forever. The same hole as `signal/void-type-argument-field` (#30), for a parameter
- fixed: turflang 3648af44 (branch `roadmap/v2.3.0`): E112 at the call, naming the instantiation and the parameter; `void*` is still accepted. Tests: `tests/38_monomorphization/fail_generic_void_param_from_continue.tr`, `fail_generic_void_param_from_void_call.tr`
