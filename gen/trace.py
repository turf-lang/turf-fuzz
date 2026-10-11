#!/usr/bin/env python3
"""Find where a wrong-answer program first diverges from the evaluator.

Regenerates SEED, inserts after every statement a print of each scalar
variable in scope (marked with a sentinel so it can be told apart from the
program's own output), renders, compiles at -O0 and runs it, and evaluates
the same instrumented AST. Prints the first statement after which the two
traces differ, with the values on each side.

    python3 gen/trace.py SEED [--turfc PATH] [--keep DIR]

The instrumented program has its own line numbers, so a trap's line is only
comparable between the two sides of this run, not with the original.
"""

import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tast  # noqa: E402
from tast import (Var, Lit, Decl, Print, If, While, ForRange, ForEach, Return,  # noqa: E402
                  Break, Continue, is_scalar)
import evaluate as ev  # noqa: E402
import generate  # noqa: E402

SENTINEL = 777000777000777


class Probe(Print):
    """A print of one variable, tagged with the statement it follows."""

    def __init__(self, tag, name, ty):
        super().__init__(Var(name, ty))
        self.tag, self.name = tag, name


def instrument(stmts, visible, tags):
    out = []
    scope = list(visible)
    for s in stmts:
        if isinstance(s, If):
            for br in s.branches:
                br[1] = instrument(br[1], scope, tags)
            if s.els is not None:
                s.els = instrument(s.els, scope, tags)
        elif isinstance(s, While):
            s.body = instrument(s.body, scope + [(s.counter, "int")], tags)
        elif isinstance(s, ForRange):
            s.body = instrument(s.body, scope + [(s.var, "int")], tags)
        elif isinstance(s, ForEach):
            inner = [(s.var, s.base.ty.elem)] + ([(s.ivar, "int")] if s.ivar else [])
            s.body = instrument(s.body, scope + inner, tags)
        out.append(s)
        if isinstance(s, Decl):
            scope.append((s.name, s.ty))
        if isinstance(s, While):
            scope.append((s.counter, "int"))
        if isinstance(s, (Return, Break, Continue)):
            continue
        tag = len(tags)
        tags.append(s)
        out.append(Print(Lit(SENTINEL)))
        out.append(Print(Lit(tag)))
        for n, t in scope:
            if is_scalar(t):
                out.append(Probe(tag, n, t))
    return out


def split_trace(text):
    """[(tag, [values])] from the program's stdout."""
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if lines[i] == str(SENTINEL) and i + 1 < len(lines):
            tag = int(lines[i + 1])
            vals = []
            i += 2
            while i < len(lines) and lines[i] != str(SENTINEL) and lines[i] != "":
                vals.append(lines[i])
                i += 1
            out.append((tag, vals))
        else:
            i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=int)
    ap.add_argument("--turfc", default=os.environ.get("TURFC") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "turflang", "build", "turfc"))
    ap.add_argument("--keep")
    a = ap.parse_args()

    prog, _, _ = generate.generate(a.seed)
    tags = []
    params = lambda f: [(n, t) for n, t in f.params]  # noqa: E731
    for f in prog.funcs:
        f.body = instrument(f.body, params(f), tags)
    for st in prog.structs:
        for m in st.methods:
            m.body = instrument(m.body, params(m), tags)
    prog.main.body = instrument(prog.main.body, [], tags)
    tast.render(prog)
    res = ev.evaluate(prog)

    wd = os.path.abspath(a.keep or tempfile.mkdtemp(prefix="trace_"))
    os.makedirs(wd, exist_ok=True)
    src = os.path.join(wd, "prog.tr")
    with open(src, "w") as f:
        f.write(prog.source)
    c = subprocess.run([a.turfc, "prog.tr", "-O0", "-o", "bin"], cwd=wd,
                       capture_output=True, text=True)
    if c.returncode != 0:
        sys.exit("instrumented program did not compile:\n" + c.stdout[-2000:] + c.stderr[-2000:])
    r = subprocess.run([os.path.join(wd, "bin")], cwd=wd, capture_output=True, text=True, timeout=30)

    want, got = split_trace(res.stdout), split_trace(r.stdout)
    for i, (w, g) in enumerate(zip(want, got)):
        if w != g:
            s = tags[w[0]]
            print("first divergence at trace point %d, after line %d of the instrumented program:"
                  % (i, s.line))
            print("    " + prog.source.split("\n")[s.line - 1].strip())
            names = [p.name for p in _probes_for(prog, w[0])]
            if w[0] != g[0]:
                print("  control flow differs: evaluator at tag %d, compiled at tag %d" % (w[0], g[0]))
            for n, wv, gv in zip(names, w[1], g[1]):
                mark = "   <--" if wv != gv else ""
                print("  %-6s evaluator %-22s compiled %s%s" % (n, wv, gv, mark))
            print("dir: " + wd)
            return
    if len(want) != len(got):
        print("traces agree for %d points, then lengths differ (evaluator %d, compiled %d)"
              % (min(len(want), len(got)), len(want), len(got)))
    else:
        print("traces agree (%d points); evaluator %s, compiled rc %d" % (len(want), res.describe(), r.returncode))
    print("dir: " + wd)


def _probes_for(prog, tag):
    bodies = [f.body for f in prog.funcs + [prog.main]]
    bodies += [m.body for st in prog.structs for m in st.methods]
    for b in bodies:
        for s in tast.walk_stmts(b):
            if isinstance(s, Probe) and s.tag == tag:
                yield s


if __name__ == "__main__":
    main()
