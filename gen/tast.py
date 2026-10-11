"""The generator's own AST and its renderer to Turf source text.

The evaluator (evaluate.py) runs this AST directly; it never parses text.
Types are the strings "int" and "bool".

Rendering assigns `.line` (1-based) to every statement and to every `elseif`
branch, so the evaluator can predict the `(file:line)` a trap reports.
"""

import random

INT_MIN = -(1 << 63)
INT_MAX = (1 << 63) - 1

# Operator precedence, docs/reference/operators.md (higher binds tighter).
PREC = {
    "||": 15, "&&": 20,
    "==": 25, "!=": 25,
    "<": 30, ">": 30, "<=": 30, ">=": 30,
    "+": 40, "-": 40,
    "*": 50, "/": 50, "%": 50,
}
PREC_UNARY = 70
PREC_ATOM = 100

ARITH_OPS = ("+", "-", "*", "/", "%")
CMP_OPS = ("<", ">", "<=", ">=")
EQ_OPS = ("==", "!=")


# ---------------------------------------------------------------- expressions

class Lit:
    ty = "int"

    def __init__(self, v):
        self.v = v


class BLit:
    ty = "bool"

    def __init__(self, v):
        self.v = v


class Var:
    def __init__(self, name, ty):
        self.name = name
        self.ty = ty


class Un:
    """op is '-' (int) or '!' (bool)."""

    def __init__(self, op, e):
        self.op = op
        self.e = e
        self.ty = "int" if op == "-" else "bool"


class Bin:
    def __init__(self, op, l, r):
        self.op = op
        self.l = l
        self.r = r
        self.ty = "int" if op in ARITH_OPS else "bool"


class Call:
    ty = "int"

    def __init__(self, fn, args):
        self.fn = fn  # function name
        self.args = args


class Give:
    """(c give a otherwise b); a and b share a type."""

    def __init__(self, c, a, b):
        self.c = c
        self.a = a
        self.b = b
        self.ty = a.ty


# ----------------------------------------------------------------- statements

class Decl:
    def __init__(self, name, ty, e, mutable=True):
        self.name, self.ty, self.e, self.mutable = name, ty, e, mutable
        self.line = 0


class Assign:
    def __init__(self, name, e):
        self.name, self.e = name, e
        self.line = 0


class Comp:
    """name op= e   (op in + - * / %)."""

    def __init__(self, name, op, e):
        self.name, self.op, self.e = name, op, e
        self.line = 0


class IncDec:
    def __init__(self, name, op):
        self.name, self.op = name, op  # op is '++' or '--'
        self.line = 0


class Print:
    def __init__(self, e):
        self.e = e
        self.line = 0


class If:
    """branches: list of [cond, body, line]; els: body or None."""

    def __init__(self, branches, els):
        self.branches = branches
        self.els = els
        self.line = 0


class While:
    """mut <counter>: int = 0; while <counter> < bound { <incr>; body }"""

    def __init__(self, counter, bound, incr_style, body):
        self.counter, self.bound = counter, bound
        self.incr_style = incr_style  # 0: c = c + 1; 1: c += 1; 2: c++
        self.body = body
        self.line = 0
        self.decl_line = 0
        self.incr_line = 0


class Break:
    def __init__(self):
        self.line = 0


class Continue:
    def __init__(self):
        self.line = 0


class Return:
    def __init__(self, e):
        self.e = e
        self.line = 0


class Func:
    def __init__(self, name, params, body):
        self.name = name
        self.params = params  # list of names (all int)
        self.body = body      # list of statements, last is Return
        self.cost = 0


class Program:
    def __init__(self, funcs, main, seed=0, paren_mode="min"):
        self.funcs = funcs
        self.main = main
        self.seed = seed
        self.paren_mode = paren_mode  # full | min | rand
        self.source = ""


# ------------------------------------------------------------------ rendering

def _prec(e):
    if isinstance(e, Bin):
        return PREC[e.op]
    if isinstance(e, Un):
        return PREC_UNARY
    if isinstance(e, Lit) and e.v < 0:
        return PREC_UNARY
    return PREC_ATOM


