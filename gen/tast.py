"""The generator's own AST and its renderer to Turf source text.

The evaluator (evaluate.py) runs this AST directly; it never parses text.
Scalar types are the strings "int", "bool" and "string"; aggregates are StructT, ArrT
(a sized `T[N]`, a value type), SliceT (`T[]`, a view that shares storage)
and ListT (`List<T>` from the stdlib).

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
    "|": 33, "^": 35, "&": 37, "<<": 38, ">>": 38,
    "+": 40, "-": 40,
    "*": 50, "/": 50, "%": 50,
}
PREC_UNARY = 70
PREC_ATOM = 100

ARITH_OPS = ("+", "-", "*", "/", "%")
BIT_OPS = ("&", "|", "^", "<<", ">>")
INT_OPS = ARITH_OPS + BIT_OPS
CMP_OPS = ("<", ">", "<=", ">=")
EQ_OPS = ("==", "!=")


# ---------------------------------------------------------------------- types

class StructT:
    def __init__(self, name, fields):
        self.name = name
        self.fields = fields      # list of (field name, type)
        self.methods = []         # Method objects, filled in by the generator

    def field_type(self, f):
        for n, t in self.fields:
            if n == f:
                return t
        raise KeyError(f)

    def __repr__(self):
        return self.name


class ArrT:
    def __init__(self, elem, n):
        self.elem, self.n = elem, n

    def __eq__(self, o):
        return isinstance(o, ArrT) and o.n == self.n and o.elem == self.elem

    def __hash__(self):
        return hash(("arr", self.n, repr(self.elem)))

    def __repr__(self):
        return ty_str(self)


class SliceT:
    def __init__(self, elem):
        self.elem = elem

    def __eq__(self, o):
        return isinstance(o, SliceT) and o.elem == self.elem

    def __hash__(self):
        return hash(("slice", repr(self.elem)))

    def __repr__(self):
        return ty_str(self)


class ListT:
    def __init__(self, elem):
        self.elem = elem

    def __eq__(self, o):
        return isinstance(o, ListT) and o.elem == self.elem

    def __hash__(self):
        return hash(("list", repr(self.elem)))

    def __repr__(self):
        return ty_str(self)


class CollT:
    """Stack<K>, Queue<K>, Set<K> or Map<K, V>: a shared handle, like List."""

    def __init__(self, kind, key, val=None):
        self.kind, self.key, self.val = kind, key, val

    def __eq__(self, o):
        return (isinstance(o, CollT) and o.kind == self.kind and o.key == self.key
                and o.val == self.val)

    def __hash__(self):
        return hash(("coll", self.kind, repr(self.key), repr(self.val)))

    def __repr__(self):
        return ty_str(self)


class EnumT:
    def __init__(self, name, variants):
        self.name, self.variants = name, variants

    def __eq__(self, o):
        return isinstance(o, EnumT) and o.name == self.name

    def __hash__(self):
        return hash(("enum", self.name))

    def __repr__(self):
        return self.name


def ty_str(t):
    if isinstance(t, str):
        return t
    if isinstance(t, EnumT):
        return t.name
    if isinstance(t, StructT):
        return t.name
    if isinstance(t, ArrT):
        # int[2][3] is 2 rows of 3: the outer size is written first.
        dims = []
        while isinstance(t, ArrT):
            dims.append(t.n)
            t = t.elem
        return ty_str(t) + "".join("[%d]" % d for d in dims)
    if isinstance(t, SliceT):
        # The unsized dimension is the outer one: rows of int[3] are int[][3].
        inner = ty_str(t.elem)
        cut = inner.find("[")
        return inner + "[]" if cut < 0 else inner[:cut] + "[]" + inner[cut:]
    if isinstance(t, ListT):
        return "List<%s>" % ty_str(t.elem)
    if isinstance(t, CollT):
        args = ty_str(t.key) if t.val is None else "%s, %s" % (ty_str(t.key), ty_str(t.val))
        return "%s<%s>" % (t.kind, args)
    raise TypeError(t)


def is_scalar(t):
    """A value with no parts to place into: int, bool and string (immutable,
    so sharing its storage is never observable)."""
    return t in ("int", "bool", "string", "double", "int32", "char") or isinstance(t, EnumT)


def contains_string(t):
    if t == "string":
        return True
    if isinstance(t, StructT):
        return any(contains_string(ft) for _, ft in t.fields)
    if isinstance(t, (ArrT, SliceT, ListT)):
        return contains_string(t.elem)
    return False


def elem_of(t):
    return t.elem if isinstance(t, (ArrT, SliceT, ListT)) else None


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
    """op is '-' (int or double), '~' (int) or '!' (bool)."""

    def __init__(self, op, e):
        self.op = op
        self.e = e
        self.ty = "bool" if op == "!" else "double" if e.ty == "double" else "int"


class Bin:
    def __init__(self, op, l, r):
        self.op = op
        self.l = l
        self.r = r
        if op == "+" and l.ty == "string":
            self.ty = "string"  # strings.tr `operator +`
        elif op in ARITH_OPS and l.ty == "double":
            self.ty = "double"
        else:
            self.ty = "int" if op in INT_OPS else "bool"


class DLit:
    """A double literal, rendered with Python's repr (always has '.' or 'e')."""
    ty = "double"

    def __init__(self, v):
        self.v = v


