"""minimize.py -- line-based delta debugging that keeps the SAME signature.

Factored out of turflang's scripts/fuzz/crash_fuzz.py.  Two differences:

  * the predicate is "still has this exact signature", not "still crashes":
    a reduction that trades the original stack overflow for an unrelated
    diagnostic-path assertion is a different bug and is rejected;
  * the compiler is not baked in.  The caller supplies `probe(source) ->
    Signature | None` (typically: write the file, run turfc, signature.compute),
    so every tier can reuse this with its own runner.

Bounded twice, because a hang costs a full timeout per probe: a probe cap and a
wall-clock cap.  A finding must be reported while the run is still alive, not
after a quarter of an hour of reducing it.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from . import signature as sigmod

Probe = Callable[[str], Optional["sigmod.Signature"]]


def same_signature(probe: Probe, target: "sigmod.Signature") -> Callable[[str], bool]:
    """Predicate for minimize(): the candidate still produces `target`."""
    def same(candidate: str) -> bool:
        got = probe(candidate)
        return got is not None and got.key == target.key
    return same


def minimize(src: str, same: Callable[[str], bool], max_probes: int = 400,
             max_seconds: float = 180.0) -> tuple[str, int]:
    """Delta-debug `src` by lines, keeping only reductions for which
    `same(candidate)` is true.  Returns (smallest source found, probes used).
    Never returns something `same` has not accepted, except `src` itself."""
    probes = 0
    give_up = time.time() + max_seconds

    def check(candidate: str) -> bool:
        nonlocal probes
        if probes >= max_probes or time.time() > give_up:
            return False
        probes += 1
        return same(candidate)

    lines = src.splitlines(keepends=True)
    n = 2
    while len(lines) >= 2 and n <= len(lines):
        size = max(1, len(lines) // n)
        reduced = False
        for start in range(0, len(lines), size):
            cand = lines[:start] + lines[start + size:]
            if cand and check("".join(cand)):
                lines, n, reduced = cand, max(n - 1, 2), True
                break
        if not reduced:
            if size == 1:
                break
            n = min(len(lines), n * 2)
    return "".join(lines), probes
