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
                  Continue, Return, SLit, SCall, ToStr, CharAt, ErrLit, Try,
                  RecoverP, ErrIs, ErrMsg, Defer, RecoverRun, EnumT, EnumLit,
                  EnumOf, EnumTo, EnumCount, Compare, DLit, CLit, Conv, CollT,
                  CollNew, CMeth, CIndex, TryGet, CStmt, GCall, GGet, GPut,
                  RefT, Null, Heap, AddrOf, RefCast, IsNull, ErrCompare, KpKey)
import math

import sys

# Recursive helpers nest up to REC_LEVELS deep per helper, several Python
# frames per Turf call.
sys.setrecursionlimit(20000)

STEP_LIMIT = 3_000_000
# A string longer than this ends the program as a Timeout (it is regenerated):
# `s = s + s` in nested loops would otherwise grow without bound.
STRING_LIMIT = 4096

LIST_GET_OOB = "List[]: index out of bounds"
LIST_SET_OOB = "List[]=: index out of bounds"
LIST_POP_EMPTY = "List.pop: pop on empty list"
SUBSTRING_OOB = "strings.substring: index out of bounds"
SET_INDEX_OOB = "Set[]: index out of bounds"
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

def fmt_double(v):
    """printf("%g"), except that a NaN is "nan" whatever its sign (turflang
    402eb39/e0c6ea0, owner decision 2026-10-12). Python's %g is C's %g."""
    return "nan" if math.isnan(v) else "%g" % v


def wrap32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >= 1 << 31 else v


class EnumV:
    """An enum value: its type and ordinal. Immutable, compared by value."""

    def __init__(self, ty, k):
        self.ty, self.k = ty, k

    def __eq__(self, o):
        return isinstance(o, EnumV) and o.ty == self.ty and o.k == self.k

    def __hash__(self):
        return hash((self.ty.name, self.k))


class ErrV:
    """A failed !T: which variant of which error type, its message ("" when
    none) and its payload fields, stored as a location of each field's type
    would store them: a struct is a copy, a List or a reference is shared
    with whoever else holds it."""

    def __init__(self, etype, variant, msg, fields=()):
        self.etype, self.variant, self.msg, self.fields = etype, variant, msg, list(fields)


class RefV:
    """A non-null reference: `to` is the struct storage (an SV) it points at,
    on the heap (`heap(e)`, a defaulted `&S` field) or a caller's local
    (`&s`). null is None. Two references are equal when they point at the same
    storage; copying a struct that holds one shares what it points at."""

    def __init__(self, to):
        self.to = to

    def __eq__(self, o):
        return isinstance(o, RefV) and o.to is self.to

    def __ne__(self, o):
        return not self.__eq__(o)

    def __hash__(self):
        return id(self.to)


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


class CV:
    """A Stack, Queue, Set or Map: one shared header, like LV (LEC-009).
    `keys` is the dense, insertion-ordered storage (a Stack's or Queue's
    elements, a Set's members, a Map's keys); `vals` is a Map's values.
    remove() moves the last entry into the hole (set.tr / map.tr)."""

    def __init__(self, kind, keys=None, vals=None):
        self.kind, self.keys, self.vals = kind, keys or [], vals or []

    def copy(self):
        return CV(self.kind, list(self.keys), list(self.vals))

    def remove(self, k):
        if k not in self.keys:
            return False
        i = self.keys.index(k)
        last = len(self.keys) - 1
        self.keys[i] = self.keys[last]
        self.keys.pop()
        if self.kind == "Map":
            self.vals[i] = self.vals[last]
            self.vals.pop()
        return True


def cp(v):
    if isinstance(v, SV):
        return SV({k: cp(x) for k, x in v.f.items()})
    if isinstance(v, AV):
        return AV([cp(x) for x in v.items])
    return v


