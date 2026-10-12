"""Reference evaluator for the generator's AST (tast.py).

Independent of the compiler: it encodes the language rules recorded in
SEMANTICS.md and nothing else. The programs must have been rendered first
(tast.render) so statements carry their source line.

evaluate(program) -> Result
    Result.stdout   the exact bytes the program prints
    Result.kind     'exit' | 'trap'
    Result.code     exit status for 'exit' (low 8 bits of main's value)
    Result.trap     the panic text for 'trap' (see SEMANTICS.md, Output)
    Result.line     source line the trap reports (0: the panic names none)

Value model (SEMANTICS.md, Aggregates): a struct or a sized array is a value
and is deep-copied whenever it is stored (declaration, assignment, parameter,
literal element, push, for-each variable). A slice and a List refer to
storage and are never copied. `self` in a method is the receiver itself.

Raises Undefined for programs whose result the language does not define
(none at present), and Timeout past a step budget.
"""

from tast import (INT_MIN, INT_MAX, StructT, ArrT, SliceT, ListT, Lit, BLit,
                  Var, Un, Bin, Call, Give, Field, Index, ArrLit, Default,
                  ListNew, Length, CopyE, Pop, Decl, Assign, Comp, IncDec,
                  Print, If, While, ForRange, ForEach, MethodCall, Push, Break,
                  Continue, Return, SLit, SCall, ToStr, CharAt)

STEP_LIMIT = 3_000_000
# A string longer than this ends the program as a Timeout (it is regenerated):
# `s = s + s` in nested loops would otherwise grow without bound.
STRING_LIMIT = 4096

LIST_GET_OOB = "List[]: index out of bounds"
LIST_SET_OOB = "List[]=: index out of bounds"
LIST_POP_EMPTY = "List.pop: pop on empty list"
SUBSTRING_OOB = "strings.substring: index out of bounds"
STRING_INDEX_OOB = "String index out of bounds"
# strings.tr trim(): space, tab, newline, carriage return only.
TRIM_SET = " \t\n\r"


class Undefined(Exception):
    pass


class Timeout(Exception):
    pass


class Trap(Exception):
    def __init__(self, kind, line):
        self.kind, self.line = kind, line


class _Ret(Exception):
    def __init__(self, v):
        self.v = v


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class Result:
    def __init__(self, stdout, kind, code=0, trap=None, line=0):
        self.stdout, self.kind, self.code, self.trap, self.line = stdout, kind, code, trap, line

    def describe(self):
        if self.kind == "exit":
            return "exit %d" % self.code
        return "trap %s line %d" % (self.trap, self.line)


# ---------------------------------------------------------------------- values

class SV:
    """A struct value."""

    def __init__(self, f):
        self.f = f


class AV:
    """Array storage: a sized array, or what a slice views."""

    def __init__(self, items):
        self.items = items


class LV:
    """A List: one shared header (turflang LEC-009, F-COLL-1). Every name
    for a List holds this same object, so `=`, passing and returning share
    it, length included; only `.copy()` makes a new one."""

    def __init__(self, items):
        self.items = items


def cp(v):
    if isinstance(v, SV):
        return SV({k: cp(x) for k, x in v.f.items()})
    if isinstance(v, AV):
        return AV([cp(x) for x in v.items])
    return v


def store(v, ty):
    """The value a location of type ty receives: a view is never copied."""
    if isinstance(ty, (SliceT, ListT)):
        return v
    return cp(v)


def overwrite(dst, src):
    """Assign src into dst's existing storage. A variable's storage never
    moves, so a for-each over an array (or a field of one) that the loop
    body reassigns sees the new elements (probe: trace of seed 8191)."""
    slots = dst.f if isinstance(dst, SV) else dst.items
    new = src.f if isinstance(src, SV) else src.items
    keys = list(new.keys()) if isinstance(src, SV) else range(len(new))
    for k in keys:
        if isinstance(slots[k], (SV, AV)):
            overwrite(slots[k], new[k])
        else:
            slots[k] = new[k]


