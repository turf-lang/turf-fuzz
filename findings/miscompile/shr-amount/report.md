# Miscompile: `>>` by an amount outside [0, 64)

- class: miscompile (result varies with -O level)
- found: while probing shift semantics for the generator's bitwise operators, compiler 479a3bc
- repro: `min.tr`; `-5 >> 64`, `12345 >> 64`, `-5 >> -1`, `b >>= 64`
- observed: -O0 printed the operand unchanged; -O1 and up printed stack addresses
- cause: `>>` and `>>=` emitted a bare `ashr`, which is poison for an amount >= 64 or negative. `<<` already had the amount check
- decision (owner, 2026-10-11): trap like `<<`, same check and the same `Integer overflow` text
- fixed: turflang e9a0617 (EmitCheckedShr; tests `36_numeric_semantics/08_right_shift_amount_checked.tr` and two `fail_runtime_overflow_shr_*` twins)
