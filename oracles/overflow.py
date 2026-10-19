#!/usr/bin/env python3
"""Overflow oracle: the runtime checks against an exact reference interpreter.

Every `int` `+ - *`, unary `-`, `op=`, `++`/`--`, `/`/`%` and array index is
checked at run time unless the compiler proves it cannot fail (check elision,
turflang's ValueRange.cpp). A wrong proof is silent: the sum wraps, the index
reads past the array, and every -O level agrees, so the differential oracle
cannot see it. This one can. Each program is a few `int` variables written
in every way the language allows (whole-statement writes, `x++` inside a call
argument, a `defer`, `&x` handed to a callee, loops with `break`/`continue`)
with values near the limits, and an interpreter with unbounded integers says
what it must print and where it must trap. Every configuration must agree
with it exactly: same stdout, same trap kind, same line.

    python3 oracles/overflow.py --seconds 600 --jobs 8 --seed 1
    python3 oracles/overflow.py --replay SEED          # print one program and its verdict

A mismatch is recorded once per kind, in findings/<class>/overflow-<kind>/
(prog.tr, expected.txt, out_<config>.txt, meta.json; a later one bumps the
count): unsound/overflow-missed-<trap> (a check that should have fired did
not), miscompile/overflow-spurious-<trap> and miscompile/overflow-wrong-value,
rejected-valid/overflow-<codes> (a program refused at compile time; an E001
is accepted only for an index line that never runs in range).
"""

import argparse
import datetime
import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import differential as D  # noqa: E402

MAX, MIN = 2**63 - 1, -2**63
LITS = [0, 1, 2, 3, 4, -4, -5, 5, -1, -2, 7, 1000, MAX, MAX - 1, MIN + 1, 3037000499,
        3037000500, -3037000500, 4611686018427387904, -4611686018427387904, 2**62 - 1, 99, -100]
NV = 4
ARR = [10, 20, 30, 40]
OPTS = ["-O0", "-O2", "-O3"]


class Trap(Exception):
    def __init__(self, kind, line):
        self.kind, self.line = kind, line


class Brk(Exception):
    pass


class Cont(Exception):
    pass


# ------------------------------------------------------------------ generating