def put(c, k, v, ty):
    """c[k] = v for a location of type ty."""
    old = c[k] if (isinstance(c, dict) and k in c) or isinstance(c, list) else None
    if isinstance(old, (SV, AV)) and not isinstance(ty, (SliceT, ListT)):
        overwrite(old, cp(v))
    else:
        c[k] = store(v, ty)


def zero(ty):
    if ty == "int":
        return 0
    if ty == "bool":
        return False
    if ty == "string":
        return ""
    if isinstance(ty, StructT):
        return SV({n: zero(t) for n, t in ty.fields})
    if isinstance(ty, ArrT):
        return AV([zero(ty.elem) for _ in range(ty.n)])
    if isinstance(ty, ListT):
        return LV([])
    raise TypeError(ty)


def _in_range(v):
    return INT_MIN <= v <= INT_MAX


def _tdiv(a, b):
    """Division truncating toward zero."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def array_name(base):
    return base.name if isinstance(base, Var) else "array"


class _Machine:
    def __init__(self, program):
        self.funcs = {f.name: f for f in program.funcs}
        self.methods = {(m.struct.name, m.name): m
                        for st in program.structs for m in st.methods}
        self.out = []
        self.steps = 0
        self.line = 0  # line of the statement / branch being evaluated

    def tick(self):
        self.steps += 1
        if self.steps > STEP_LIMIT:
            raise Timeout()

    def trap(self, kind, line=None):
        raise Trap(kind, self.line if line is None else line)

    # ------------------------------------------------------------ locations
    def norm(self, obj, i, base, for_set):
        n = len(obj.items)
        j = i + n if i < 0 else i
        if 0 <= j < n:
            return j
        if isinstance(obj, LV):
            self.trap(LIST_SET_OOB if for_set else LIST_GET_OOB)
        self.trap("Array index out of bounds for '%s'" % array_name(base))

    def obj(self, e, env):
        """The live value e denotes (no copy): used for bases and reads."""
        self.tick()
        if isinstance(e, Var):
            return env[e.name]
        if isinstance(e, Field):
            return self.obj(e.base, env).f[e.name]
        if isinstance(e, Index):
            o = self.obj(e.base, env)
            i = self.ev(e.idx, env)
            return o.items[self.norm(o, i, e.base, False)]
        return self.ev(e, env)

    def locate(self, place, env, for_set=True):
        """(container, key) of a place; indices are evaluated outer to inner."""
        if isinstance(place, Var):
            return env, place.name
        if isinstance(place, Field):
            return self.obj(place.base, env).f, place.name
        if isinstance(place, Index):
            o = self.obj(place.base, env)
            i = self.ev(place.idx, env)
            return o.items, self.norm(o, i, place.base, for_set)
        raise TypeError(place)

    # ----------------------------------------------------------- expressions
    def ev(self, e, env):
        self.tick()
        if isinstance(e, (Lit, BLit)):
            return e.v
        if isinstance(e, (Var, Field, Index)):
            return self.obj(e, env)
        if isinstance(e, Un):
            v = self.ev(e.e, env)
            if e.op == "!":
                return not v
            if e.op == "~":
                return ~v
            if v == INT_MIN:
                self.trap("Integer overflow")
            return -v
        if isinstance(e, Bin):
            return self.binary(e, env)
        if isinstance(e, Call):
            args = [self.ev(a, env) for a in e.args]  # left to right
            return self.call(self.funcs[e.fn], args)
        if isinstance(e, Give):
            return self.ev(e.a, env) if self.ev(e.c, env) else self.ev(e.b, env)
        if isinstance(e, ArrLit):
            return AV([store(self.ev(x, env), e.ty.elem) for x in e.elems])
        if isinstance(e, (Default, ListNew)):
            return zero(e.ty)
        if isinstance(e, Length):
            return len(self.obj(e.base, env).items)
        if isinstance(e, CopyE):
            src = self.obj(e.base, env)
            if isinstance(src, LV):
                return LV([cp(x) for x in src.items])
            return AV([cp(x) for x in src.items])
        if isinstance(e, Pop):
            lv = self.obj(e.base, env)
            if not lv.items:
                self.trap(LIST_POP_EMPTY)
            return lv.items.pop()
        if isinstance(e, SLit):
            return e.v
        if isinstance(e, ToStr):
            return str(self.ev(e.e, env))
        if isinstance(e, CharAt):
            s = self.ev(e.base, env)
            i = self.ev(e.idx, env)
            n = len(s)
            j = i + n if i < 0 else i  # wraps once, operators.md
            if not 0 <= j < n:
                self.trap(STRING_INDEX_OOB)
            return ord(s[j])
        if isinstance(e, SCall):
            args = [self.ev(a, env) for a in e.args]  # left to right
            return self.string_fn(e.fn, args)
        raise TypeError(e)

    def string_fn(self, fn, a):
        """lib/std/core/strings.tr, on ASCII-only strings (SEMANTICS.md)."""
        s = a[0]
        if fn == "length":
            return len(s)
        if fn == "index_of":
            return s.find(a[1])        # "" is found at 0
        if fn == "last_index_of":
            return s.rfind(a[1])       # "" is found at len(s)
        if fn == "count_char":
            return sum(1 for c in s if ord(c) == a[1])
        if fn == "contains":
            return a[1] in s
        if fn == "starts_with":
            return s.startswith(a[1])
        if fn == "ends_with":
            return s.endswith(a[1])
        if fn == "repeat":
            return self.sized(s * a[1] if a[1] > 0 else "")
        if fn == "substring":
            st, en = a[1], a[2]
            if st < 0 or en < st or en > len(s):
                self.trap(SUBSTRING_OOB)
            return s[st:en]
        if fn == "replace":
            return s if a[1] == "" else self.sized(s.replace(a[1], a[2]))
        if fn == "to_upper":
            return "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in s)
        if fn == "to_lower":
            return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)
        if fn == "trim":
            return s.strip(TRIM_SET)
        if fn == "trim_start":
            return s.lstrip(TRIM_SET)
        if fn == "trim_end":
            return s.rstrip(TRIM_SET)
        raise ValueError(fn)

    @staticmethod
    def sized(s):
        if len(s) > STRING_LIMIT:
            raise Timeout()
        return s

    def binary(self, e, env):
        op = e.op
        if op == "&&":
            return bool(self.ev(e.l, env)) and bool(self.ev(e.r, env))
        if op == "||":
            return bool(self.ev(e.l, env)) or bool(self.ev(e.r, env))
        a = self.ev(e.l, env)
        b = self.ev(e.r, env)
        return self.apply(op, a, b)

    def apply(self, op, a, b, compound=False):
        if isinstance(a, str):
            if op == "+":
                return self.sized(a + b)
            # Comparisons are strcmp: byte order, which is code-point order
            # for ASCII.
            return {"==": a == b, "!=": a != b, "<": a < b, ">": a > b,
                    "<=": a <= b, ">=": a >= b}[op]
        if op == "+":
            r = a + b
        elif op == "-":
            r = a - b
        elif op == "*":
            r = a * b
        elif op in ("/", "%"):
            if b == 0:
                self.trap(("Division by zero" if op == "/" else "Modulo by zero")
                          + (" in compound assignment" if compound else ""))
            if a == INT_MIN and b == -1:
                if op == "/":
                    self.trap("Integer overflow")
                return 0  # INT_MIN % -1, turflang 20385de
            q = _tdiv(a, b)
            return q if op == "/" else a - q * b
        elif op == "&":
            return a & b
        elif op == "|":
            return a | b
        elif op == "^":
            return a ^ b
        elif op in ("<<", ">>"):
            if not 0 <= b < 64:
                self.trap("Integer overflow")
            if op == ">>":
                return a >> b  # Python's >> on ints is arithmetic
            r = a << b
        elif op == "==":
            return a == b
        elif op == "!=":
            return a != b
        elif op == "<":
            return a < b
        elif op == ">":
            return a > b
        elif op == "<=":
            return a <= b
        elif op == ">=":
            return a >= b
        else:
            raise ValueError(op)
        if not _in_range(r):
            self.trap("Integer overflow")
        return r

    # ------------------------------------------------------------ statements
    def bind(self, params, args):
        return {n: store(a, t) for (n, t), a in zip(params, args)}

    def call(self, f, args):
        env = self.bind(f.params, args)
        saved = self.line
        try:
            self.block(f.body, env)
        except _Ret as r:
            self.line = saved
            return r.v
        raise AssertionError("function %s fell off its end" % f.name)

    def call_method(self, m, recv, args):
        env = self.bind(m.params, args)
        env["self"] = recv
        saved = self.line
        self.block(m.body, env)
        self.line = saved

    def block(self, stmts, env):
        for s in stmts:
            self.stmt(s, env)

    def loop_body(self, body, env):
        """Run one iteration; True means break."""
        try:
            self.block(body, env)
        except _Break:
            return True
        except _Continue:
            pass
        return False

    def stmt(self, s, env):
        self.tick()
        self.line = getattr(s, "line", self.line)
        if isinstance(s, Decl):
            env[s.name] = zero(s.ty) if s.e is None else store(self.ev(s.e, env), s.ty)
        elif isinstance(s, Assign):
            p = s.place
            v = self.ev(s.e, env)  # the value first, then the place (List too)
            c, k = self.locate(p, env)
            put(c, k, v, p.ty)
        elif isinstance(s, Comp):
            rhs = self.ev(s.e, env)
            c, k = self.locate(s.place, env)
            c[k] = self.apply(s.op, c[k], rhs, True)
        elif isinstance(s, IncDec):
            env[s.name] = self.apply("+" if s.op == "++" else "-", env[s.name], 1)
        elif isinstance(s, Print):
            v = self.ev(s.e, env)
            if isinstance(v, str):
                self.out.append(v + "\n")
            else:
                if isinstance(v, bool):
                    v = 1 if v else 0
                self.out.append("%d\n" % v)
        elif isinstance(s, MethodCall):
            args = [self.ev(a, env) for a in s.args]  # arguments, then receiver
            recv = self.obj(s.recv, env)
            self.call_method(self.methods[(s.recv.ty.name, s.method)], recv, args)
        elif isinstance(s, Push):
            lv = self.obj(s.base, env)
            lv.items.append(store(self.ev(s.e, env), s.base.ty.elem))
        elif isinstance(s, If):
            for cond, body, line in s.branches:
                self.line = line
                if self.ev(cond, env):
                    self.block(body, env)
                    return
            if s.els is not None:
                self.block(s.els, env)
        elif isinstance(s, While):
            self.line = s.decl_line
            env[s.counter] = 0
            while True:
                self.line = s.line
                self.tick()
                if not env[s.counter] < s.bound:
                    break
                self.line = s.incr_line
                env[s.counter] = self.apply("+", env[s.counter], 1)
                if self.loop_body(s.body, env):
                    break
        elif isinstance(s, ForRange):
            for i in range(s.lo, s.hi):
                self.tick()
                env[s.var] = i
                if self.loop_body(s.body, env):
                    break
        elif isinstance(s, ForEach):
            o = self.obj(s.base, env)
            for i in range(len(o.items)):  # live: sees writes made by the body
                self.tick()
                if s.ivar is not None:
                    env[s.ivar] = i
                env[s.var] = cp(o.items[i])
                if self.loop_body(s.body, env):
                    break
        elif isinstance(s, Break):
            raise _Break()
        elif isinstance(s, Continue):
            raise _Continue()
        elif isinstance(s, Return):
            raise _Ret(self.ev(s.e, env))
        else:
            raise TypeError(s)


def evaluate(program):
    m = _Machine(program)
    try:
        v = m.call(program.main, [])
    except Trap as t:
        return Result("".join(m.out), "trap", trap=t.kind, line=t.line)
    return Result("".join(m.out), "exit", code=v & 0xFF)
