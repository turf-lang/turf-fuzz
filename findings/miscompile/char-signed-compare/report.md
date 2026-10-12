# Wrong answer: `char` ordering compared signed bytes

- class: wrong answer, identical in all 9 configurations (so the oracle first presumes the evaluator); 5 hits in a 3-minute run (seeds 9046, 9100)
- found: generator (`char` from `int to char`, turf-fuzz item 2), compiler 94c78ae
- signature: `wrong-answer` with a control-flow divergence at `if !((v4 to char) >= (c1 to char))`, `v4 = 9999999999` (`gen/trace.py 9046`)
- repro: `min.tr`, `(200 to char) > 'a'` printed 0 while `((200 to char) to int) > ('a' to int)` printed 1
- cause: `< > <= >=` on two `char`s used signed icmp predicates; the docs make `char` an unsigned byte (`variables_and_types.md`: 0-255, `char -> int` zero-extends)
- fixed: turflang c851cac (`tests/17_char_type/07_char_ordering_is_unsigned.tr`)
