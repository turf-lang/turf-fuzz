# The F-MEM-5 escape boundary: ten holes fixed, four owner questions open

- class: accepted-but-unsound (a reference into a dead frame read back: garbage at -O0 after the next call clobbers the frame, often 0 at -O2)
- found: probing for the soundness oracle (turf-fuzz item 3, slice 4), compiler bfa72ba
- every file here has a `clobber()` call between the escape and the read; `main` prints the escaped node's `val`, which should be 42

## Fixed

- `k1_return_after_loop.tr` (also a loop before a push or a field store): EscapePass met a loop header's uncomputed back edge as an empty map, so every provenance became Unknown at every loop. Fixed in turflang 9398f38 (`tests/39_nullable_references/fail_escape_*_loop.tr`). There since FDN-002.
- `k1_return_give.tr`: a `give` was always Unknown; when both branches have the same provenance it now has it. Fixed in turflang 1de8105 (`fail_escape_return_give_of_locals.tr`).

## Open: the spec is silent, owner decision needed

- **One path Stack, another not** (`k1_return_cond.tr`: `p = heap(n); if c { p = &n; } return p;`; `k1_return_foreach.tr`: a loop that may or may not run, `give` with mixed branches). The pass meets disagreeing paths to Unknown (must-analysis) and never flags it. Flagging needs a may-analysis at merges; F-MEM-5 says "a Stack-provenance value", and FDN-002 says "never a false positive", so this is a policy choice.
- **A frame reference inside an error payload** (`k1_return_payload.tr`: `return E1::P(&n);`). Not a listed sink; the closest documented gap is "inside a by-value struct copy".
- **A field store whose base is a call result** (`k3_call_result_field.tr`: `mut h: &N = mk(); h.next = &n; return h;`) and **through a List element** (`k4_list_elem_field.tr`: `l[0].next = &n`): the base is Unknown, which is never flagged. Not listed among the documented gaps, but follows from the same "Unknown is permissive" rule.
- **defer false positive** (`k1_return_defer_assigned.tr`): `p = heap(n); defer p = &n; return p;` is rejected, though the return value is computed before the defer runs. A defer is analysed where it is written. Skipping it is unsound inside a loop (the next iteration sees `p = &n`), so the fix needs defers modelled at block exit.

## Fixed 2026-10-12: the documented residual gaps (FDN-002 Known pitfalls, INV-MEM-6)

Owner decision: reopen FDN-002's per-field Non-goal, keeping "never a false positive". turflang 8e16021 (EscapePass keyed by place; `ReturnsParam` call summary) makes all eight E086: `k1_return_struct_copy.tr` (by-value struct copy), `k1_return_via_call.tr` (intermediate call), `k1_return_arr_elem.tr` (`&arr[i]`), `k2_param_two_hops.tr` (`h.own.next = &n`), `k2_struct_param_byval.tr`, `k4_list_of_struct.tr` (a struct holding `&n` pushed), `k5_heap_struct_with_ref.tr` / `k5_heap_node_with_ref.tr` (`heap(m)` where `m.next = &n`). Pinned in turflang `tests/39_nullable_references/fail_escape_*` (struct, heap, push, call, array element, two hops, by-value parameter, array of references) with `escape_struct_fields_kept_local_ok.tr` for the safe shapes; the soundness oracle generates them as sources and sinks (2662 cases). The open questions above are unchanged.
