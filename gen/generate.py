#!/usr/bin/env python3
"""Typed program generator for Turf, tier 1 of turf-fuzz.

Subset: int and bool locals, literals, + - * / %, comparisons, && || !,
unary -, give/otherwise, if/elseif/else, while with a bounded counter
(break/continue), helper functions int* -> int (calls only go to earlier
functions, so no recursion), early return, printing.

Every program terminates (loop bounds are literals, a static cost estimate
caps total work). The generator is deterministic per seed. The expected
result comes from evaluate.py, which runs this module's AST.

    python3 gen/generate.py SEED [-o prog.tr] [--expected expected.txt]
"""

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tast  # noqa: E402
from tast import (INT_MIN, INT_MAX, Lit, BLit, Var, Un, Bin, Call, Give, Decl,  # noqa: E402
                  Assign, Comp, IncDec, Print, If, While, Break, Continue,
                  Return, Func, Program)
import evaluate as ev  # noqa: E402

BOUNDARY = [INT_MAX, INT_MIN, INT_MAX - 1, INT_MIN + 1, 2147483647, -2147483648,
            4294967296, 3037000500, 3037000499, -1, 0, 1, 2, -2]


class Scope:
    """Lexical scopes of the function being generated."""

    def __init__(self):
        self.frames = [[]]

    def push(self):
        self.frames.append([])

    def pop(self):
        self.frames.pop()

    def declare(self, name, ty, assignable):
        self.frames[-1].append((name, ty, assignable))

    def vars(self, ty=None, assignable=False):
        out = []
        for fr in self.frames:
            for name, t, a in fr:
                if name == "chk":
                    continue  # only the checksum statements touch chk
                if (ty is None or t == ty) and (a or not assignable):
                    out.append((name, t))
        return out


def const_value(e):
    """Value of a literal-only int/bool subtree, else None.

    Raises Overflow if folding it would leave int64: the compiler folds
    literal arithmetic at compile time and rejects an unrepresentable result
    (E076, FREEZE 6.2 row 6), so such a program is not a runtime-trap test."""
    if isinstance(e, (Lit, BLit)):
        return e.v
    if isinstance(e, Un):
        v = const_value(e.e)
        if v is None:
            return None
        if e.op == "!":
            return not v
        if v == INT_MIN:
            raise Overflow()
        return -v
    if isinstance(e, Bin):
        a = const_value(e.l)
        b = const_value(e.r)
        if a is None or b is None:
            return None
        op = e.op
        if op in ("+", "-", "*"):
            r = a + b if op == "+" else a - b if op == "-" else a * b
            if not INT_MIN <= r <= INT_MAX:
                raise Overflow()
            return r
        if op in ("/", "%"):
            if b == 0 or (a == INT_MIN and b == -1):
                return None  # runtime check, not folded
            q = abs(a) // abs(b)
            q = q if (a < 0) == (b < 0) else -q
            return q if op == "/" else a - q * b
        if op == "&&":
            return bool(a) and bool(b)
        if op == "||":
            return bool(a) or bool(b)
        return {"==": a == b, "!=": a != b, "<": a < b, ">": a > b,
                "<=": a <= b, ">=": a >= b}[op]
    return None  # Var, Call, Give


QUIRK_FILTER = True


class Overflow(Exception):
    pass


def _is_intmin(e):
    return isinstance(e, Lit) and e.v == INT_MIN


def check_foldable(e):
    """Raise Overflow if any literal-only subtree of e would fold out of range."""
    if isinstance(e, (Lit, BLit, Var)):
        return
    if isinstance(e, Un):
        check_foldable(e.e)
        const_value(e)
    elif isinstance(e, Bin):
        if QUIRK_FILTER and e.op in ("+", "-", "*") and (
                _is_intmin(e.l) or (_is_intmin(e.r) and const_value_safe(e.l) is not None)):
            # `-9223372036854775808 + x` and `1 + -9223372036854775808 + x` are
            # rejected (E076) though `x + -9223372036854775808` is accepted; see
            # SEMANTICS.md, open questions. Skipped by default (--probe-quirks).
            raise Overflow()
        check_foldable(e.l)
        check_foldable(e.r)
        const_value(e)
    elif isinstance(e, Call):
        for a in e.args:
            check_foldable(a)
    elif isinstance(e, Give):
        check_foldable(e.c)
        check_foldable(e.a)
        check_foldable(e.b)


