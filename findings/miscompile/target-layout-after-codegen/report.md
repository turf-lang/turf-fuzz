# Miscompile: codegen sized types with LLVM's default layout

- class: miscompile / memory corruption from safe code
- found: generator + GC differential, seed 100328 (20-minute run, compiler 44406a5). Configurations disagreed; every one hit `malloc(): corrupted top size`
- repro: `min.tr` printed 534 instead of 12: `.copy()` of a `{bool, int, bool[4]}[3]`
- cause: the module got the target triple and data layout only after codegen (in front of optimization), so every byte size codegen computed (array `.copy()`, `sizeof`, element type descriptors) used LLVM's default layout, which aligns i64 to 4. The struct came out 16 bytes instead of 24; `.copy()` allocated 48 bytes and the GEPs, laid out with the real layout at emission, wrote 72. `sizeof`, which sizes every `List<T>` buffer, was short the same way
- located with: `gen/trace.py 100328` (last trace point before the abort was the `.copy()`)
- fixed: turflang f6ddd39 (target machine built right after `InitializeModule()`; `tests/05_arrays_pointers_ffi/24_padded_struct_sizes_use_target_layout.tr`, which fails on the old compiler)