class CLit:
    """A char literal 'x' (letters and digits only)."""
    ty = "char"

    def __init__(self, ch):
        self.ch = ch


class Conv:
    """(e to T) between int, double, int32 and char, or double -> string."""

    def __init__(self, e, to):
        self.e, self.to = e, to
        self.ty = to


class SLit:
    """A string literal; v holds only printable ASCII, tab and newline."""
    ty = "string"

    def __init__(self, v):
        self.v = v


class SCall:
    """strings::fn(args) with result type ty (STRING_FNS)."""

    def __init__(self, fn, args):
        self.fn, self.args = fn, args
        self.ty = STRING_FNS[fn]


class ToStr:
    """(e to string) for an int e."""
    ty = "string"

    def __init__(self, e):
        self.e = e


class CharAt:
    """(s[i] to int): one byte of a string as its code."""
    ty = "int"

    def __init__(self, base, idx):
        self.base, self.idx = base, idx


class EnumLit:
    """En::Vk"""

    def __init__(self, ty, k):
        self.ty, self.k = ty, k


class EnumOf:
    """(e to En) for an int e the generator keeps in [0, COUNT): a cast from a
    run-time int is unchecked (`(7 to En)` is a value named "<unknown enum>")."""

    def __init__(self, ty, e):
        self.ty, self.e = ty, e


class EnumTo:
    """(v to int) -- the ordinal -- or (v to string) -- the variant's name."""

    def __init__(self, e, to):
        self.e, self.to = e, to
        self.ty = to


class EnumCount:
    """En::COUNT"""
    ty = "int"

    def __init__(self, enum):
        self.enum = enum


class ErrLit:
    """E::V or E::V("message"): an error value, only ever returned."""
    ty = "error"

    def __init__(self, etype, variant, msg=None):
        self.etype, self.variant, self.msg = etype, variant, msg


class Try:
    """try call: the success value, or return the error from here."""
    ty = "int"

    def __init__(self, call):
        self.call = call


class RecoverP:
    """(call recover produce fallback); the fallback runs only on failure."""
    ty = "int"

    def __init__(self, call, fallback):
        self.call, self.fallback = call, fallback


class ErrIs:
    """err == E::V   (err bound by `recover as err run`)."""
    ty = "bool"

    def __init__(self, err, etype, variant):
        self.err, self.etype, self.variant = err, etype, variant


class ErrMsg:
    """err.message: "" when the variant was built without one."""
    ty = "string"

    def __init__(self, err):
        self.err = err


