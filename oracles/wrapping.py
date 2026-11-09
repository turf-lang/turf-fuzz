#!/usr/bin/env python3
"""Wrapping oracle: math::wrapping_* against an exact reference interpreter.

The overflow oracle's programs (oracles/overflow.py: a few `int` variables
near the limits, defers, `&x` writes, loops with break/continue) with about a
third of their operations replaced by `math::wrapping_*` calls, half of their
statements printing or storing a wrapped value (they cannot trap, so a wrong
value shows before the program's first trap), and loop-carried hashes
(`h = wrapping_mul(h, K)` run up to 70 times, printed, then `h + 1`). Every
calling form appears: `math::wrapping_add(a, b)`, bare `wrapping_add(a, b)`,
UFCS `(a).wrapping_add(b)`, and a function value. Operands may all be
literals, since a wrapping call is never folded at compile time.

A wrapped value is unbounded in a way its operands do not show, which is
exactly where a range proof (turflang's check elision, NM-2) can be wrong:
the checked `+` after the loop must still trap at its line. The interpreter
computes each wrapping call modulo 2^64 and every checked operator on
unbounded integers; every -O level must agree with it exactly.

    python3 oracles/wrapping.py --seconds 600 --jobs 8 --seed 1
    python3 oracles/wrapping.py --replay SEED

Findings go to findings/<class>/wrapping-<kind>/, in the overflow oracle's
format (see its docstring for the classes).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import overflow  # noqa: E402

if __name__ == "__main__":
    overflow.main(oracle="wrapping", wrap=0.35)