class Gen:
    def __init__(self, rnd):
        self.r = rnd
        self.ctr = 0

    def lit(self):
        return ("lit", self.r.choice(LITS))

    def var(self):
        return ("var", self.r.randrange(NV))

    def expr(self, d, need_var=True):
        r = self.r
        if d <= 0 or r.random() < 0.3:
            if need_var or r.random() < 0.6:
                if r.random() < 0.08:
                    return ("idpost", r.randrange(NV), r.choice("+-"))
                return self.var()
            return self.lit()
        need_var = True  # an all-literal operation is folded at compile time (E076)
        k = r.random()
        if k < 0.12:
            return ("neg", self.expr(d - 1))
        if k < 0.2:
            return ("id", self.expr(d - 1))
        op = r.choice("+-*+-*+-*/%")
        if op in "/%":
            # never a zero divisor: (e % 7 + 8) or a nonzero literal
            dv = ("bin", "+", ("bin", "%", self.expr(d - 1), ("lit", 7)), ("lit", 8)) \
                if r.random() < 0.5 else ("lit", r.choice([1, -1, 2, 3, 7]))
            return ("bin", op, self.expr(d - 1), dv)
        left_var = r.random() < 0.5
        return ("bin", op, self.expr(d - 1, left_var), self.expr(d - 1, not left_var))

    def cond(self):
        return ("cmp", self.r.choice(["<", "<=", ">", ">=", "==", "!="]), self.expr(2),
                self.expr(1, False))

    def stmts(self, d, n, in_loop=False):
        return [self.stmt(d, in_loop) for _ in range(n)]

    def stmt(self, d, in_loop):
        r = self.r
        k = r.random()
        v = r.randrange(NV)
        if d > 0 and k < 0.1:
            return self.pattern(v)
        k = r.random()
        if k < 0.12:
            return ("assign", v, self.lit())  # a constant the dataflow may track
        if k < 0.2:
            return ("aidx", v)
        if k < 0.25:
            return ("assign", v, self.expr(3))
        if k < 0.37:
            return ("compound", v, r.choice("+-*"), self.expr(2))
        if k < 0.45:
            return ("post", v, r.choice("+-"))
        if k < 0.53:
            return ("print", self.expr(3))
        if k < 0.58:
            return ("setref", v, self.expr(2))
        if k < 0.62:
            return ("tmp", self.expr(3))
        if in_loop and k < 0.66:
            return ("brk" if r.random() < 0.5 else "cont", self.cond())
        if d > 0 and k < 0.78:
            body = self.stmts(d - 1, r.randint(1, 3), in_loop)
            if r.random() < 0.35:
                body.insert(r.randrange(len(body) + 1), ("defer", r.randrange(NV), self.expr(2)))
            els = self.stmts(d - 1, r.randint(0, 2), in_loop) if r.random() < 0.5 else None
            return ("if", self.cond(), body, els)
        if d > 0 and k < 0.88:
            self.ctr += 1
            return ("while", self.ctr, r.randint(1, 4), self.stmts(d - 1, r.randint(1, 3), True))
        return ("assign", v, self.expr(2))


    def pattern(self, v):
        """The shape a wrong elision proof needs: `v` gets a constant, a write
        the dataflow might not see changes it, then `v` is used where a check
        could be removed. As an `if true` block so it nests anywhere."""
        r = self.r
        big = ("lit", r.choice([MAX, MAX - 1, MIN + 1, 4611686018427387904, 1000000]))
        hidden = r.choice([
            ("setref", v, big),
            ("if", ("cmp", "==", ("var", v), ("var", v)), [("defer", v, big)], None),
            ("tmp", ("idpost", v, r.choice("+-"))),
        ])
        use = r.choice([
            ("print", ("bin", r.choice("+-*"), ("var", v), ("lit", r.choice([1, 2, -1, 3])))),
            ("aidx", v),
            ("compound", v, r.choice("+-*"), ("lit", r.choice([1, 2, 3]))),
            ("post", v, r.choice("+-")),
            ("print", ("neg", ("var", v))),
        ])
        first = ("assign", v, ("lit", r.choice([0, 1, 2, 3, -1, MAX, MIN + 1])))
        return ("if", ("cmp", "==", ("var", v), ("var", v)), [first, hidden, use], None)


def pe(e):
    t = e[0]
    if t == "lit":
        return str(e[1]) if e[1] >= 0 else "(%d)" % e[1]
    if t == "var":
        return "v%d" % e[1]
    if t == "idpost":
        return "id(v%d%s)" % (e[1], "++" if e[2] == "+" else "--")
    if t == "neg":
        return "-(%s)" % pe(e[1])
    if t == "id":
        return "id(%s)" % pe(e[1])
    if t == "bin":
        return "(%s %s %s)" % (pe(e[2]), e[1], pe(e[3]))
    if t == "cmp":
        return "%s %s %s" % (pe(e[2]), e[1], pe(e[3]))
    raise ValueError(t)