# The strings:: functions the generator calls, with their result types.
STRING_FNS = {
    "length": "int", "index_of": "int", "last_index_of": "int",
    "count_char": "int", "contains": "bool", "starts_with": "bool",
    "ends_with": "bool", "repeat": "string", "substring": "string",
    "replace": "string", "to_upper": "string", "to_lower": "string",
    "trim": "string", "trim_start": "string", "trim_end": "string",
}


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


class Field:
    def __init__(self, base, name):
        self.base, self.name = base, name
        self.ty = base.ty.field_type(name)


class Index:
    """base[idx] for an array, a slice or a List."""

    def __init__(self, base, idx):
        self.base, self.idx = base, idx
        self.ty = base.ty.elem


class ArrLit:
    def __init__(self, ty, elems):
        self.ty, self.elems = ty, elems


class Default:
    """S::default()"""

    def __init__(self, ty):
        self.ty = ty


class ListNew:
    """List::default()"""

    def __init__(self, ty):
        self.ty = ty


class Length:
    ty = "int"

    def __init__(self, base):
        self.base = base


class CopyE:
    """base.copy(): a fresh T[] on the GC heap, or for a List an independent
    List<T> (turflang LEC-009, F-COLL-1)."""

    def __init__(self, base):
        self.base = base
        if isinstance(base.ty, ListT):
            self.ty = ListT(base.ty.elem)
        elif isinstance(base.ty, CollT):
            self.ty = base.ty
        else:
            self.ty = SliceT(base.ty.elem)


class Pop:
    def __init__(self, base):
        self.base = base
        self.ty = base.ty.elem


class CollNew:
    """Stack::default(), Map::default(), ..."""

    def __init__(self, ty):
        self.ty = ty


class CMeth:
    """base.name(args) as an expression of type ty, on a Stack/Queue/Set/Map."""

    def __init__(self, base, name, args, ty):
        self.base, self.name, self.args, self.ty = base, name, args, ty


class CIndex:
    """s[i] on a Set (i-th member, no negative index) or m[k] on a Map."""
    ty = "int"

    def __init__(self, base, idx):
        self.base, self.idx = base, idx


class TryGet:
    """(m.try_get(k) recover produce d)"""
    ty = "int"

    def __init__(self, base, key, fallback):
        self.base, self.key, self.fallback = base, key, fallback


# ----------------------------------------------------------------- statements

class Decl:
    """mut/immut name: ty = e;   e None means no initializer (zeroed array)."""

    def __init__(self, name, ty, e, mutable=True):
        self.name, self.ty, self.e, self.mutable = name, ty, e, mutable
        self.line = 0


class Assign:
    """place = e;   place is a Var, Field or Index."""

    def __init__(self, place, e):
        self.place, self.e = place, e
        self.line = 0


class Comp:
    """place op= e   (op in + - * / % & | ^ << >>)."""

    def __init__(self, place, op, e):
        self.place, self.op, self.e = place, op, e
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


class ForRange:
    """for var in lo..hi { body }   lo, hi literals."""

    def __init__(self, var, lo, hi, body):
        self.var, self.lo, self.hi, self.body = var, lo, hi, body
        self.line = 0


class ForEach:
    """for v in base { body }  or  for (i, v) in base { body }."""

    def __init__(self, ivar, var, base, body):
        self.ivar, self.var, self.base, self.body = ivar, var, base, body
        self.line = 0


class MethodCall:
    """recv.method(args);   recv is a place of a struct type."""

    def __init__(self, recv, method, args):
        self.recv, self.method, self.args = recv, method, args
        self.line = 0


class Push:
    def __init__(self, base, e):
        self.base, self.e = base, e
        self.line = 0


class Defer:
    """defer <stmt>;  stmt is a Print, Assign or Comp. Runs at the exit of
    the enclosing block (also by return/break/continue/try), LIFO, never on
    a panic. A deferred Print renders as `defer io::flush();` followed by
    `defer io::printline(e);`, so its output is flushed like any print."""

    def __init__(self, stmt):
        self.stmt = stmt
        self.line = 0


