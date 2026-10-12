# Open questions for the owner, to check at the end

Language decisions the fuzzing work found but did not make: each needs the
owner, because the spec is silent or a frozen decision would change. Parked
here on the owner's instruction (2026-10-12) so the fuzzing work can move on;
go through them once the turf-fuzz roadmap is done. Delete a line once it is
decided, and record the decision where it belongs (FREEZE.md, gen/SEMANTICS.md).

## From item 3 (GC/ownership), 2026-10-12

Details and reproducers: `findings/unsound/escape-boundary/report.md`,
`findings/unsound/int-to-reference-cast/report.md`. `python3
oracles/soundness.py --gaps` shows what each does today.

- **Mixed paths and E086.** Should F-MEM-5 apply when only some paths carry
  the local reference? `p = heap(n); if c { p = &n; } return p;`, or a `give`
  with a local and a `heap()` branch. EscapePass drops anything it isn't sure
  of, and FDN-002 says "never a false positive", so flagging these changes
  that policy.
- **Error payloads.** `return E1::P(&n);` lets a local reference escape inside
  an error payload. Payloads are not among the escape routes F-MEM-5 lists.
- **Call-result fields.** `mut h: &N = mk(); h.next = &n;` and
  `l[0].next = &n` compile (the base's provenance is Unknown, never flagged).
- **`defer` false positive.** `p = heap(n); defer p = &n; return p;` is
  rejected, although the returned value is computed before the defer runs. A
  simple fix is unsound inside loops; defers need handling at block exit.
- **Casts to references.** `(0 to &N)`, `(s to &N)` and `(n to &M)` compile
  in safe code and fault. Reject everywhere, or allow only inside `unsafe { }`
  (as `T* -> &T` already is)?

## Still open from earlier items

- **FREEZE §6.14 F-STR-1 vs #256.** F-STR-1 says a negative string index read
  panics; #256 (turflang 2853007) made `s[-1]` the last byte, and
  `operators.md` documents that. Changing a frozen decision's text needs the
  owner.
- **Null strings in zero-filled string arrays, and the array-element `&U`
  hole.** `mut a: string[2];` then reading an unwritten element is a null
  string (segfault); `mut a: H[2];` leaves a `&U` field null. Owner said
  2026-10-12: a separate issue from LEC-009.
- **Unchecked run-time int-to-enum casts.** `(7 to En)` from a run-time int is
  a value named `<unknown enum>` and defeats `compare` exhaustiveness (docs:
  "allowed through").
- **Int `**` precision above 2^53.** It goes through `pow()` on doubles, so
  `3 ** 39` is wrong by 11.