class Printer:
    def __init__(self):
        self.lines = []
        self.at = {}  # id(stmt) -> its line

    def emit(self, ind, text):
        self.lines.append("    " * ind + text)
        return len(self.lines)

    def block(self, ss, ind):
        for s in ss:
            self.stmt(s, ind)

    def stmt(self, s, ind):
        t = s[0]
        simple = {
            "assign": lambda: "v%d = %s;" % (s[1], pe(s[2])),
            "compound": lambda: "v%d %s= %s;" % (s[1], s[2], pe(s[3])),
            "post": lambda: "v%d%s;" % (s[1], "++" if s[2] == "+" else "--"),
            "print": lambda: "printline(%s);" % pe(s[1]),
            "setref": lambda: "setv(&v%d, %s);" % (s[1], pe(s[2])),
            "aidx": lambda: "printline(arr[v%d]);" % s[1],
            "tmp": lambda: "mut t%d: int = %s;" % (len(self.lines), pe(s[1])),
            "defer": lambda: "defer v%d = %s;" % (s[1], pe(s[2])),
        }
        if t in simple:
            self.at[id(s)] = self.emit(ind, simple[t]())
        elif t in ("brk", "cont"):
            self.at[id(s)] = self.emit(ind, "if %s {" % pe(s[1]))
            self.emit(ind + 1, "break;" if t == "brk" else "continue;")
            self.emit(ind, "}")
        elif t == "if":
            self.at[id(s)] = self.emit(ind, "if %s {" % pe(s[1]))
            self.block(s[2], ind + 1)
            if s[3] is not None:
                self.emit(ind, "} else {")
                self.block(s[3], ind + 1)
            self.emit(ind, "}")
        elif t == "while":
            self.emit(ind, "mut c%d: int = 0;" % s[1])
            self.at[id(s)] = self.emit(ind, "while c%d < %d {" % (s[1], s[2]))
            self.emit(ind + 1, "c%d += 1;" % s[1])
            self.block(s[3], ind + 1)
            self.emit(ind, "}")


# ------------------------------------------------------------------ evaluating

class Interp:
    """The language's semantics (operators.md) on unbounded ints: a result
    outside int's range traps where the operation is written."""

    def __init__(self, at, init):
        self.v = list(init)
        self.out = []
        self.at = at
        self.line = 0
        self.inrange = {}  # line -> {index was in range}, over every execution

    def chk(self, x):
        if x > MAX or x < MIN:
            raise Trap("overflow", self.line)
        return x

    def ev(self, e):
        t = e[0]
        if t == "lit":
            return e[1]
        if t == "var":
            return self.v[e[1]]
        if t == "idpost":
            old = self.v[e[1]]
            self.v[e[1]] = self.chk(old + (1 if e[2] == "+" else -1))
            return old
        if t == "neg":
            return self.chk(-self.ev(e[1]))
        if t == "id":
            return self.ev(e[1])
        if t == "bin":
            a, b, op = self.ev(e[2]), self.ev(e[3]), e[1]
            if op in "+-*":
                return self.chk(a + b if op == "+" else a - b if op == "-" else a * b)
            if b == 0:
                raise Trap("divzero", self.line)
            if op == "/":  # truncates toward zero; INT_MIN / -1 overflows
                q = abs(a) // abs(b)
                return self.chk(q if (a >= 0) == (b >= 0) else -q)
            r = abs(a) % abs(b)  # the sign of the dividend
            return r if a >= 0 else -r
        if t == "cmp":
            a, b = self.ev(e[2]), self.ev(e[3])
            return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
                    "==": a == b, "!=": a != b}[e[1]]
        raise ValueError(t)

    def block(self, ss):
        defers = []
        try:
            for s in ss:
                if s[0] == "defer":
                    defers.append(s)
                else:
                    self.stmt(s)
        finally:
            # a trap aborts the program, so no defer runs; break/continue run them
            if not isinstance(sys.exc_info()[1], Trap):
                for d in reversed(defers):
                    self.line = self.at[id(d)]
                    self.v[d[1]] = self.ev(d[2])

    def stmt(self, s):
        t = s[0]
        self.line = self.at.get(id(s), self.line)
        if t in ("assign", "setref"):
            self.v[s[1]] = self.ev(s[2])
        elif t == "compound":
            rhs = self.ev(s[3])  # the right side runs first
            a = self.v[s[1]]
            self.v[s[1]] = self.chk(a + rhs if s[2] == "+" else a - rhs if s[2] == "-" else a * rhs)
        elif t == "post":
            self.v[s[1]] = self.chk(self.v[s[1]] + (1 if s[2] == "+" else -1))
        elif t == "print":
            self.out.append(str(self.ev(s[1])))
        elif t == "aidx":
            i = self.v[s[1]]
            ok = -len(ARR) <= i < len(ARR)  # a negative index counts from the end
            self.inrange.setdefault(self.line, set()).add(ok)
            if not ok:
                raise Trap("bounds", self.line)
            self.out.append(str(ARR[i]))
        elif t == "tmp":
            self.ev(s[1])
        elif t in ("brk", "cont"):
            if self.ev(s[1]):
                raise Brk() if t == "brk" else Cont()
        elif t == "if":
            if self.ev(s[1]):
                self.block(s[2])
            elif s[3] is not None:
                self.block(s[3])
        elif t == "while":
            c = 0
            while True:
                self.line = self.at[id(s)]
                if c >= s[2]:
                    break
                c += 1
                try:
                    self.block(s[3])
                except Cont:
                    continue
                except Brk:
                    break


