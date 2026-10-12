# hang: hang

- signature: `hang`
- class: hang
- hash: 2aca73797065
- occurrences: 1
- first seen: 2026-10-12 13:51:13Z
- compiler.lock commit: c77a3a87d08210d211a8c7658b198d82bb25fc41
- checkout HEAD: c77a3a87d08210d211a8c7658b198d82bb25fc41
- command: `/home/a2ys/turflang/turflang/build/turfc min.tr -o a.out -O2 --emit-obj`
- flags: -O2
- seed file: tests/12_string_ops/09_string_concat_with_casts.tr
- fuzz seed: 202642000000
- worker: 4
- minimized: yes

minimized with 27 probes.

Inputs that import sibling modules must be compiled beside them; see `seed file` and `tier0/README.md` (`--replay-beside`).

## Compiler output (tail)

```
timeout
```

## Triage (2026-10-12)

- not a hang in the usual sense: exponential time. Codegen's `FieldAccessExprAST::codegenLValue()` retried each link of an invalid `x.a.a...a` chain as a place and then as a value, doubling the work per `.a` (20 links 2.6 s, 22 links 10.6 s); the input has ~1000 links. The type checker never rejected a field on a non-struct value, so Codegen's E121 (internal compiler error) backstop was the only diagnostic
- found by `ci/process.sh`'s first local dry run (tier 0, seed 202642000000)
- fixed: turflang 900f330 (E068 "A value of type 'string' has no fields" from the type checker; this input now fails in 0.19 s), tests `tests/06_structs/fail_field_on_non_struct_value.tr`, `fail_field_chain_on_non_struct_is_fast.tr`