class Compare:
    """compare subject { matches p: [matches q:] body done ... otherwise: body }
    arms: list of (patterns, body); patterns are EnumLit or Lit, all distinct;
    els: body or None. Bodies are never empty (E058)."""

    def __init__(self, subject, arms, els):
        self.subject, self.arms, self.els = subject, arms, els
        self.line = 0


class RecoverRun:
    """mut name: int = call recover as err run { body };   body diverges.
    err None is the plain form, `recover run { body }`, with no binding."""

    def __init__(self, name, call, err, body):
        self.name, self.call, self.err, self.body = name, call, err, body
        self.line = 0


class CStmt:
    """base.name(args);  -- push, enqueue, add, put, remove (Map)."""

    def __init__(self, base, name, args):
        self.base, self.name, self.args = base, name, args
        self.line = 0


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
    def __init__(self, name, params, body, fallible=False, rec=False):
        self.name = name
        self.params = params  # list of (name, type)
        self.body = body      # list of statements, last is Return
        self.cost = 0
        self.fallible = fallible  # returns !int
        self.rec = rec            # first parameter is the recursion depth `d`


class Method:
    """fn S.name(params): void { body }  -- self is a pointer to the receiver."""

    def __init__(self, struct, name, params, body):
        self.struct = struct
        self.name = name
        self.params = params
        self.body = body
        self.cost = 0


class Program:
    def __init__(self, structs, funcs, main, seed=0, paren_mode="min", errors=None,
                 enums=None):
        self.errors = errors or []  # [(error type name, [variants])]
        self.enums = enums or []    # [EnumT]
        self.structs = structs
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
    if isinstance(e, (Lit, DLit)) and (e.v < 0 or str(e.v).startswith("-")):
        return PREC_UNARY
    if isinstance(e, ErrIs):
        return PREC["=="]
    if isinstance(e, Try):
        return 1  # parenthesized inside any operator: `(try f(x)) * 2`
    return PREC_ATOM


def walk_stmts(stmts):
    """Every statement in stmts, nested bodies included."""
    for s in stmts:
        yield s
        if isinstance(s, If):
            for br in s.branches:
                yield from walk_stmts(br[1])
            if s.els:
                yield from walk_stmts(s.els)
        elif isinstance(s, (While, ForRange, ForEach, RecoverRun)):
            yield from walk_stmts(s.body)
        elif isinstance(s, Compare):
            for _, body in s.arms:
                yield from walk_stmts(body)
            if s.els:
                yield from walk_stmts(s.els)


def _uses_list(p):
    # A helper may take a List parameter with no List local anywhere.
    if any(isinstance(t, ListT) for f in p.funcs for _, t in f.params):
        return True
    bodies = [f.body for f in p.funcs + [p.main]]
    bodies += [m.body for st in p.structs for m in st.methods]
    for b in bodies:
        for s in walk_stmts(b):
            if isinstance(s, Decl) and isinstance(s.ty, ListT):
                return True
    return False