def store(v, ty):
    """The value a location of type ty receives: a view is never copied."""
    if isinstance(ty, (SliceT, ListT, CollT)):
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
    if ty == "double":
        return 0.0
    if ty in ("int32", "char"):
        return 0
    if isinstance(ty, EnumT):
        return EnumV(ty, 0)  # storage is zero: the first variant
    if isinstance(ty, RefT):
        # `&?S` defaults to null; `&S` cannot be null, so `::default()` gives
        # it a fresh `S::default()` on the heap (FREEZE F-COLL-2).
        return None if ty.nullable else RefV(zero(ty.st))
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
        self.defers = []  # one list per block being run, innermost last
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
            return self.target(e.base, env).f[e.name]
        if isinstance(e, Index):
            o = self.obj(e.base, env)
            i = self.ev(e.idx, env)
            return o.items[self.norm(o, i, e.base, False)]
        return self.ev(e, env)

    def target(self, e, env):
        """The struct storage a field access or method call on e reaches:
        e's own value, or what it points at when e is a reference. The
        generator only reads through a reference the compiler can prove
        non-null, so a null here is a generator bug, never a program result."""
        v = self.obj(e, env)
        if isinstance(e.ty, RefT):
            assert v is not None, "null dereference in a generated program"
            return v.to
        return v

    def locate(self, place, env, for_set=True):
        """(container, key) of a place; indices are evaluated outer to inner."""
        if isinstance(place, Var):
            return env, place.name
        if isinstance(place, Field):
            return self.target(place.base, env).f, place.name
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
            if isinstance(v, float):
                return -v  # IEEE negate, never a trap
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
            if isinstance(src, CV):
                return src.copy()
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
        if isinstance(e, GCall):
            args = [self.ev(a, env) for a in e.args]  # left to right
            return self.call(self.funcs[e.fn], args)  # T's values pass by value
        if isinstance(e, GGet):
            return cp(self.obj(e.recv, env).f["v"])
        if isinstance(e, CollNew):
            return CV(e.ty.kind)
        if isinstance(e, CMeth):
            args = [self.ev(a, env) for a in e.args]  # arguments, then the receiver
            return self.coll_method(self.obj(e.base, env), e.name, args)
        if isinstance(e, CIndex):
            c = self.obj(e.base, env)
            k = self.ev(e.idx, env)
            if c.kind == "Set":
                if not 0 <= k < len(c.keys):  # no negative index on a Set
                    self.trap(SET_INDEX_OOB)
                return c.keys[k]
            return self.coll_method(c, "get", [k])
        if isinstance(e, TryGet):
            c = self.obj(e.base, env)
            k = self.ev(e.key, env)
            if k in c.keys:
                return c.vals[c.keys.index(k)]
            return self.ev(e.fallback, env)  # only on a miss
        if isinstance(e, DLit):
            return e.v
        if isinstance(e, CLit):
            return ord(e.ch)
        if isinstance(e, KpKey):
            # Kp's `operator ==` compares the two fields, so equal keys are
            # equal tuples: what CV's `k in keys` needs.
            return ("Kp", self.ev(e.c, env), self.ev(e.n, env))
        if isinstance(e, Conv):
            return self.convert(self.ev(e.e, env), e.e.ty, e.to)
        if isinstance(e, EnumLit):
            return EnumV(e.ty, e.k)
        if isinstance(e, EnumOf):
            k = self.ev(e.e, env)
            assert 0 <= k < len(e.ty.variants), "generator keeps enum casts in range"
            return EnumV(e.ty, k)
        if isinstance(e, EnumTo):
            v = self.ev(e.e, env)
            return v.k if e.to == "int" else v.ty.variants[v.k]
        if isinstance(e, EnumCount):
            return len(e.enum.variants)
        if isinstance(e, ErrLit):
            # Payload arguments left to right, then the message.
            fields = [store(self.ev(a, env), t) for a, t in zip(e.args, e.ftypes)]
            return ErrV(e.etype, e.variant, "" if e.msg is None else self.ev(e.msg, env),
                        fields)
        if isinstance(e, Try):
            v = self.ev(e.call, env)
            if isinstance(v, ErrV):
                raise _Ret(v)  # unchanged, after this function's pending defers
            return v
        if isinstance(e, RecoverP):
            v = self.ev(e.call, env)
            return self.ev(e.fallback, env) if isinstance(v, ErrV) else v
        if isinstance(e, ErrIs):
            # Which variant it is, never its payload or message.
            return env[e.err].etype == e.etype and env[e.err].variant == e.variant
        if isinstance(e, ErrMsg):
            return env[e.err].msg
        if isinstance(e, Null):
            return None
        if isinstance(e, Heap):
            return RefV(cp(self.ev(e.e, env)))  # a copy: later writes to e miss it
        if isinstance(e, AddrOf):
            return RefV(self.obj(e.place, env))  # the caller's own storage
        if isinstance(e, RefCast):
            v = self.ev(e.e, env)
            assert v is not None, "cast of null to &T in a generated program"
            return v
        if isinstance(e, IsNull):
            return (self.ev(e.e, env) is None) != e.neg
        raise TypeError(e)

    def coll_method(self, c, name, a):
        """lib/std/collections/{stack,queue,set,map}.tr."""
        ks = c.keys
        if name == "length":
            return len(ks)
        if name in ("push", "enqueue"):
            ks.append(a[0])
        elif name == "pop":
            if not ks:
                self.trap("Stack.pop: pop on empty stack")
            return ks.pop()
        elif name == "dequeue":
            if not ks:
                self.trap("Queue.dequeue: dequeue on empty queue")
            return ks.pop(0)
        elif name == "peek":
            if not ks:
                self.trap("Stack.peek: peek on empty stack" if c.kind == "Stack"
                          else "Queue.peek: peek on empty queue")
            return ks[-1] if c.kind == "Stack" else ks[0]
        elif name == "add":
            if a[0] not in ks:
                ks.append(a[0])
        elif name == "contains":
            return a[0] in ks
        elif name == "index_of":
            return ks.index(a[0]) if a[0] in ks else -1
        elif name == "remove":
            return c.remove(a[0])  # a Set's is a bool; a Map's is a statement
        elif name == "put":
            if a[0] in ks:
                c.vals[ks.index(a[0])] = a[1]
            else:
                ks.append(a[0])
                c.vals.append(a[1])
        elif name == "get":
            if a[0] not in ks:
                self.trap("Map.get: no such key")
            return c.vals[ks.index(a[0])]
        elif name in ("keys", "values"):
            return LV(list(ks if name == "keys" else c.vals))  # a fresh List
        else:
            raise ValueError(name)
        return None

    def convert(self, v, frm, to):
        """`to` between numeric types (variables_and_types.md, Legal
        conversions; FREEZE 6.2 row 5)."""
        if to == "double":
            return float(v)               # round to nearest, like sitofp
        if to == "string":
            return fmt_double(v)          # only double -> string comes here
        if frm == "double":
            # Truncate toward zero; no integer value -> trap (turflang e0c6ea0).
            lo, hi = (-(2 ** 31) - 1, 2 ** 31) if to == "int32" else (-(2 ** 63), 2 ** 63)
            if math.isnan(v) or not (lo < v if to == "int32" else lo <= v) or not v < hi:
                self.trap("Integer overflow")
            return int(v)                 # int() truncates toward zero
        if to == "int32":
            return wrap32(v)              # int -> int32 truncates
        if to == "char":
            return v & 0xFF               # int -> char truncates
        return v                          # int32/char -> int widen

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
        if isinstance(a, float):
            return self.apply_double(op, a, b, compound)
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

    def apply_double(self, op, a, b, compound):
        """IEEE: no trap on overflow (inf) or NaN; `/` and `%` by zero (also
        -0.0) still trap (FREEZE 6.2); `%` is C fmod (sign of the dividend)."""
        if op in ("/", "%"):
            if b == 0.0:
                self.trap(("Division by zero" if op == "/" else "Modulo by zero")
                          + (" in compound assignment" if compound else ""))
            if op == "/":
                return a / b
            if math.isinf(a) or math.isnan(a) or math.isnan(b):
                return math.nan            # C fmod; Python's raises for inf
            return math.fmod(a, b)
        return {"+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b,
                "==": lambda: a == b, "!=": lambda: a != b, "<": lambda: a < b,
                ">": lambda: a > b, "<=": lambda: a <= b, ">=": lambda: a >= b}[op]()

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
        """Run a `{ }` block. Its defers run LIFO when it exits normally or by
        return/break/continue/try, and not on a panic (no unwinding:
        control_flow.md). A `return`'s value is computed before they run
        (probe d1.tr)."""
        defers = []
        self.defers.append(defers)
        try:
            for s in stmts:
                self.stmt(s, env)
        except (_Ret, _Break, _Continue):
            self.defers.pop()
            self.run_defers(defers, env)
            raise
        except BaseException:
            self.defers.pop()
            raise
        self.defers.pop()
        self.run_defers(defers, env)

    def run_defers(self, defers, env):
        for d in reversed(defers):
            self.stmt(d.stmt, env)

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
            if isinstance(p, CIndex):  # m[k] = v is put(k, v), key after the value
                c = self.obj(p.base, env)
                self.coll_method(c, "put", [self.ev(p.idx, env), v])
                return
            c, k = self.locate(p, env)
            put(c, k, v, p.ty)
        elif isinstance(s, GPut):
            v = self.ev(s.e, env)  # the argument, then the receiver
            sv = self.obj(s.recv, env)
            put(sv.f, "v", v, s.recv.ty.field_type("v"))
            sv.f["n"] = self.apply("+", sv.f["n"], 1)
        elif isinstance(s, CStmt):
            args = [self.ev(a, env) for a in s.args]
            self.coll_method(self.obj(s.base, env), s.name, args)
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
            elif isinstance(v, float):
                self.out.append(fmt_double(v) + "\n")
            else:
                if isinstance(v, bool):
                    self.out.append(("true" if v else "false") + "\n")
                else:
                    self.out.append("%d\n" % v)
        elif isinstance(s, MethodCall):
            args = [self.ev(a, env) for a in s.args]  # arguments, then receiver
            recv = self.target(s.recv, env)
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
                if s.guard is not None and env[s.guard.name] is None:
                    break
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
        elif isinstance(s, Defer):
            self.defers[-1].append(s)
        elif isinstance(s, Compare):
            v = self.ev(s.subject, env)  # once
            for pats, body in s.arms:
                if any(self.ev(p, env) == v for p in pats):
                    self.block(body, env)
                    return
            if s.els is not None:
                self.block(s.els, env)
        elif isinstance(s, ErrCompare):
            v = env[s.err]
            for arm in s.arms:
                if v.etype != arm.etype or v.variant != arm.variant:
                    continue
                if any(p[0] == "lit" and v.fields[i] != p[1].v
                       for i, p in enumerate(arm.pats)):
                    continue
                for i, p in enumerate(arm.pats):
                    if p[0] == "bind":
                        env[p[1]] = store(v.fields[i], p[2])
                self.line = arm.line
                if arm.guard is not None and not self.ev(arm.guard, env):
                    continue  # a failed guard falls through to the next arm
                self.block(arm.body, env)
                return
            self.block(s.els, env)
        elif isinstance(s, RecoverRun):
            v = self.ev(s.call, env)
            if isinstance(v, ErrV):
                if s.err is not None:
                    env[s.err] = v
                self.block(s.body, env)
                raise AssertionError("recover run block fell off its end")
            env[s.name] = v
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
