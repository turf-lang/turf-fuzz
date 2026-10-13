# ice: Internal compiler error: generated LLVM IR is invalid.

- signature: `Internal compiler error: generated LLVM IR is invalid.`
- class: ice
- hash: f3bfb63be78f
- occurrences: 2
- first seen: 2026-10-12 14:40:06Z
- compiler.lock commit: 900f330c341435521e5f3ec0c378e2199f2d6de4
- checkout HEAD: ef80c3db257b5de3529ba22e0b91dfa299442487  (DIFFERS from compiler.lock)
- command: `/home/a2ys/turflang/turf-fuzz/../turflang/build_san/turfc min.tr -o a.out -Os --emit-obj`
- flags: -Os; ulimit -S -s 65536
- seed file: tests/07_functions_modules/fail_static_method_via_instance.tr
- fuzz seed: 202642500000
- worker: 3
- minimized: yes

minimized with 31 probes.

Inputs that import sibling modules must be compiled beside them; see `seed file` and `tier0/README.md` (`--replay-beside`).

## Compiler output (tail)

```
[1m[96m__fuzz_w3.tr[0m [90m(1 warning)[0m

[1m[93m── warning [0m[90mat line 7, col 5[0m

  [1m[93mWarning: [0m[93mVariable '[36my[93m' is declared but never used.[0m

  [92mHint: If the variable is intentionally unused, prefix its name with '_' by convention. Otherwise, remove the declaration or use its value.[0m

[90m  7 │ [0m    immut y: int = mu.square();
[90m    │ [0m    [1m[93m^ Here![0m

Internal compiler error: generated LLVM IR is invalid.
This is a compiler bug, not a mistake in your Turf code -- please report it.

Call parameter type does not match function signature!
  %mu = alloca %MathUtil, align 8
 i64  %methodcalltmp = call i64 @turf_MathUtil.square_int(ptr %mu)
```

## Triage (2026-10-12)

- `mu.square()` on a static `square(x: int)`: the '.' call's receiver was counted as `x`, so the static-method check (which needed the counts to mismatch) never fired, and Codegen failed on the call it built
- found by `ci/sanitized.sh`'s first local dry run (seed 202642500000); an ICE (LLVM verifier), not a sanitizer report
- fixed: turflang 7ec1581 (`tests/07_functions_modules/fail_static_method_via_instance_no_args.tr`)

## A second bug under the same signature (2026-10-13)

The signature is the generic verifier message, so a different ICE counted as
a duplicate hit of this fixed finding (occurrences 1 → 2) and its input was
thrown away. Re-run with `--keep-dups` / a fresh `--out` (tier 0, seed
202642000000, 8 jobs, the hit near 140 s) to recover it: `min2.tr`, from
`input2.tr` (seed file `tests/45_pattern_matching/fail_pattern_missing_field_without_rest.tr`,
`-O2 --emit-obj`).

- `compare p { otherwise: return 0; }` on a struct `p`: with no arms the
  compare is not a pattern match, so it fell through to the integer switch,
  whose `CastToType(struct, int)` emitted an FCmp on a struct value ("Both
  operands to FCmp instruction are not of the same type!"), at every level
- not caused by REL-005's commits: that code dates from LEC-003 (2026-10-01);
  the mutator had not produced the input before
- fixed: turflang 91e784a (`tests/45_pattern_matching/21_compare_with_only_otherwise.tr`):
  an only-`otherwise` compare runs its body whatever the subject's type
- `tier0/crash_fuzz.py --keep-dups DIR` (added with this note) keeps every
  duplicate's input from now on, so the next one under a generic signature is
  not lost