def _coll_kinds(p):
    out = set()
    bodies = [f.body for f in p.funcs + [p.main]]
    for b in bodies:
        for s in walk_stmts(b):
            if isinstance(s, Decl) and isinstance(s.ty, CollT):
                out.add(s.ty.kind)
    return out


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
        if isinstance(e, Field):
            return "%s.%s" % (self.expr(e.base, PREC_ATOM), e.name)
        if isinstance(e, Index):
            return "%s[%s]" % (self.expr(e.base, PREC_ATOM), self.expr(e.idx))
        if isinstance(e, ArrLit):
            return "[%s]" % ", ".join(self.expr(x) for x in e.elems)
        if isinstance(e, Default):
            return "%s::default()" % e.ty.name
        if isinstance(e, ListNew):
            return "List::default()"
        if isinstance(e, Length):
            return "%s.length()" % self.expr(e.base, PREC_ATOM)
        if isinstance(e, CopyE):
            return "%s.copy()" % self.expr(e.base, PREC_ATOM)
        if isinstance(e, Pop):
            return "%s.pop()" % self.expr(e.base, PREC_ATOM)
        if isinstance(e, CollNew):
            return "%s::default()" % e.ty.kind
        if isinstance(e, CMeth):
            return "%s.%s(%s)" % (self.expr(e.base, PREC_ATOM), e.name,
                                  ", ".join(self.expr(a) for a in e.args))
        if isinstance(e, CIndex):
            return "%s[%s]" % (self.expr(e.base, PREC_ATOM), self.expr(e.idx))
        if isinstance(e, TryGet):
            return "(%s.try_get(%s) recover produce %s)" % (
                self.expr(e.base, PREC_ATOM), self.expr(e.key), self.expr(e.fallback))
        if isinstance(e, DLit):
            return repr(e.v)
        if isinstance(e, CLit):
            return "'%s'" % e.ch
        if isinstance(e, Conv):
            return "(%s to %s)" % (self.expr(e.e, PREC_ATOM), e.to)
        if isinstance(e, SLit):
            return '"%s"' % e.v.replace("\t", "\\t").replace("\n", "\\n")
        if isinstance(e, SCall):
            return "strings::%s(%s)" % (e.fn, ", ".join(self.expr(a) for a in e.args))
        if isinstance(e, ToStr):
            return "(%s to string)" % self.expr(e.e, PREC_ATOM)
        if isinstance(e, CharAt):
            return "(%s[%s] to int)" % (self.expr(e.base, PREC_ATOM), self.expr(e.idx))
        if isinstance(e, EnumLit):
            return "%s::%s" % (e.ty.name, e.ty.variants[e.k])
        if isinstance(e, EnumOf):
            return "(%s to %s)" % (self.expr(e.e, PREC_ATOM), e.ty.name)
        if isinstance(e, EnumTo):
            return "(%s to %s)" % (self.expr(e.e, PREC_ATOM), e.to)
        if isinstance(e, EnumCount):
            return "%s::COUNT" % e.enum.name
        if isinstance(e, ErrLit):
            s = "%s::%s" % (e.etype, e.variant)
            return s if e.msg is None else s + "(%s)" % self.expr(e.msg)
        if isinstance(e, Try):
            return "try %s" % self.expr(e.call)
        if isinstance(e, RecoverP):
            return "(%s recover produce %s)" % (self.expr(e.call), self.expr(e.fallback))
        if isinstance(e, ErrIs):
            return "%s == %s::%s" % (e.err, e.etype, e.variant)
        if isinstance(e, ErrMsg):
            return "%s.message" % e.err
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
            if s.e is None:
                s.line = self.emit(ind, "%s %s: %s;" % (kw, s.name, ty_str(s.ty)))
            else:
                s.line = self.emit(ind, "%s %s: %s = %s;" % (kw, s.name, ty_str(s.ty), self.expr(s.e)))
        elif isinstance(s, Assign):
            s.line = self.emit(ind, "%s = %s;" % (self.expr(s.place), self.expr(s.e)))
        elif isinstance(s, Comp):
            s.line = self.emit(ind, "%s %s= %s;" % (self.expr(s.place), s.op, self.expr(s.e)))
        elif isinstance(s, IncDec):
            s.line = self.emit(ind, "%s%s;" % (s.name, s.op))
        elif isinstance(s, Print):
            s.line = self.emit(ind, "io::printline(%s);" % self.expr(s.e))
            self.emit(ind, "io::flush();")
        elif isinstance(s, Defer):
            if isinstance(s.stmt, Print):
                self.emit(ind, "defer io::flush();")
                s.stmt.line = s.line = self.emit(
                    ind, "defer io::printline(%s);" % self.expr(s.stmt.e))
            else:
                start = len(self.lines)
                self.stmt(s.stmt, ind)
                self.lines[start] = "    " * ind + "defer " + self.lines[start].lstrip()
                s.line = s.stmt.line
        elif isinstance(s, Compare):
            s.line = self.emit(ind, "compare (%s) {" % self.expr(s.subject))
            for pats, body in s.arms:
                for p in pats:
                    self.emit(ind + 1, "matches %s:" % self.expr(p))
                self.block(body, ind + 2)
                self.emit(ind + 1, "done")
            if s.els is not None:
                self.emit(ind + 1, "otherwise:")
                self.block(s.els, ind + 2)
            self.emit(ind, "}")
        elif isinstance(s, RecoverRun):
            how = "recover run {" if s.err is None else "recover as %s run {" % s.err
            s.line = self.emit(ind, "mut %s: int = %s %s" % (s.name, self.expr(s.call), how))
            self.block(s.body, ind + 1)
            self.emit(ind, "};")
        elif isinstance(s, CStmt):
            s.line = self.emit(ind, "%s.%s(%s);" % (self.expr(s.base, PREC_ATOM), s.name,
                                                    ", ".join(self.expr(a) for a in s.args)))
        elif isinstance(s, Break):
            s.line = self.emit(ind, "break;")
        elif isinstance(s, Continue):
            s.line = self.emit(ind, "continue;")
        elif isinstance(s, Return):
            s.line = self.emit(ind, "return %s;" % self.expr(s.e))
        elif isinstance(s, MethodCall):
            s.line = self.emit(ind, "%s.%s(%s);" % (self.expr(s.recv, PREC_ATOM), s.method,
                                                    ", ".join(self.expr(a) for a in s.args)))
        elif isinstance(s, Push):
            s.line = self.emit(ind, "%s.push(%s);" % (self.expr(s.base, PREC_ATOM), self.expr(s.e)))
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
        elif isinstance(s, ForRange):
            s.line = self.emit(ind, "for %s in %d..%d {" % (s.var, s.lo, s.hi))
            self.block(s.body, ind + 1)
            self.emit(ind, "}")
        elif isinstance(s, ForEach):
            head = s.var if s.ivar is None else "(%s, %s)" % (s.ivar, s.var)
            s.line = self.emit(ind, "for %s in %s {" % (head, self.expr(s.base, PREC_ATOM)))
            self.block(s.body, ind + 1)
            self.emit(ind, "}")
        else:
            raise TypeError(s)

    def params(self, ps):
        return ", ".join("%s: %s" % (n, ty_str(t)) for n, t in ps)

    def func(self, f, is_main=False):
        if is_main:
            self.emit(0, "fn main(): int {")
        else:
            self.emit(0, "fn %s(%s): %s {" % (f.name, self.params(f.params),
                                             "!int" if f.fallible else "int"))
        self.block(f.body, 1)
        self.emit(0, "}")
        self.emit(0, "")

    def method(self, m):
        self.emit(0, "fn %s.%s(%s): void {" % (m.struct.name, m.name, self.params(m.params)))
        self.block(m.body, 1)
        self.emit(0, "}")
        self.emit(0, "")

    def program(self):
        self.emit(0, "// turf-fuzz gen: seed=%d paren=%s" % (self.p.seed, self.p.paren_mode))
        self.emit(0, "import io;")
        if _uses_list(self.p):
            self.emit(0, "import list::List;")
        for kind in sorted(_coll_kinds(self.p)):
            self.emit(0, "import %s::%s;" % (kind.lower(), kind))
        self.emit(0, "")
        for name, variants in self.p.errors:
            self.emit(0, "error %s { %s }" % (name, ", ".join(variants)))
            self.emit(0, "")
        for en in self.p.enums:
            self.emit(0, "enum %s { %s }" % (en.name, ", ".join(en.variants)))
            self.emit(0, "")
        for st in self.p.structs:
            self.emit(0, "struct %s { %s }" % (st.name, ", ".join(
                "%s: %s" % (n, ty_str(t)) for n, t in st.fields)))
            self.emit(0, "")
        for st in self.p.structs:
            for m in st.methods:
                self.method(m)
        for f in self.p.funcs:
            self.func(f)
        self.func(self.p.main, True)
        return "\n".join(self.lines).rstrip("\n") + "\n"


def render(program):
    program.source = _Renderer(program).program()
    return program.source
