# Doc drift: variables_and_types.md still described "Mechanism B"

- class: doc disagrees with compiler (compiler is right: FREEZE.md 6.4, F-INIT-1)
- found: generator seed 12 emitted `mut a2: S0[3];` and read it; rejected E008, although the reference said arrays and structs are readable at once
- detail: SEM-004 removed the eager auto-default; the reference page kept it, and its `char` and struct examples failed (DOC_EXAMPLES_GAPS.txt had them as "not self-contained"). An array is unassigned until assigned whole or one element is written
- fixed: turflang 49e654b (page rewritten, two stale register lines removed); the generator follows a bare array declaration with an element write
