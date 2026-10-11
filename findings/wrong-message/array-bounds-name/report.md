# Wrong panic text: a `T[N]` bounds panic said `'array'`

- class: wrong message
- found: while probing array panics for the generator, compiler 479a3bc
- repro: `min.tr` panicked `Array index out of bounds for 'array'`; arrays.md documents `'<name>'`, and the `T[]` path already used the variable's name
- fixed: turflang a89bd67 (two existing runtime tests now pin the name)