class Gen:
    def __init__(self, seed):
        self.seed = seed
        self.rng = random.Random(seed)
        r = self.rng
        self.tame = r.random() < 0.7        # keep values small: few traps
        self.safe_div = 0.985 if self.tame else 0.8
        self.boundary_p = 0.03 if self.tame else 0.12
        self.paren_mode = r.choice(["min", "min", "rand", "full"])
        self.funcs = []
        self.scope = None
        self.counter_n = 0
        self.var_n = 0
        self.mult = 1          # product of enclosing loop bounds
        self.cost = 0          # estimated steps spent in the current function
        self.cost_cap = 4000
        self.in_loop = 0
        self.in_func = False   # helper (non-main)

    # --------------------------------------------------------------- helpers
    def chance(self, p):
        return self.rng.random() < p

    def pick(self, seq):
        return self.rng.choice(seq)

    def new_var(self):
        self.var_n += 1
        return "v%d" % self.var_n

    def new_counter(self):
        self.counter_n += 1
        return "c%d" % self.counter_n

    # ----------------------------------------------------------- expressions
    def literal(self):
        r = self.rng
        if r.random() < self.boundary_p:
            return Lit(self.pick(BOUNDARY))
        k = r.random()
        if k < 0.6:
            return Lit(r.randint(-9, 9))
        if k < 0.85:
            return Lit(r.randint(-100, 100))
        if k < 0.95:
            return Lit(r.randint(-100000, 100000))
        return Lit(r.randint(INT_MIN, INT_MAX) if not self.tame else r.randint(-10**9, 10**9))

    def int_leaf(self):
        vs = self.scope.vars("int")
        if vs and self.chance(0.75):
            n, _ = self.pick(vs)
            return Var(n, "int")
        return self.literal()

    def bool_leaf(self):
        vs = self.scope.vars("bool")
        if vs and self.chance(0.6):
            n, _ = self.pick(vs)
            return Var(n, "bool")
        return BLit(self.chance(0.5))

    def shrink(self, e):
        """In tame mode keep a multiplication operand small."""
        if not self.tame:
            return e
        if isinstance(e, Lit) and -1000 <= e.v <= 1000:
            return e
        return Bin("%", e, Lit(self.pick([7, 10, 100, 1000])))

    def divisor(self, depth):
        if self.chance(self.safe_div):
            k = self.rng.random()
            if k < 0.35:
                v = self.pick([1, 2, 3, 5, 7, 10, -1, -2, -3, -7, 1000])
                return Lit(v)
            # (e % 7) + 8 is in [2, 14]: never zero, never -1
            return Bin("+", Bin("%", self.gen_int(depth - 1), Lit(7)), Lit(8))
        return self.gen_int(depth - 1)

    def gen_int(self, depth):
        r = self.rng
        if depth <= 0:
            return self.int_leaf()
        k = r.random()
        if k < 0.30:
            return self.int_leaf()
        if k < 0.80:
            op = self.pick(["+", "+", "-", "-", "*", "/", "%"])
            if op in ("/", "%"):
                return Bin(op, self.gen_int(depth - 1), self.divisor(depth))
            if op == "*":
                return Bin(op, self.shrink(self.gen_int(depth - 1)), self.shrink(self.gen_int(depth - 1)))
            return Bin(op, self.gen_int(depth - 1), self.gen_int(depth - 1))
        if k < 0.86:
            return Un("-", self.gen_int(depth - 1))
        if k < 0.93:
            c = self.try_call(depth)
            if c is not None:
                return c
            return self.int_leaf()
        return Give(self.gen_bool(depth - 1), self.gen_int(depth - 1), self.gen_int(depth - 1))

    def gen_bool(self, depth):
        r = self.rng
        if depth <= 0:
            return self.bool_leaf() if self.chance(0.4) else self.compare(0)
        k = r.random()
        if k < 0.15:
            return self.bool_leaf()
        if k < 0.50:
            return self.compare(depth)
        if k < 0.75:
            return Bin(self.pick(["&&", "||"]), self.gen_bool(depth - 1), self.gen_bool(depth - 1))
        if k < 0.88:
            return Un("!", self.gen_bool(depth - 1))
        if k < 0.94:
            return Bin(self.pick(["==", "!="]), self.gen_bool(depth - 1), self.gen_bool(depth - 1))
        return Give(self.gen_bool(depth - 1), self.gen_bool(depth - 1), self.gen_bool(depth - 1))

    def compare(self, depth):
        op = self.pick(["<", ">", "<=", ">=", "==", "!="])
        return Bin(op, self.gen_int(max(depth - 1, 0)), self.gen_int(max(depth - 1, 0)))

    def try_call(self, depth):
        if not self.funcs:
            return None
        callee = self.pick(self.funcs)
        spent = self.mult * (callee.cost + 1)
        if self.cost + spent > self.cost_cap:
            return None
        self.cost += spent
        return Call(callee.name, [self.gen_int(max(depth - 2, 0)) for _ in callee.params])

    def expr_ok(self, make):
        """Build an expression with `make`; retry until no literal-only
        subtree folds out of range."""
        for _ in range(40):
            e = make()
            try:
                check_foldable(e)
                return e
            except Overflow:
                continue
        return Lit(self.rng.randint(-9, 9)) if self.last_ty == "int" else BLit(True)

    def int_expr(self, depth=3):
        self.last_ty = "int"
        return self.expr_ok(lambda: self.gen_int(depth))

    def bool_expr(self, depth=3):
        self.last_ty = "bool"
        return self.expr_ok(lambda: self.gen_bool(depth))

    # ------------------------------------------------------------ statements
    def spend(self, n=1):
        self.cost += self.mult * n

    def gen_block(self, n, depth):
        self.scope.push()
        out = []
        for _ in range(n):
            out.extend(self.gen_stmt(depth))
        self.scope.pop()
        return out

    def mix(self):
        """chk = (chk * 31 + e % 1000003) % 1000000007 : bounded, never traps by itself."""
        e = self.int_expr(3)
        inner = Bin("+", Bin("*", Var("chk", "int"), Lit(31)), Bin("%", e, Lit(1000003)))
        return Assign("chk", Bin("%", inner, Lit(1000000007)))

    def gen_stmt(self, depth):
        r = self.rng
        self.spend()
        if self.cost > self.cost_cap * 2:
            return [self.mix()]
        k = r.random()
        if k < 0.17:
            return [self.gen_decl()]
        if k < 0.34:
            s = self.gen_assign()
            if s:
                return [s]
        elif k < 0.42:
            s = self.gen_comp()
            if s:
                return [s]
        elif k < 0.46:
            vs = self.scope.vars("int", True)
            if vs:
                return [IncDec(self.pick(vs)[0], self.pick(["++", "--"]))]
        elif k < 0.56:
            return [self.print_stmt()]
        elif k < 0.80:
            if depth < 3:
                return [self.gen_if(depth)]
        elif k < 0.89:
            if depth < 2:
                return [self.gen_while(depth)]
        elif k < 0.93:
            if self.in_loop:
                return [If([[self.bool_expr(2), [self.pick([Break, Continue])()], 0]], None)]
        elif k < 0.96:
            if self.in_func:
                return [If([[self.bool_expr(2), [Return(self.int_expr(2))], 0]], None)]
        return [self.mix()]

    def print_stmt(self):
        if self.chance(0.75):
            return Print(self.int_expr(3))
        return Print(self.bool_expr(3))

    def gen_decl(self):
        ty = "int" if self.chance(0.75) else "bool"
        e = self.int_expr(3) if ty == "int" else self.bool_expr(3)
        mutable = True
        if self.chance(0.2) and const_value_safe(e) is None:
            mutable = False  # immut with a non-constant initializer
        name = self.new_var()
        self.scope.declare(name, ty, mutable)
        return Decl(name, ty, e, mutable)

    def gen_assign(self):
        vs = self.scope.vars(None, True)
        if not vs:
            return None
        name, ty = self.pick(vs)
        e = self.int_expr(3) if ty == "int" else self.bool_expr(3)
        return Assign(name, e)

    def gen_comp(self):
        vs = self.scope.vars("int", True)
        if not vs:
            return None
        name = self.pick(vs)[0]
        op = self.pick(["+", "-", "*", "/", "%"])
        if op in ("/", "%"):
            e = self.divisor(2)
            self.last_ty = "int"
            e = self.expr_ok(lambda: e)
        elif op == "*":
            e = self.expr_ok(lambda: self.shrink(self.gen_int(2)))
            if self.tame:
                e = Bin("%", e, Lit(10))
        else:
            e = self.int_expr(2)
        return Comp(name, op, e)

    def gen_if(self, depth):
        branches = []
        nb = 1 + (self.pick([0, 0, 1, 2]))
        for _ in range(nb):
            cond = self.bool_expr(3)
            branches.append([cond, self.gen_block(self.rng.randint(0, 3), depth + 1), 0])
        els = None
        if self.chance(0.5):
            els = self.gen_block(self.rng.randint(0, 3), depth + 1)
        return If(branches, els)

    def gen_while(self, depth):
        bound = self.rng.randint(0, 8)
        saved = self.mult
        self.mult *= max(bound, 1)
        c = self.new_counter()
        self.scope.declare(c, "int", False)
        self.in_loop += 1
        body = self.gen_block(self.rng.randint(1, 4), depth + 1)
        self.in_loop -= 1
        self.mult = saved
        return While(c, bound, self.rng.randint(0, 2), body)

    # ------------------------------------------------------------- functions
    def gen_func(self, idx):
        self.scope = Scope()
        self.var_n = 0
        self.counter_n = 0
        self.cost = 0
        self.mult = 1
        self.in_func = True
        self.cost_cap = 1500
        nparams = self.rng.randint(0, 3)
        params = ["p%d" % i for i in range(nparams)]
        for p in params:
            self.scope.declare(p, "int", False)
        self.scope.declare("chk", "int", True)
        body = [Decl("chk", "int", Lit(0), True)]
        body.extend(self.initial_vars())
        body.extend(self.gen_block(self.rng.randint(1, 6), 0))
        body.append(Return(self.int_expr(3)))
        f = Func("f%d" % idx, params, body)
        f.cost = self.cost + 2
        return f

    def initial_vars(self):
        out = []
        for _ in range(self.rng.randint(2, 4)):
            ty = "int" if self.chance(0.75) else "bool"
            e = self.literal() if ty == "int" else BLit(self.chance(0.5))
            name = self.new_var()
            self.scope.declare(name, ty, True)
            out.append(Decl(name, ty, e, True))
        return out

    def gen_main(self):
        self.scope = Scope()
        self.var_n = 0
        self.counter_n = 0
        self.cost = 0
        self.mult = 1
        self.in_func = False
        self.cost_cap = 4000
        self.scope.declare("chk", "int", True)
        body = [Decl("chk", "int", Lit(0), True)]
        body.extend(self.initial_vars())
        body.extend(self.gen_block(self.rng.randint(4, 14), 0))
        body.append(Print(Var("chk", "int")))
        if self.chance(0.7):
            body.append(Return(Lit(0)))
        else:
            body.append(Return(self.int_expr(2)))
        return Func("main", [], body)

    def program(self):
        for i in range(self.rng.randint(0, 4)):
            self.funcs.append(self.gen_func(i))
        main = self.gen_main()
        return Program(list(self.funcs), main, self.seed, self.paren_mode)