def build(seed):
    """(source, expected stdout lines, expected trap or None, index lines)"""
    rnd = random.Random(seed)
    g = Gen(rnd)
    init = [rnd.choice(LITS) for _ in range(NV)]
    body = g.stmts(3, rnd.randint(4, 10))
    pr = Printer()
    for line in ["// turf-fuzz overflow oracle: seed=%d" % seed,
                 "fn id(x: int): int {", "    return x;", "}",
                 "fn setv(p: &int, x: int): void {", "    p[0] = x;", "}",
                 "fn main(): int {"]:
        pr.emit(0, line)
    for i, v in enumerate(init):
        pr.emit(1, "mut v%d: int = %s;" % (i, pe(("lit", v))))
    pr.emit(1, "mut arr: int[%d] = [%s];" % (len(ARR), ", ".join(map(str, ARR))))
    pr.block(body, 1)
    for i in range(NV):
        pr.emit(1, "printline(v%d);" % i)
    pr.emit(1, "return 0;")
    pr.emit(0, "}")
    it = Interp(pr.at, init)
    trap = None
    try:
        it.block(body)
        it.out += [str(x) for x in it.v]
    except Trap as t:
        trap = (t.kind, t.line)
    return "\n".join(pr.lines) + "\n", it.out, trap, it.inrange


# ------------------------------------------------------------------ checking

PANIC = re.compile(r"panic: runtime check: (.*?) \(prog\.tr:(\d+)\)")


def trap_kind(msg):
    m = msg.lower()
    return "overflow" if "overflow" in m else "divzero" if "by zero" in m else \
        "bounds" if "bounds" in m else msg


def verdict(seed, workdir):
    """None, or (class, kind, details) for the first configuration that
    disagrees with the interpreter."""
    src, out, trap, inrange = build(seed)
    with open(os.path.join(workdir, "prog.tr"), "w") as f:
        f.write(src)
    runs = {}
    for opt in OPTS:
        binary = os.path.join(workdir, "b" + opt)
        rc, so, se, to = D._run([D.TURFC, "prog.tr", opt, "-o", binary], workdir,
                                timeout=D.COMPILE_TIMEOUT)
        text = D.ANSI_RE.sub("", so + se)
        if to or rc != 0:
            codes = sorted(set(D.ERROR_CODE_RE.findall(text)))
            e001 = [int(x) for x in re.findall(r"at line (\d+), col \d+ \[E001", text)]
            # E001 (a constant index out of range) is the language's rule; it
            # is wrong only on a line that runs with an in-range index.
            if codes == ["E001"] and not any(True in inrange.get(l, ()) for l in e001):
                return None
            kind = "compile-" + ("-".join(codes) if codes else "crash")
            return "rejected-valid", kind, (src, out, trap, {opt: "compile rc=%s\n%s" % (rc, text)})
        rc, so, se, to = D._run([binary], workdir, timeout=D.RUN_TIMEOUT)
        m = PANIC.search(se)
        got = (trap_kind(m.group(1)), int(m.group(2))) if m else \
            ("timeout", 0) if to else ("rc%s" % rc, 0) if rc != 0 else None
        runs[opt] = "rc: %s\n--- stdout ---\n%s--- stderr ---\n%s" % (rc, so, se)
        got_out = so.split()
        if got == trap and got_out == out:
            continue
        if trap and (got is None or got[0] != trap[0] or got[1] > trap[1]):
            return "unsound", "missed-" + trap[0], (src, out, trap, runs)
        if got and (trap is None or got[1] < trap[1] or got[0] != trap[0]):
            return "miscompile", "spurious-" + got[0], (src, out, trap, runs)
        return "miscompile", "wrong-value", (src, out, trap, runs)
    return None


