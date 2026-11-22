# ICE: an instantiation that fails to resolve because of an error already reported

- class: internal compiler error, `monomorphized instantiation of 'bad' failed to re-resolve. This is a compiler bug.` (E011) after a real E040
- found: nightly process tier (pinned), from `tests/54_size_parameters/fail_size_out_of_bounds_in_body.tr` (turf-fuzz #36, signature `ice/10bc87fbe126`)
- repro: `min.tr`; an unknown name in a generic function's body (`defer[N] = v;` with no `N`) and a call that instantiates it
- cause: the instantiation fails to resolve for the same reason as the E040, and the monomorphizer added E011 on top, calling it a compiler bug
- fixed: turflang 978a3f54 (branch `roadmap/v2.3.0`): with an error already recorded the pass stops on that error, without a second diagnostic; a failure with no error behind it is still E011. Test: `tests/38_monomorphization/fail_generic_body_unknown_name_no_ice.tr`