def const_value_safe(e):
    try:
        return const_value(e)
    except Overflow:
        return 0


def generate(seed, allow_undefined=False, max_attempts=20):
    """Return (program, result, attempt). The attempt index only grows when a
    program's result is undefined by the language (those are skipped unless
    allow_undefined) or runs past the evaluator's step budget."""
    for attempt in range(max_attempts):
        g = Gen(seed if attempt == 0 else seed * 7919 + attempt * 104729)
        prog = g.program()
        tast.render(prog)
        try:
            res = ev.evaluate(prog)
        except ev.Undefined:
            if allow_undefined:
                res = None
                return prog, res, attempt
            continue
        except ev.Timeout:
            continue
        return prog, res, attempt
    raise RuntimeError("no usable program for seed %d" % seed)


def format_expected(res):
    if res is None:
        return "outcome: undefined\n--- stdout ---\n"
    if res.kind == "exit":
        head = "outcome: exit %d" % res.code
    else:
        head = "outcome: trap %s line %d" % (res.trap, res.line)
    return head + "\n--- stdout ---\n" + res.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=int)
    ap.add_argument("-o", "--output", help="write program here (default stdout)")
    ap.add_argument("--expected", help="write the expected outcome + stdout here")
    ap.add_argument("--probe-quirks", action="store_true",
                    help="stop avoiding shapes known to be rejected (INT_MIN literal in + - * with a constant or on the left)")
    ap.add_argument("--allow-undefined", action="store_true",
                    help="keep programs whose result the language leaves undefined")
    a = ap.parse_args()
    global QUIRK_FILTER
    QUIRK_FILTER = not a.probe_quirks
    prog, res, attempt = generate(a.seed, a.allow_undefined)
    if a.output:
        with open(a.output, "w") as f:
            f.write(prog.source)
    else:
        sys.stdout.write(prog.source)
    if a.expected:
        with open(a.expected, "w") as f:
            f.write(format_expected(res))
    if attempt:
        sys.stderr.write("note: attempt %d\n" % attempt)


if __name__ == "__main__":
    main()