class Recorder:
    def __init__(self, findings_dir):
        self.dir = findings_dir
        self.lock = threading.Lock()
        self.new = []

    def record(self, seed, cls, kind, details):
        src, out, trap, runs = details
        d = os.path.join(self.dir, cls, "overflow-" + kind)
        meta_path = os.path.join(d, "meta.json")
        with self.lock:
            if os.path.exists(meta_path):
                meta = json.load(open(meta_path))
                meta["count"] = meta.get("count", 1) + 1
                meta["last_seed"] = seed
                json.dump(meta, open(meta_path, "w"), indent=2)
                return False
            os.makedirs(d, exist_ok=True)
            self.new.append(d)
        with open(os.path.join(d, "prog.tr"), "w") as f:
            f.write(src)
        with open(os.path.join(d, "expected.txt"), "w") as f:
            f.write("trap: %s\n--- stdout ---\n%s\n" % (trap, "\n".join(out)))
        for name, text in runs.items():
            with open(os.path.join(d, "out_%s.txt" % name), "w", errors="replace") as f:
                f.write(text)
        shutil.copy(os.path.join(ROOT, "compiler.lock"), os.path.join(d, "compiler.lock"))
        json.dump({
            "class": cls, "signature": "overflow oracle: " + kind,
            "summary": "expected %s; %s" % ("trap %s at line %d" % trap if trap else "no trap",
                                            ", ".join(sorted(runs))),
            "seed": seed, "last_seed": seed, "count": 1,
            "compiler_lock_commit": D.lock_commit(), "compiler_tree_head": D.tree_head(),
            "found": datetime.datetime.now().isoformat(timespec="seconds"),
            "reproduce": "python3 oracles/overflow.py --replay %d" % seed,
        }, open(meta_path, "w"), indent=2)
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--replay", type=int, help="print one program, its expectation and verdict")
    ap.add_argument("--findings", default=os.path.join(ROOT, "findings"))
    a = ap.parse_args()

    if a.replay is not None:
        src, out, trap, _ = build(a.replay)
        print(src)
        print("// expected: trap %s, stdout %s" % (trap, out))
        with tempfile.TemporaryDirectory() as wd:
            v = verdict(a.replay, wd)
        print("verdict:", "ok" if v is None else "%s/overflow-%s" % (v[0], v[1]))
        return

    rec = Recorder(a.findings)
    deadline = time.time() + a.seconds
    lock = threading.Lock()
    state = {"next": a.seed, "done": 0, "bad": 0}

    def worker():
        while time.time() < deadline:
            with lock:
                seed = state["next"]
                state["next"] += 1
            with tempfile.TemporaryDirectory() as wd:
                v = verdict(seed, wd)
            with lock:
                state["done"] += 1
            if v:
                with lock:
                    state["bad"] += 1
                if rec.record(seed, *v):
                    print("NEW %s/overflow-%s seed %d" % (v[0], v[1], seed), flush=True)

    with ThreadPoolExecutor(min(a.jobs, 8)) as ex:
        for f in [ex.submit(worker) for _ in range(min(a.jobs, 8))]:
            f.result()
    print("overflow: %d programs in %ds, %d mismatch(es), %d new finding(s); seed %d"
          % (state["done"], a.seconds, state["bad"], len(rec.new), a.seed))


if __name__ == "__main__":
    main()