class _Renderer:
    def __init__(self, program):
        self.p = program
        self.rng = random.Random(program.seed ^ 0x5EED)
        self.lines = []

    # -- expressions
    def expr(self, e, parent_prec=0, right=False):
        s = self._expr(e)
        p = _prec(e)
        need = p < parent_prec or (p == parent_prec and right and parent_prec < PREC_UNARY)
        if parent_prec > 0 and not need:
            mode = self.p.paren_mode
            if mode == "full" and isinstance(e, (Bin, Give)):
                need = True
            elif mode == "rand" and isinstance(e, Bin) and self.rng.random() < 0.3:
                need = True
        if parent_prec == PREC_UNARY and isinstance(e, (Bin,)):
            need = True
        if need:
            return "(" + s + ")"
        return s

    def _expr(self, e):
        if isinstance(e, Lit):
            return str(e.v)
        if isinstance(e, BLit):
            return "true" if e.v else "false"
        if isinstance(e, Var):
            return e.name
        if isinstance(e, Un):
            inner = self.expr(e.e, PREC_UNARY)
            if inner.startswith(e.op) and e.op == "-":
                inner = " " + inner  # never emit '--'
            return e.op + inner
        if isinstance(e, Bin):
            pr = PREC[e.op]
            return "%s %s %s" % (self.expr(e.l, pr, False), e.op, self.expr(e.r, pr, True))
        if isinstance(e, Call):
            return "%s(%s)" % (e.fn, ", ".join(self.expr(a) for a in e.args))
        if isinstance(e, Give):
            return "(%s give %s otherwise %s)" % (self.expr(e.c), self.expr(e.a), self.expr(e.b))
        raise TypeError(e)

    # -- statements
    def emit(self, indent, text):
        self.lines.append("    " * indent + text)
        return len(self.lines)

    def block(self, stmts, indent):
        for s in stmts:
            self.stmt(s, indent)

    def stmt(self, s, ind):
        if isinstance(s, Decl):
            kw = "mut" if s.mutable else "immut"
            s.line = self.emit(ind, "%s %s: %s = %s;" % (kw, s.name, s.ty, self.expr(s.e)))
        elif isinstance(s, Assign):
            s.line = self.emit(ind, "%s = %s;" % (s.name, self.expr(s.e)))
        elif isinstance(s, Comp):
            s.line = self.emit(ind, "%s %s= %s;" % (s.name, s.op, self.expr(s.e)))
        elif isinstance(s, IncDec):
            s.line = self.emit(ind, "%s%s;" % (s.name, s.op))
        elif isinstance(s, Print):
            s.line = self.emit(ind, "io::printline(%s);" % self.expr(s.e))
            self.emit(ind, "io::flush();")
        elif isinstance(s, Break):
            s.line = self.emit(ind, "break;")
        elif isinstance(s, Continue):
            s.line = self.emit(ind, "continue;")
        elif isinstance(s, Return):
            s.line = self.emit(ind, "return %s;" % self.expr(s.e))
        elif isinstance(s, If):
            for i, br in enumerate(s.branches):
                cond, body, _ = br
                head = ("if %s {" if i == 0 else "} elseif %s {") % self.expr(cond)
                br[2] = self.emit(ind, head)
                if i == 0:
                    s.line = br[2]
                self.block(body, ind + 1)
            if s.els is not None:
                self.emit(ind, "} else {")
                self.block(s.els, ind + 1)
            self.emit(ind, "}")
        elif isinstance(s, While):
            s.decl_line = self.emit(ind, "mut %s: int = 0;" % s.counter)
            s.line = self.emit(ind, "while %s < %d {" % (s.counter, s.bound))
            c = s.counter
            incr = ("%s = %s + 1;" % (c, c), "%s += 1;" % c, "%s++;" % c)[s.incr_style]
            s.incr_line = self.emit(ind + 1, incr)
            self.block(s.body, ind + 1)
            self.emit(ind, "}")
        else:
            raise TypeError(s)

    def func(self, f, is_main=False):
        if is_main:
            self.emit(0, "fn main(): int {")
        else:
            params = ", ".join("%s: int" % p for p in f.params)
            self.emit(0, "fn %s(%s): int {" % (f.name, params))
        self.block(f.body, 1)
        self.emit(0, "}")
        self.emit(0, "")

    def program(self):
        self.emit(0, "// turf-fuzz gen: seed=%d paren=%s" % (self.p.seed, self.p.paren_mode))
        self.emit(0, "import io;")
        self.emit(0, "")
        for f in self.p.funcs:
            self.func(f)
        self.func(self.p.main, True)
        return "\n".join(self.lines).rstrip("\n") + "\n"


def render(program):
    program.source = _Renderer(program).program()
    return program.source
