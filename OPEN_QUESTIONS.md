# Open questions for the owner, to check at the end

Language decisions the fuzzing work found but did not make: each needs the
owner, because the spec is silent or a frozen decision would change. Parked
here on the owner's instruction (2026-10-12) so the fuzzing work can move on;
go through them once the turf-fuzz roadmap is done. Delete a line once it is
decided, and record the decision where it belongs (FREEZE.md, gen/SEMANTICS.md).

## Still open from earlier items

- **Null strings in zero-filled string arrays, and the array-element `&U`
  hole.** `mut a: string[2];` then reading an unwritten element is a null
  string (segfault); `mut a: H[2];` leaves a `&U` field null. Owner said
  2026-10-12: a separate issue from LEC-009.
