#!/usr/bin/env python3
"""Typed program generator for Turf, tier 1 of turf-fuzz.

Subset:
  * int and bool locals, literals, + - * / %, & | ^ << >> ~, comparisons,
    && || !, unary -, give/otherwise;
  * structs of int, bool, sized arrays and earlier structs, built with
    `S::default()`, with fields read and written, copied by assignment and
    by parameter, and with `void` methods that mutate `self`;
  * sized arrays `T[N]` (literals, zeroed declarations, element reads and
    writes, copies, `.length()`, for-each), `T[]` parameters that write
    through to the caller's array, and `.copy()` views on the GC heap;
  * `List<T>` locals (push, guarded index read and write, pop, length),
    shared through aliases and List<int> parameters, `.copy()`;
  * `string` locals, fields and elements: literals, `+`, `+=`, `to string`,
    comparisons, `s[i] to int`, and strings:: length, index_of,
    last_index_of, count_char, contains, starts_with, ends_with, repeat,
    substring, replace, to_upper, to_lower, trim, trim_start, trim_end;
  * an error type, `!int` helpers returning `E0::V` (with a message),
    `recover produce`, `recover as err run`, `try`; `defer`; recursive
    helpers with a literal depth; enums (ordinal and name casts, COUNT,
    ==) and `compare` over enums and ints; `double` (IEEE), `int32` and
    `char` with the `to` conversions between them and int; Stack, Queue,
    Set and Map locals (shared, copied, emptied, missed);
  * if/elseif/else, while with a bounded counter, `for i in a..b`,
    break/continue, helper functions returning int (calls only go to
    earlier functions, so no recursion), early return, printing.

Every program terminates (loop bounds are literals or array lengths, a static
cost estimate caps total work). The generator is deterministic per seed. The
expected result comes from evaluate.py, which runs this module's AST.

    python3 gen/generate.py SEED [-o prog.tr] [--expected expected.txt]
"""

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tast  # noqa: E402
from tast import (INT_MIN, INT_MAX, StructT, ArrT, SliceT, ListT, is_scalar,  # noqa: E402
                  Lit, BLit, Var, Un, Bin, Call, Give, Field, Index, ArrLit,
                  Default, ListNew, Length, CopyE, Pop, Decl, Assign, Comp,
                  IncDec, Print, If, While, ForRange, ForEach, MethodCall,
                  Push, Break, Continue, Return, Func, Method, Program,
                  SLit, SCall, ToStr, CharAt, contains_string, ErrLit, Try,
                  RecoverP, ErrIs, ErrMsg, Defer, RecoverRun, EnumT, EnumLit,
                  EnumOf, EnumTo, EnumCount, Compare, DLit, CLit, Conv, CollT,
                  CollNew, CMeth, CIndex, TryGet, CStmt)
import evaluate as ev  # noqa: E402

BOUNDARY = [INT_MAX, INT_MIN, INT_MAX - 1, INT_MIN + 1, 2147483647, -2147483648,
            4294967296, 3037000500, 3037000499, -1, 0, 1, 2, -2]

# Scope entry kinds. Whole assignment only to MUT; element/field writes
# through MUT, SELF, VIEW and SLICE_PARAM; everything else is read-only.
MUT, IMMUT, PARAM, SELF, VIEW, SLICE_PARAM, LOOP = range(7)
WRITABLE = (MUT, SELF, VIEW, SLICE_PARAM)


class Ent:
    def __init__(self, name, ty, kind):
        self.name, self.ty, self.kind = name, ty, kind


class Scope:
    """Lexical scopes of the function being generated."""

    def __init__(self):
        self.frames = [[]]

    def push(self):
        self.frames.append([])

    def pop(self):
        self.frames.pop()

    def declare(self, name, ty, kind):
        self.frames[-1].append(Ent(name, ty, kind))

    def entries(self):
        for fr in self.frames:
            for e in fr:
                if e.name != "chk":  # only the checksum statements touch chk
                    yield e

    def vars(self, ty=None, assignable=False):
        """(name, ty) of scalar-or-any variables; assignable means whole-var MUT."""
        return [(e.name, e.ty) for e in self.entries()
                if (ty is None or e.ty == ty) and (e.kind == MUT or not assignable)]


def reaches(t, target, through_lists=False):
    """Can a place of type `target` be reached from a value of type t by
    fields and indexing?"""
    if t == target:
        return True
    if isinstance(t, StructT):
        return any(reaches(ft, target, through_lists) for _, ft in t.fields)
    if isinstance(t, (ArrT, SliceT)):
        return reaches(t.elem, target, through_lists)
    if isinstance(t, ListT) and through_lists:
        return reaches(t.elem, target, through_lists)
    return False


# ------------------------------------------------------- compile-time folding

class Overflow(Exception):
    pass


def const_value(e):
    """Value of a literal-only int/bool subtree, else None.

    Raises Overflow if folding it would leave int64: the compiler folds
    literal arithmetic at compile time and rejects an unrepresentable result
    (E076, FREEZE 6.2 row 6), so such a program is not a runtime-trap test.
    Bitwise operators on literals are not folded (probe: `1 << 63` and
    `(1 << 2) + 9223372036854775807` trap at run time)."""
    if isinstance(e, (Lit, BLit)):
        return e.v
    if isinstance(e, Un):
        v = const_value(e.e)
        if v is None or e.op == "~":
            return None
        if e.op == "!":
            return not v
        if v == INT_MIN:
            raise Overflow()
        return -v
    if isinstance(e, Bin):
        if e.op in tast.BIT_OPS:
            return None
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
            return None  # runtime check, not folded
        if op == "&&":
            return bool(a) and bool(b)
        if op == "||":
            return bool(a) or bool(b)
        return {"==": a == b, "!=": a != b, "<": a < b, ">": a > b,
                "<=": a <= b, ">=": a >= b}[op]
    return None


def const_value_safe(e):
    try:
        return const_value(e)
    except Overflow:
        return 0


def check_foldable(e):
    """Raise Overflow if any literal-only subtree of e would fold out of range."""
    if isinstance(e, (Lit, BLit, SLit, Var, Default, ListNew)):
        return
    if isinstance(e, SCall):
        for a in e.args:
            check_foldable(a)
        return
    if isinstance(e, ToStr):
        check_foldable(e.e)
        return
    if isinstance(e, CharAt):
        check_foldable(e.base)
        check_foldable(e.idx)
        return
    if isinstance(e, Try):
        check_foldable(e.call)
        return
    if isinstance(e, RecoverP):
        check_foldable(e.call)
        check_foldable(e.fallback)
        return
    if isinstance(e, (ErrIs, ErrMsg, EnumLit, EnumCount, DLit, CLit, CollNew)):
        return
    if isinstance(e, CMeth):
        for a in e.args:
            check_foldable(a)
        return
    if isinstance(e, CIndex):
        check_foldable(e.idx)
        return
    if isinstance(e, TryGet):
        check_foldable(e.key)
        check_foldable(e.fallback)
        return
    if isinstance(e, (EnumOf, EnumTo, Conv)):
        check_foldable(e.e)
        return
    if isinstance(e, ErrLit):
        if e.msg is not None:
            check_foldable(e.msg)
        return
    if isinstance(e, Un):
        check_foldable(e.e)
        const_value(e)
    elif isinstance(e, Bin):
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
    elif isinstance(e, Field):
        check_foldable(e.base)
    elif isinstance(e, Index):
        check_foldable(e.base)
        check_foldable(e.idx)
    elif isinstance(e, ArrLit):
        for x in e.elems:
            check_foldable(x)
    elif isinstance(e, (Length, CopyE, Pop)):
        check_foldable(e.base)


class Gen:
    def __init__(self, seed):
        self.seed = seed
        self.rng = random.Random(seed)
        r = self.rng
        self.tame = r.random() < 0.7        # keep values small: few traps
        self.safe_div = 0.985 if self.tame else 0.8
        self.boundary_p = 0.03 if self.tame else 0.12
        self.opaque_p = 0.01 if self.tame else 0.06
        self.paren_mode = r.choice(["min", "min", "rand", "full"])
        # Feature switches, per program, so plain int/bool programs stay common.
        self.bitwise = r.random() < 0.5
        self.use_structs = r.random() < 0.5
        self.use_arrays = r.random() < 0.5
        self.use_lists = r.random() < 0.35
        # Strings: GC-heavy (every +, to string and strings:: result is a new
        # GC block), also as struct fields and array/List elements.
        self.use_strings = r.random() < 0.4
        self.use_errors = r.random() < 0.35   # error type, !int helpers, recover, try
        self.use_defer = r.random() < 0.35
        self.use_rec = r.random() < 0.35      # helpers that call themselves
        self.use_numeric = r.random() < 0.4   # double, int32, char and casts
        self.use_colls = r.random() < 0.3     # Stack, Queue, Set, Map
        self.errors = []
        if self.use_errors:
            self.errors = [("E0", ["A", "B", "C"][:r.randint(1, 3)])]
        self.enums = []
        if r.random() < 0.35:
            names = ["Red", "Green", "Blue", "Gold"]
            for i in range(r.randint(1, 2)):
                self.enums.append(EnumT("En%d" % i, names[:r.randint(2, 4)]))
        self.cur_fn = None     # the Func being generated (None in main/methods)
        self.in_defer = False  # `try` is E095 inside a defer
        self.in_run = False    # ... and kept out of recover blocks
        self.structs = []
        self.funcs = []
        self.opaque_used = False
        self.scope = None
        self.names = {}
        self.mult = 1          # product of enclosing loop bounds
        self.cost = 0          # estimated steps spent in the current function
        self.cost_cap = 4000
        self.in_loop = 0
        self.in_func = False   # helper (non-main)
        self.in_method = False
        self.last_ty = "int"

    # --------------------------------------------------------------- helpers
    def chance(self, p):
        return self.rng.random() < p

    def pick(self, seq):
        return self.rng.choice(seq)

    def new_name(self, prefix):
        n = self.names.get(prefix, 0) + 1
        self.names[prefix] = n
        return "%s%d" % (prefix, n)

    # ---------------------------------------------------------------- types
    def gen_structs(self):
        for i in range(self.rng.randint(1, 3)):
            fields = []
            for j in range(self.rng.randint(1, 4)):
                k = self.rng.random()
                if k < 0.45:
                    t = "string" if self.use_strings and self.chance(0.3) else "int"
                    if self.enums and self.chance(0.25):
                        t = self.pick(self.enums)
                    elif self.use_numeric and self.chance(0.3):
                        t = self.pick(["double", "double", "int32", "char"])
                elif k < 0.62:
                    t = "bool"
                elif k < 0.80:
                    t = ArrT(self.elem_type(), self.rng.randint(1, 4))
                elif k < 0.95 and self.structs:
                    t = self.pick(self.structs)
                elif self.structs:
                    t = ArrT(self.pick(self.structs), self.rng.randint(1, 3))
                else:
                    t = "int"
                fields.append(("f%d" % j, t))
            self.structs.append(StructT("S%d" % i, fields))

    def elem_type(self):
        opts = ["int", "int", "int", "bool"]
        if self.use_strings:
            opts.append("string")
        if self.enums:
            opts.append(self.pick(self.enums))
        if self.use_numeric:
            opts.append(self.pick(["double", "double", "int32", "char"]))
        return self.pick(opts)

    def scalar_type(self):
        """The type of a new scalar local."""
        k = self.rng.random()
        if self.use_strings and k < 0.25:
            return "string"
        if self.enums and k < 0.4:
            return self.pick(self.enums)
        if self.use_numeric and k < 0.6:
            return self.pick(["double", "double", "int32", "char"])
        return "int" if self.chance(0.75) else "bool"

    def zero_of(self, t):
        if isinstance(t, EnumT):
            return EnumLit(t, 0)
        if t in ("int32", "char"):
            return Conv(Lit(0), t)
        return {"int": Lit(0), "bool": BLit(False), "string": SLit(""),
                "double": DLit(0.0)}[t]

    def agg_type(self):
        """A type for a new aggregate local."""
        opts = []
        if self.use_arrays:
            opts += ["arr"] * 3
        if self.use_structs and self.structs:
            opts += ["struct"] * 3
            if self.use_arrays:
                opts += ["sarr"]
        if self.use_lists and not self.in_method:
            opts += ["list"]
        if self.use_colls and not self.in_method:
            opts += ["coll", "coll"]
        if not opts:
            return None
        k = self.pick(opts)
        if k == "coll":
            kind = self.pick(["Stack", "Queue", "Set", "Map", "Map"])
            if kind != "Map":
                return CollT(kind, "int")
            return CollT("Map", "string" if self.use_strings and self.chance(0.4) else "int", "int")
        if k == "struct":
            return self.pick(self.structs)
        if k == "sarr":
            return ArrT(self.pick(self.structs), self.rng.randint(1, 3))
        if k == "list":
            if self.structs and self.chance(0.25):
                return ListT(self.pick(self.structs))
            if self.use_strings and self.chance(0.3):
                return ListT("string")
            return ListT("int")
        n = self.rng.randint(1, 5)
        if self.chance(0.1):
            return ArrT(ArrT("int", self.rng.randint(1, 3)), n)
        return ArrT(self.elem_type(), n)

    # ----------------------------------------------------------- places
    def roots(self, writable):
        out = []
        for e in self.scope.entries():
            if writable and e.kind not in WRITABLE:
                continue
            out.append(e)
        return out

    def descend(self, root, t, target, depth):
        """A random path of fields and indices from root (of type t) to a
        value of type target, or None if there is none."""
        if t == target and (not self._has_children(t, target) or self.chance(0.7)):
            return root
        got = self.descend_strict(root, t, target, depth)
        if got is None and t == target:
            return root
        return got

    def _has_children(self, t, target):
        if isinstance(t, StructT):
            return any(reaches(ft, target) for _, ft in t.fields)
        if isinstance(t, (ArrT, SliceT)):
            return reaches(t.elem, target)
        return False

    def place(self, target, mode="assign", depth=2):
        """An expression of type target rooted at a variable, or None.

        mode 'read'   any variable, for reading;
             'assign' a place that may be assigned: the variable itself only
                      if it is a whole-assignable MUT local;
             'mutate' a receiver for a method call: any writable variable."""
        cands = []
        for e in self.roots(mode != "read"):
            if isinstance(e.ty, ListT) or not reaches(e.ty, target):
                continue
            if e.ty == target and not self._root_ok(e, mode) \
                    and not self._has_children(e.ty, target):
                continue
            cands.append(e)
        if not cands:
            return None
        e = self.pick(cands)
        root = Var(e.name, e.ty)
        if e.ty == target and not self._root_ok(e, mode):
            return self.descend_strict(root, e.ty, target, depth)
        return self.descend(root, e.ty, target, depth)

    @staticmethod
    def _root_ok(e, mode):
        if mode == "assign":
            return e.kind == MUT
        if mode == "mutate":
            return e.kind in WRITABLE
        return e.kind != SELF  # `self` is a pointer: read its fields, not it

    def descend_strict(self, root, t, target, depth):
        if isinstance(t, StructT):
            fs = [(f, ft) for f, ft in t.fields if reaches(ft, target)]
            if not fs:
                return None
            f, ft = self.pick(fs)
            return self.descend(Field(root, f), ft, target, depth)
        if isinstance(t, (ArrT, SliceT)):
            return self.descend(Index(root, self.gen_index(root, depth)), t.elem, target, depth)
        return None

    def gen_index(self, base, depth):
        t = base.ty
        k = self.rng.random()
        if k < self.opaque_p:
            self.opaque_used = True
            return Call("opaque", [self.gen_int(max(depth - 1, 0))])
        if isinstance(t, ArrT):
            if k < 0.4:
                return Lit(self.rng.randint(-t.n, t.n - 1))
            return Bin("%", self.gen_int(max(depth - 1, 0)), Lit(t.n))
        # A slice: never empty here (every array has n >= 1), length unknown.
        if k < 0.3:
            return Lit(self.pick([0, -1]))
        return Bin("%", self.gen_int(max(depth - 1, 0)), Length(base))

    def list_read(self, target, depth):
        """(l.length() > 0 give l[i]... otherwise zero) for a scalar target."""
        ls = [e for e in self.scope.entries()
              if isinstance(e.ty, ListT) and reaches(e.ty.elem, target)]
        if not ls:
            return None
        e = self.pick(ls)
        lv = Var(e.name, e.ty)
        if self.chance(self.opaque_p):
            self.opaque_used = True
            idx = Call("opaque", [self.gen_int(max(depth - 1, 0))])
        else:
            idx = Bin("%", self.gen_int(max(depth - 1, 0)), Length(lv))
        got = self.descend(Index(lv, idx), e.ty.elem, target, depth)
        if got is None:
            return None
        if isinstance(idx, Call):
            return got  # may be out of bounds on purpose
        return Give(Bin(">", Length(lv), Lit(0)), got, self.zero_of(target))

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

    def agg_read(self, target, depth):
        k = self.rng.random()
        if k < 0.75:
            return self.place(target, "read", depth)
        if k < 0.9:
            return self.list_read(target, depth)
        arrs = [e for e in self.scope.entries()
                if isinstance(e.ty, (ArrT, SliceT, ListT)) and target == "int"]
        if arrs:
            e = self.pick(arrs)
            return Length(Var(e.name, e.ty))
        return None

    def int_leaf(self, depth=0):
        if self.chance(0.3):
            got = self.agg_read("int", depth)
            if got is not None:
                return got
        vs = [(n, t) for n, t in self.scope.vars("int")]
        if vs and self.chance(0.75):
            n, _ = self.pick(vs)
            return Var(n, "int")
        return self.literal()

    def bool_leaf(self, depth=0):
        if self.chance(0.25):
            got = self.agg_read("bool", depth)
            if got is not None:
                return got
        vs = self.scope.vars("bool")
        if vs and self.chance(0.6):
            n, _ = self.pick(vs)
            return Var(n, "bool")
        return BLit(self.chance(0.5))

    # ---------------------------------------------------------------- strings
    # A small alphabet, so that searches, replaces and comparisons often
    # match; whitespace from trim's set (space, tab, newline) is included.
    ALPHA = "aabbab AZ09\t\n.-"

    def str_literal(self):
        n = self.pick([0, 1, 1, 2, 2, 3, 4, 5])
        return SLit("".join(self.pick(self.ALPHA) for _ in range(n)))

    def str_leaf(self, depth=0):
        if self.chance(0.25):
            got = self.agg_read("string", depth)
            if got is not None:
                return got
        vs = self.scope.vars("string")
        if vs and self.chance(0.65):
            return Var(self.pick(vs)[0], "string")
        return self.str_literal()

    def gen_str(self, depth):
        if depth <= 0:
            return self.str_leaf()
        k = self.rng.random()
        if k < 0.25:
            return self.str_leaf(depth)
        if k < 0.45:
            return Bin("+", self.gen_str(depth - 1), self.gen_str(depth - 1))
        if k < 0.52:
            if self.enums and self.chance(0.3):
                return EnumTo(self.gen_enum(self.pick(self.enums), depth - 1), "string")
            if self.use_numeric and self.chance(0.3):
                return Conv(self.gen_double(depth - 1), "string")
            return ToStr(self.gen_int(depth - 1))
        if k < 0.57:
            n = Lit(self.rng.randint(-1, 3))
            return SCall("repeat", [self.gen_str(depth - 1), n])
        if k < 0.70:
            return self.substring(depth)
        if k < 0.78:
            return SCall("replace", [self.gen_str(depth - 1), self.str_literal(),
                                     self.str_leaf()])
        if k < 0.92:
            fn = self.pick(["to_upper", "to_lower", "trim", "trim_start", "trim_end"])
            return SCall(fn, [self.gen_str(depth - 1)])
        return Give(self.gen_bool(depth - 1), self.gen_str(depth - 1), self.gen_str(depth - 1))

    def substring(self, depth):
        """strings::substring, panicking when the range is outside the string
        (start < 0, end < start or end > length). The base is a leaf because it
        is named twice: once to cut, once for its length."""
        b = self.str_leaf()
        if self.chance(self.opaque_p * 4):
            lo, hi = self.rng.randint(-1, 3), self.rng.randint(-1, 6)
            return SCall("substring", [b, Lit(lo), Lit(hi)])  # may panic
        k = self.rng.randint(0, 2)
        cut = SCall("substring", [b, Lit(k), SCall("length", [b])])
        return Give(Bin(">=", SCall("length", [b]), Lit(k)), cut, b)

    def string_int(self, depth):
        """An int computed from strings."""
        k = self.rng.random()
        if k < 0.35:
            return SCall("length", [self.gen_str(depth - 1)])
        if k < 0.55:
            fn = self.pick(["index_of", "last_index_of"])
            return SCall(fn, [self.gen_str(depth - 1), self.str_leaf()])
        if k < 0.70:
            ch = ord(self.pick(self.ALPHA))
            return SCall("count_char", [self.gen_str(depth - 1), Lit(ch)])
        # One byte: `% length` is in (-n, n), and a negative index wraps once
        # (operators.md), so the guarded read is always in range.
        b = self.str_leaf()
        if self.chance(self.opaque_p * 3):
            self.opaque_used = True
            return CharAt(b, Call("opaque", [self.gen_int(max(depth - 1, 0))]))
        idx = Bin("%", self.gen_int(max(depth - 1, 0)), SCall("length", [b]))
        return Give(Bin(">", SCall("length", [b]), Lit(0)), CharAt(b, idx), Lit(-1))

    def string_bool(self, depth):
        if self.chance(0.55):
            op = self.pick(["==", "!=", "<", ">", "<=", ">="])
            return Bin(op, self.gen_str(depth - 1), self.gen_str(depth - 1))
        fn = self.pick(["contains", "starts_with", "ends_with"])
        return SCall(fn, [self.gen_str(depth - 1), self.str_leaf()])

    # ------------------------------------------- Stack, Queue, Set, Map
    def colls(self, kinds=None):
        return [e for e in self.scope.entries()
                if isinstance(e.ty, CollT) and (kinds is None or e.ty.kind in kinds)]

    def coll_key(self, t, depth=1):
        """A key or member from a small domain, so that lookups often hit."""
        if t == "string":
            return self.str_literal() if self.chance(0.7) else self.str_expr(depth)
        return Bin("%", self.gen_int(depth), Lit(self.pick([4, 8, 8, 16])))

    def coll_stmt(self):
        cs = self.colls()
        if not cs:
            return None
        e = self.pick(cs)
        c, t = Var(e.name, e.ty), e.ty
        if e.kind == MUT and self.chance(0.08):
            others = [o for o in self.lists_of(t) if o.name != e.name]
            if others:
                o = Var(self.pick(others).name, t)
                return [Assign(c, o if self.chance(0.5) else CopyE(o))]
        nonempty = Bin(">", CMeth(c, "length", [], "int"), Lit(0))
        k = self.rng.random()
        if t.kind in ("Stack", "Queue"):
            put, take = ("push", "pop") if t.kind == "Stack" else ("enqueue", "dequeue")
            if k < 0.6:
                return [CStmt(c, put, [self.int_expr(2)])]
            name = self.new_name("v")
            if self.chance(self.opaque_p * 3):
                # May take from an empty one on purpose.
                self.scope.declare(name, "int", MUT)
                return [Decl(name, "int", CMeth(c, take, [], "int"))]
            self.scope.push()
            self.scope.declare(name, "int", MUT)
            body = [Decl(name, "int", CMeth(c, take, [], "int")), self.mix()]
            self.scope.pop()
            return [If([[nonempty, body, 0]], None)]
        if t.kind == "Set":
            if k < 0.65:
                return [CStmt(c, "add", [self.expr_ok(lambda: self.coll_key("int"))])]
            name = self.new_name("b")
            self.scope.declare(name, "bool", MUT)
            return [Decl(name, "bool",
                         CMeth(c, "remove", [self.expr_ok(lambda: self.coll_key("int"))], "bool"))]
        key = self.expr_ok(lambda: self.coll_key(t.key), t.key)
        if k < 0.45:
            return [CStmt(c, "put", [key, self.int_expr(2)])]
        if k < 0.75:
            return [Assign(CIndex(c, key), self.int_expr(2))]  # m[k] = v
        return [CStmt(c, "remove", [key])]

    def coll_int(self, depth):
        cs = self.colls()
        if not cs:
            return None
        e = self.pick(cs)
        c, t = Var(e.name, e.ty), e.ty
        length = CMeth(c, "length", [], "int")
        nonempty = Bin(">", length, Lit(0))
        k = self.rng.random()
        if k < 0.3:
            return length
        if t.kind in ("Stack", "Queue"):
            peek = CMeth(c, "peek", [], "int")
            return peek if self.chance(self.opaque_p * 3) else Give(nonempty, peek, Lit(0))
        if t.kind == "Set":
            if k < 0.6:
                return CMeth(c, "index_of", [self.coll_key("int", depth - 1)], "int")
            if self.chance(self.opaque_p * 3):
                return CIndex(c, self.gen_int(depth - 1))  # may be out of range
            n = length
            idx = Bin("%", Bin("+", Bin("%", self.gen_int(depth - 1), n), n), n)
            return Give(nonempty, CIndex(c, idx), Lit(0))  # a Set has no negative index
        key = self.coll_key(t.key, depth - 1)
        if k < 0.45:
            return CMeth(c, "index_of", [key], "int")
        if k < 0.65:
            return TryGet(c, key, self.gen_int(depth - 1))
        read = CIndex(c, key) if self.chance(0.5) else CMeth(c, "get", [key], "int")
        if self.chance(self.opaque_p * 3):
            return read  # may miss on purpose
        return Give(CMeth(c, "contains", [key], "bool"), read, Lit(0))

    def coll_bool(self, depth):
        cs = self.colls(("Set", "Map"))
        if not cs:
            return None
        e = self.pick(cs)
        return CMeth(Var(e.name, e.ty), "contains",
                     [self.coll_key(e.ty.key, depth - 1)], "bool")

    # --------------------------------------------------- double, int32, char
    DOUBLES = [0.5, 1.5, 2.0, 2.5, 0.1, 0.2, 3.0, 10.0, 100.0, 0.25, 1e-3,
               123.456, 7.0, 1e10, 1e16]
    DOUBLE_EDGES = [1e300, 1e308, -1e308, 5e-324, 9.2233720368547758e18,
                    -9.2233720368547758e18, 2147483647.5, 4.5e15, -0.0]

    def double_literal(self):
        if self.chance(self.boundary_p * 2):
            return DLit(self.pick(self.DOUBLE_EDGES))
        v = self.pick(self.DOUBLES)
        return DLit(-v if self.chance(0.3) else v)

    def num_leaf(self, t, depth, lit):
        if self.chance(0.25):
            got = self.agg_read(t, depth)
            if got is not None:
                return got
        vs = self.scope.vars(t)
        if vs and self.chance(0.65):
            return Var(self.pick(vs)[0], t)
        return lit()

    def gen_double(self, depth):
        """IEEE arithmetic: overflow is inf, never a trap; `/` and `%` by
        zero trap, so a divisor is a nonzero literal or `x * x + 1.0`."""
        if depth <= 0:
            return self.num_leaf("double", depth, self.double_literal)
        k = self.rng.random()
        if k < 0.3:
            return self.num_leaf("double", depth, self.double_literal)
        if k < 0.6:
            op = self.pick(["+", "-", "*", "*", "/", "%"])
            l = self.gen_double(depth - 1)
            if op in ("/", "%"):
                if self.chance(self.safe_div):
                    r = self.double_literal() if self.chance(0.5) else None
                    if r is None or r.v == 0.0:
                        x = self.gen_double(depth - 1)
                        r = Bin("+", Bin("*", x, x), DLit(1.0))
                else:
                    r = self.gen_double(depth - 1)  # may be zero: traps
                return Bin(op, l, r)
            return Bin(op, l, self.gen_double(depth - 1))
        if k < 0.7:
            return Un("-", self.gen_double(depth - 1))
        if k < 0.85:
            return Conv(self.gen_int(depth - 1), "double")
        if k < 0.9:
            return Conv(self.gen_i32(depth - 1), "double")
        return Give(self.gen_bool(depth - 1), self.gen_double(depth - 1),
                    self.gen_double(depth - 1))

    def gen_i32(self, depth):
        """int32 values come from `to int32` (truncating) or storage; int32
        arithmetic is int (probe c1.tr), so none is generated on int32."""
        if depth <= 0 or self.chance(0.4):
            return self.num_leaf("int32", depth,
                                 lambda: Conv(Lit(self.rng.randint(-100, 100)), "int32"))
        if self.chance(0.7):
            return Conv(self.gen_int(depth - 1), "int32")
        return Conv(self.gen_double(depth - 1), "int32")  # may trap

    CHARS = "abcAZ09"

    def gen_char(self, depth):
        if depth <= 0 or self.chance(0.5):
            return self.num_leaf("char", depth, lambda: CLit(self.pick(self.CHARS)))
        return Conv(self.gen_int(depth - 1), "char")  # truncates to a byte

    def numeric_int(self, depth):
        k = self.rng.random()
        if k < 0.45:
            return Conv(self.gen_double(depth - 1), "int")  # may trap
        if k < 0.75:
            return Conv(self.gen_i32(depth - 1), "int")
        return Conv(self.gen_char(depth - 1), "int")

    def numeric_bool(self, depth):
        op = self.pick(["==", "!=", "<", ">", "<=", ">="])
        if self.chance(0.7):
            return Bin(op, self.gen_double(depth - 1), self.gen_double(depth - 1))
        return Bin(op, self.gen_char(depth - 1), self.gen_char(depth - 1))

    # ------------------------------------------------------------------ enums
    def gen_enum(self, t, depth):
        k = self.rng.random()
        if depth <= 0 or k < 0.45:
            if self.chance(0.3):
                got = self.agg_read(t, depth)
                if got is not None:
                    return got
            vs = self.scope.vars(t)
            if vs and self.chance(0.6):
                return Var(self.pick(vs)[0], t)
            return EnumLit(t, self.rng.randrange(len(t.variants)))
        if k < 0.75:
            # ((e % n) + n) % n is in [0, n): a cast from a run-time int is
            # unchecked, so the generator never casts one out of range.
            n = Lit(len(t.variants))
            e = Bin("%", Bin("+", Bin("%", self.gen_int(depth - 1), n), n), n)
            return EnumOf(t, e)
        return Give(self.gen_bool(depth - 1), self.gen_enum(t, depth - 1),
                    self.gen_enum(t, depth - 1))

    def enum_int(self, depth):
        t = self.pick(self.enums)
        if self.chance(0.2):
            return EnumCount(t)
        return EnumTo(self.gen_enum(t, depth - 1), "int")

    def enum_bool(self, depth):
        t = self.pick(self.enums)
        return Bin(self.pick(["==", "!="]), self.gen_enum(t, depth - 1), self.gen_enum(t, depth - 1))

    def compare_stmt(self, depth):
        """compare over an enum (every variant covered, or an `otherwise`) or
        an int (literal patterns, `otherwise` optional). Distinct patterns,
        stacked headers, non-empty bodies (E058)."""
        if self.enums and self.chance(0.6):
            t = self.pick(self.enums)
            subject = self.gen_enum(t, 2)
            pool = [EnumLit(t, k) for k in range(len(t.variants))]
            exhaustive = self.chance(0.5)
        else:
            subject = self.int_expr(2)
            pool = [Lit(v) for v in self.rng.sample(range(-3, 6), self.rng.randint(1, 4))]
            exhaustive = False
        self.rng.shuffle(pool)
        used = pool if exhaustive else pool[:self.rng.randint(1, len(pool))]
        arms = []
        i = 0
        while i < len(used):
            n = self.rng.randint(1, min(2, len(used) - i))
            arms.append((used[i:i + n], self.gen_block(self.rng.randint(1, 2), depth + 1)))
            i += n
        els = None
        if not exhaustive and (isinstance(subject.ty, EnumT) and len(used) < len(pool)
                               or self.chance(0.5)):
            els = self.gen_block(self.rng.randint(1, 2), depth + 1)
        return Compare(subject, arms, els)

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

    def shift_amount(self, depth, left):
        if not self.tame and self.chance(0.15):
            return self.gen_int(depth - 1)  # may be out of [0, 64)
        if self.chance(0.5):
            return Lit(self.rng.randint(0, 8 if left and self.tame else 63))
        mask = 7 if left and self.tame else 63
        return Bin("&", self.gen_int(depth - 1), Lit(mask))  # in [0, mask]

    def gen_int(self, depth):
        r = self.rng
        if depth <= 0:
            return self.int_leaf()
        if self.use_strings and self.chance(0.08):
            return self.string_int(depth)
        if self.enums and self.chance(0.06):
            return self.enum_int(depth)
        if self.use_numeric and self.chance(0.08):
            return self.numeric_int(depth)
        if self.use_colls and self.chance(0.1):
            got = self.coll_int(depth)
            if got is not None:
                return got
        k = r.random()
        if k < 0.30:
            return self.int_leaf(depth)
        if k < 0.80:
            ops = ["+", "+", "-", "-", "*", "/", "%"]
            if self.bitwise:
                ops += ["&", "|", "^", "<<", ">>"]
            op = self.pick(ops)
            if op in ("/", "%"):
                return Bin(op, self.gen_int(depth - 1), self.divisor(depth))
            if op == "*":
                return Bin(op, self.shrink(self.gen_int(depth - 1)), self.shrink(self.gen_int(depth - 1)))
            if op in ("<<", ">>"):
                lhs = self.gen_int(depth - 1)
                if op == "<<":
                    lhs = self.shrink(lhs)
                return Bin(op, lhs, self.shift_amount(depth, op == "<<"))
            return Bin(op, self.gen_int(depth - 1), self.gen_int(depth - 1))
        if k < 0.86:
            if self.bitwise and self.chance(0.4):
                return Un("~", self.gen_int(depth - 1))
            return Un("-", self.gen_int(depth - 1))
        if k < 0.93:
            c = self.try_call(depth)
            if c is not None:
                return c
            return self.int_leaf(depth)
        return Give(self.gen_bool(depth - 1), self.gen_int(depth - 1), self.gen_int(depth - 1))

    def gen_bool(self, depth):
        r = self.rng
        if depth <= 0:
            return self.bool_leaf() if self.chance(0.4) else self.compare(0)
        if self.use_strings and self.chance(0.12):
            return self.string_bool(depth)
        if self.enums and self.chance(0.1):
            return self.enum_bool(depth)
        if self.use_numeric and self.chance(0.1):
            return self.numeric_bool(depth)
        if self.use_colls and self.chance(0.08):
            got = self.coll_bool(depth)
            if got is not None:
                return got
        k = r.random()
        if k < 0.15:
            return self.bool_leaf(depth)
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

    def gen_value(self, t, depth):
        """An expression of any non-List type t."""
        if t == "int":
            return self.gen_int(depth)
        if t == "bool":
            return self.gen_bool(depth)
        if t == "string":
            return self.gen_str(depth)
        if isinstance(t, EnumT):
            return self.gen_enum(t, depth)
        if t == "double":
            return self.gen_double(depth)
        if t == "int32":
            return self.gen_i32(depth)
        if t == "char":
            return self.gen_char(depth)
        if isinstance(t, StructT):
            if self.chance(0.6):
                got = self.place(t, "read", depth)
                if got is not None:
                    return got
            return Default(t)
        if isinstance(t, ArrT):
            if self.chance(0.4):
                got = self.place(t, "read", depth)
                if got is not None:
                    return got
            return ArrLit(t, [self.gen_value(t.elem, max(depth - 1, 0)) for _ in range(t.n)])
        if isinstance(t, SliceT):
            srcs = [e for e in self.scope.entries()
                    if isinstance(e.ty, (ArrT, SliceT)) and e.ty.elem == t.elem]
            if not srcs:
                return None
            e = self.pick(srcs)
            return CopyE(Var(e.name, e.ty))
        raise TypeError(t)

    def try_call(self, depth):
        if not self.funcs:
            return None
        callee = self.pick(self.funcs)
        spent = self.mult * (callee.cost + 1)
        if self.cost + spent > self.cost_cap:
            return None
        call = self.call_args(callee, depth)
        if call is None:
            return None
        self.cost += spent
        return self.handle_fallible(call, callee, depth)

    def handle_fallible(self, call, callee, depth):
        """A call as an int expression: a `!int` callee goes through `try`
        (only in a `!int` function, never in a defer or recover block: E095)
        or `recover produce` with a fallback evaluated only on failure."""
        if not callee.fallible:
            return call
        if (self.cur_fn is not None and self.cur_fn.fallible and not self.in_defer
                and not self.in_run and self.chance(0.4)):
            return Try(call)
        return RecoverP(call, self.gen_int(max(depth - 2, 0)))

    def call_args(self, callee, depth):
        """Call(callee, args), or None if an argument cannot be built. A
        recursive callee's first argument is its depth, a literal in 0..6."""
        args = []
        params = callee.params
        if callee.rec:
            args.append(Lit(self.rng.randint(0, 6)))
            params = params[1:]
        for _, t in params:
            if isinstance(t, SliceT):
                a = self.slice_arg(t)
            elif isinstance(t, ListT):
                a = self.list_arg(t)
            else:
                a = self.gen_value(t, max(depth - 2, 0))
            if a is None:
                return None
            args.append(a)
        return Call(callee.name, args)

    def lists_of(self, t, kinds=None):
        return [e for e in self.scope.entries()
                if e.ty == t and (kinds is None or e.kind in kinds)]

    def list_arg(self, t):
        """A List of type t in scope, shared with the callee, or now and then
        a `.copy()` of one, whose changes the caller never sees."""
        cands = self.lists_of(t)
        if not cands:
            return None
        e = self.pick(cands)
        v = Var(e.name, e.ty)
        return CopyE(v) if self.chance(0.2) else v

    def slice_arg(self, t):
        """A writable array (any length) or slice with t's element type."""
        cands = []
        for e in self.roots(True):
            for target in [SliceT(t.elem)] + [ArrT(t.elem, n) for n in range(1, 6)]:
                if reaches(e.ty, target):
                    cands.append((e, target))
        if not cands:
            return None
        e, target = self.pick(cands)
        root = Var(e.name, e.ty)
        if e.ty == target:
            return root
        return self.descend_strict(root, e.ty, target, 1)

    def expr_ok(self, make, ty="int"):
        """Build an expression with `make`; retry until no literal-only
        subtree folds out of range."""
        for _ in range(40):
            e = make()
            if e is None:
                continue
            try:
                check_foldable(e)
                return e
            except Overflow:
                continue
        if ty == "int":
            return Lit(self.rng.randint(-9, 9))
        if ty == "bool":
            return BLit(True)
        if ty == "string":
            return SLit("")
        if isinstance(ty, EnumT) or ty in ("double", "int32", "char"):
            return self.zero_of(ty)
        return None

    def int_expr(self, depth=3):
        return self.expr_ok(lambda: self.gen_int(depth), "int")

    def bool_expr(self, depth=3):
        return self.expr_ok(lambda: self.gen_bool(depth), "bool")

    def str_expr(self, depth=2):
        return self.expr_ok(lambda: self.gen_str(depth), "string")

    def value_expr(self, t, depth=2):
        if t == "int":
            return self.int_expr(depth + 1)
        if t == "bool":
            return self.bool_expr(depth + 1)
        if t == "string":
            return self.str_expr(depth)
        return self.expr_ok(lambda: self.gen_value(t, depth), t)

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
        if self.in_method:
            return self.print_stmt()
        e = self.int_expr(3)
        inner = Bin("+", Bin("*", Var("chk", "int"), Lit(31)), Bin("%", e, Lit(1000003)))
        return Assign(Var("chk", "int"), Bin("%", inner, Lit(1000000007)))

    def has_aggs(self):
        return self.use_structs or self.use_arrays or self.use_lists or self.use_colls

    def gen_stmt(self, depth):
        r = self.rng
        self.spend()
        if self.cost > self.cost_cap * 2:
            return [self.mix()]
        if self.has_aggs() and self.chance(0.35):
            got = self.agg_stmt(depth)
            if got:
                return got
        if self.use_defer and not self.in_defer and self.chance(0.06):
            got = self.defer_stmt()
            if got:
                return [got]
        if self.use_errors and not self.in_method and not self.in_run and self.chance(0.06):
            got = self.recover_run(depth)
            if got:
                return got
        if depth < 3 and self.chance(0.05 if self.enums else 0.02):
            return [self.compare_stmt(depth)]
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
        elif k < 0.78:
            if depth < 3:
                return [self.gen_if(depth)]
        elif k < 0.86:
            if depth < 2:
                return [self.gen_while(depth)]
        elif k < 0.89:
            if depth < 2:
                return [self.gen_for_range(depth)]
        elif k < 0.93:
            if self.in_loop:
                return [If([[self.bool_expr(2), [self.pick([Break, Continue])()], 0]], None)]
        elif k < 0.96:
            if self.in_func and not self.in_method:
                return [If([[self.bool_expr(2), [self.return_stmt()], 0]], None)]
        return [self.mix()]

    def return_stmt(self):
        """An early return from a helper: in a `!int` one, half the time an
        error, now and then with a message (`E0::A("...")`)."""
        if self.cur_fn is not None and self.cur_fn.fallible and self.chance(0.5):
            return Return(self.err_lit())
        return Return(self.int_expr(2))

    def err_lit(self):
        name, variants = self.errors[0]
        msg = None
        if self.use_strings and self.chance(0.4):
            msg = self.str_expr(1)
        return ErrLit(name, self.pick(variants), msg)

    def defer_stmt(self):
        """defer <print | chk mix | scalar assignment>; never a jump (rejected)
        and never a `try` (E095)."""
        self.in_defer = True
        try:
            k = self.rng.random()
            if k < 0.5:
                inner = self.print_stmt()
            elif k < 0.75:
                inner = self.mix()
            else:
                inner = self.gen_assign()
        finally:
            self.in_defer = False
        return Defer(inner) if inner is not None else None

    def recover_run(self, depth):
        """mut v: int = f(...) recover as err run { ...; if err == E0::X { jump }
        jump };  The block must diverge (int success type): `return`, or
        `break`/`continue` in a loop."""
        fallible = [f for f in self.funcs if f.fallible]
        if not fallible:
            return None
        callee = self.pick(fallible)
        spent = self.mult * (callee.cost + 1)
        if self.cost + spent > self.cost_cap:
            return None
        # Through expr_ok like every expression: a literal-only argument
        # that folds out of int64 is E076 (seed 11388).
        call = self.expr_ok(lambda: self.call_args(callee, depth), None)
        if call is None:
            return None
        self.cost += spent
        # A third of the time the plain form, `recover run { ... }`: no binding,
        # so nothing in the block can ask which error it was.
        err = None if self.chance(0.33) else self.new_name("err")
        name_, variants = self.errors[0]
        self.in_run = True
        self.scope.push()
        try:
            body = []
            if err is not None and self.use_strings and self.chance(0.4):
                body.append(Print(ErrMsg(err)))
            if self.chance(0.6):
                body.append(self.mix() if self.chance(0.5) else self.print_stmt())
            if err is not None:
                body.append(If([[ErrIs(err, name_, self.pick(variants)), [self.jump()], 0]], None))
            elif self.chance(0.4):
                body.append(If([[self.bool_expr(2), [self.jump()], 0]], None))
            body.append(self.jump())
        finally:
            self.scope.pop()
            self.in_run = False
        name = self.new_name("v")
        self.scope.declare(name, "int", MUT)
        return [RecoverRun(name, call, err, body)]

    def jump(self):
        if self.in_loop and self.chance(0.35):
            return self.pick([Break, Continue])()
        if self.cur_fn is not None and self.cur_fn.fallible and self.chance(0.3):
            return Return(self.err_lit())
        return Return(self.int_expr(2))

    def agg_stmt(self, depth):
        if self.use_colls and self.chance(0.3):
            got = self.coll_stmt()
            if got:
                return got
        k = self.rng.random()
        if k < 0.25:
            return self.agg_decl()
        if k < 0.45:
            s = self.agg_assign()
            return [s] if s else None
        if k < 0.60:
            s = self.method_call()
            return [s] if s else None
        if k < 0.80:
            return self.list_stmt()
        if k < 0.92:
            if depth < 2:
                s = self.gen_for_each(depth)
                return [s] if s else None
            return None
        s = self.slice_decl()
        return [s] if s else None

    def print_stmt(self):
        if self.use_strings and self.chance(0.3):
            return Print(self.str_expr(3))
        if self.use_numeric and self.chance(0.25):
            return Print(self.value_expr("double", 3))
        if self.chance(0.75):
            return Print(self.int_expr(3))
        return Print(self.bool_expr(3))

    def scalar_expr(self, ty, depth=3):
        if ty == "string":
            return self.str_expr(depth)
        if isinstance(ty, EnumT) or ty in ("double", "int32", "char"):
            return self.expr_ok(lambda: self.gen_value(ty, depth - 1), ty)
        return self.int_expr(depth) if ty == "int" else self.bool_expr(depth)

    def gen_decl(self):
        ty = self.scalar_type()
        e = self.scalar_expr(ty)
        kind = MUT
        if self.chance(0.2) and const_value_safe(e) is None:
            kind = IMMUT  # immut with a non-constant initializer
        name = self.new_name("v")
        self.scope.declare(name, ty, kind)
        return Decl(name, ty, e, kind == MUT)

    def agg_decl(self):
        t = self.agg_type()
        if t is None:
            return None
        if isinstance(t, CollT):
            # New, another name for one in scope, or a `.copy()` (F-COLL-1).
            init = CollNew(t)
            srcs = self.lists_of(t)
            if srcs and self.chance(0.4):
                e = self.pick(srcs)
                v = Var(e.name, e.ty)
                init = v if self.chance(0.5) else CopyE(v)
            name = self.new_name("c")
            self.scope.declare(name, t, MUT)
            return [Decl(name, t, init)]
        if isinstance(t, ListT):
            # A new list, another name for one in scope (`=` shares,
            # LEC-009), or an independent `.copy()` of one.
            init = ListNew(t)
            srcs = self.lists_of(t)
            if srcs and self.chance(0.4):
                e = self.pick(srcs)
                v = Var(e.name, e.ty)
                init = v if self.chance(0.5) else CopyE(v)
            # A Map's keys() / values(): a fresh List in the map's dense order.
            maps = [m for m in self.scope.entries()
                    if isinstance(m.ty, CollT) and m.ty.kind == "Map"
                    and t.elem in (m.ty.key, m.ty.val)]
            if maps and self.chance(0.5):
                m = self.pick(maps)
                which = [n for n, kt in (("keys", m.ty.key), ("values", m.ty.val)) if kt == t.elem]
                init = CMeth(Var(m.name, m.ty), self.pick(which), [], t)
            name = self.new_name("l")
            self.scope.declare(name, t, MUT)
            return [Decl(name, t, init)]
        if isinstance(t, ArrT) and not contains_string(t) and self.chance(0.2):
            # `mut a: int[3];` is zero-filled, but definite assignment (F-INIT-1)
            # counts the whole array unassigned until one element is written.
            # Not with strings: an unwritten `string` element is a null
            # pointer, not "" (open turflang issue, SEMANTICS.md).
            first = self.value_expr(t.elem, 2)
            if first is None:
                return None
            name = self.new_name("a")
            self.scope.declare(name, t, MUT)
            k = Lit(self.rng.randint(-t.n, t.n - 1))
            return [Decl(name, t, None), Assign(Index(Var(name, t), k), first)]
        e = self.value_expr(t, 2)
        if e is None:
            return None
        name = self.new_name("s" if isinstance(t, StructT) else "a")
        self.scope.declare(name, t, MUT)
        return [Decl(name, t, e)]

    def slice_decl(self):
        if not self.use_arrays:
            return None
        srcs = [e for e in self.scope.entries() if isinstance(e.ty, (ArrT, SliceT))]
        if not srcs:
            return None
        e = self.pick(srcs)
        t = SliceT(e.ty.elem)
        name = self.new_name("w")
        self.scope.declare(name, t, VIEW)
        return Decl(name, t, CopyE(Var(e.name, e.ty)))

    def gen_assign(self):
        vs = self.scope.vars(None, True)
        vs = [(n, t) for n, t in vs if is_scalar(t)]
        if not vs:
            return None
        name, ty = self.pick(vs)
        return Assign(Var(name, ty), self.scalar_expr(ty))

    def agg_assign(self):
        """Assignment into a field or element, or of a whole aggregate."""
        k = self.rng.random()
        target = "int" if k < 0.45 else "bool" if k < 0.58 else None
        if target is None and self.use_strings and k < 0.68:
            target = "string"
        if target is None:
            types = [e.ty for e in self.roots(True)
                     if isinstance(e.ty, (StructT, ArrT))]
            sub = []
            for t in types:
                sub.extend(self._subtypes(t))
            if not sub:
                return None
            target = self.pick(sub)
        p = self.place(target, "assign")
        if p is None:
            return None
        e = self.value_expr(target, 2)
        if e is None:
            return None
        return Assign(p, e)

    def _subtypes(self, t):
        out = [t]
        if isinstance(t, StructT):
            for _, ft in t.fields:
                if not is_scalar(ft):
                    out.extend(self._subtypes(ft))
        elif isinstance(t, ArrT) and not is_scalar(t.elem):
            out.extend(self._subtypes(t.elem))
        return out

    def comp_rhs(self, op):
        if op in ("/", "%"):
            return self.expr_ok(lambda: self.divisor(2))
        if op == "*":
            e = self.expr_ok(lambda: self.shrink(self.gen_int(2)))
            return Bin("%", e, Lit(10)) if self.tame else e
        if op in ("<<", ">>"):
            return self.expr_ok(lambda: self.shift_amount(2, op == "<<"))
        return self.int_expr(2)

    def comp_op(self):
        ops = ["+", "-", "*", "/", "%"]
        if self.bitwise:
            ops += ["&", "|", "^", "<<", ">>"]
        return self.pick(ops)

    def gen_comp(self):
        if self.use_strings and self.chance(0.2):
            # `s += t` desugars to `s = s + t` (strings.tr `operator +`).
            p = self.place("string", "assign") if self.has_aggs() else None
            if p is None:
                vs = self.scope.vars("string", True)
                p = Var(self.pick(vs)[0], "string") if vs else None
            if p is not None:
                return Comp(p, "+", self.str_expr(1))
        if self.use_numeric and self.chance(0.2):
            vs = self.scope.vars("double", True)
            if vs:
                op = self.pick(["+", "-", "*", "/", "%"])
                rhs = self.value_expr("double", 1)
                if op in ("/", "%") and self.chance(self.safe_div):
                    rhs = Bin("+", Bin("*", rhs, rhs), DLit(1.0))
                return Comp(Var(self.pick(vs)[0], "double"), op, rhs)
        if self.has_aggs() and self.chance(0.3):
            p = self.place("int", "assign")
        else:
            vs = self.scope.vars("int", True)
            p = Var(self.pick(vs)[0], "int") if vs else None
        if p is None:
            return None
        op = self.comp_op()
        if op == "<<" and self.tame:
            return Comp(p, "+", self.int_expr(2))  # p may be large: avoid a near-certain trap
        return Comp(p, op, self.comp_rhs(op))

    def method_call(self):
        cands = []
        for st in self.structs:
            if st.methods:
                cands.append(st)
        if not cands:
            return None
        st = self.pick(cands)
        recv = self.place(st, "mutate")
        if recv is None:
            return None
        m = self.pick(st.methods)
        spent = self.mult * (m.cost + 1)
        if self.cost + spent > self.cost_cap:
            return None
        self.cost += spent
        args = [self.value_expr(t, 1) for _, t in m.params]
        return MethodCall(recv, m.name, args)

    def list_stmt(self):
        ls = [e for e in self.scope.entries() if isinstance(e.ty, ListT)]
        if not ls:
            return None
        e = self.pick(ls)
        lv = Var(e.name, e.ty)
        k = self.rng.random()
        guard = Bin(">", Length(lv), Lit(0))
        if e.kind == MUT and self.chance(0.1):
            # Rebind a local to another list (shared) or a copy of one.
            others = [o for o in self.lists_of(e.ty) if o.name != e.name]
            if others:
                o = self.pick(others)
                ov = Var(o.name, o.ty)
                return [Assign(lv, ov if self.chance(0.5) else CopyE(ov))]
        if k < 0.55:
            return [Push(lv, self.value_expr(e.ty.elem, 2))]
        if k < 0.80:
            if self.chance(self.opaque_p):
                self.opaque_used = True
                return [Assign(Index(lv, Call("opaque", [self.int_expr(2)])),
                               self.value_expr(e.ty.elem, 2))]
            idx = Bin("%", self.int_expr(2), Length(lv))
            return [If([[guard, [Assign(Index(lv, idx), self.value_expr(e.ty.elem, 2))], 0]], None)]
        # pop into a variable of the element type, or into chk's mix
        t = e.ty.elem
        if is_scalar(t):
            vs = self.scope.vars(t, True)
            if vs:
                tgt = Var(self.pick(vs)[0], t)
                s = Assign(tgt, Pop(lv))
                if self.chance(1 - self.opaque_p * 3):
                    return [If([[guard, [s], 0]], None)]
                return [s]  # may pop an empty list on purpose
        name = self.new_name("v" if is_scalar(t) else "s")
        self.scope.push()
        self.scope.declare(name, t, MUT)
        body = [Decl(name, t, Pop(lv))]
        if t == "int":
            body.append(self.mix() if not self.in_method else Print(Var(name, t)))
        self.scope.pop()
        return [If([[guard, body, 0]], None)]

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
        c = self.new_name("c")
        self.scope.declare(c, "int", LOOP)
        self.in_loop += 1
        body = self.gen_block(self.rng.randint(1, 4), depth + 1)
        self.in_loop -= 1
        self.mult = saved
        return While(c, bound, self.rng.randint(0, 2), body)

    def gen_for_range(self, depth):
        lo = self.rng.randint(-3, 3)
        hi = lo + self.rng.randint(0, 8)
        saved = self.mult
        self.mult *= max(hi - lo, 1)
        name = self.new_name("r")
        self.scope.push()
        self.scope.declare(name, "int", LOOP)
        self.in_loop += 1
        body = self.gen_block(self.rng.randint(1, 4), depth + 1)
        self.in_loop -= 1
        self.scope.pop()
        self.mult = saved
        return ForRange(name, lo, hi, body)

    def gen_for_each(self, depth):
        srcs = [e for e in self.scope.entries() if isinstance(e.ty, (ArrT, SliceT))]
        if not srcs:
            return None
        e = self.pick(srcs)
        n = e.ty.n if isinstance(e.ty, ArrT) else 6
        saved = self.mult
        self.mult *= max(n, 1)
        var = self.new_name("e")
        ivar = self.new_name("i") if self.chance(0.4) else None
        self.scope.push()
        self.scope.declare(var, e.ty.elem, LOOP)
        if ivar:
            self.scope.declare(ivar, "int", LOOP)
        self.in_loop += 1
        body = self.gen_block(self.rng.randint(1, 3), depth + 1)
        self.in_loop -= 1
        self.scope.pop()
        self.mult = saved
        return ForEach(ivar, var, Var(e.name, e.ty), body)

    # ------------------------------------------------------------- functions
    def reset_fn(self, cap):
        self.scope = Scope()
        self.names = {}
        self.cost = 0
        self.mult = 1
        self.cost_cap = cap
        self.in_loop = 0

    def gen_method(self, st, idx):
        self.reset_fn(300)
        self.in_func = True
        self.in_method = True
        params = []
        for i in range(self.rng.randint(0, 2)):
            params.append(("p%d" % i, "int" if self.chance(0.8) else "bool"))
        for n, t in params:
            self.scope.declare(n, t, PARAM)
        self.scope.declare("self", st, SELF)
        body = self.gen_block(self.rng.randint(1, 4), 1)
        m = Method(st, "m%d" % idx, params, body)
        m.cost = self.cost + 2
        self.in_method = False
        return m

    def param_type(self):
        k = self.rng.random()
        if k < 0.6 or not self.has_aggs():
            return "int"
        if k < 0.7:
            return "bool"
        if k < 0.82 and self.use_structs and self.structs:
            return self.pick(self.structs)
        if k < 0.9 and self.use_arrays:
            return ArrT("int", self.rng.randint(1, 4))
        if k < 0.95 and self.use_lists:
            # Shared with the caller (LEC-009): the callee's push/pop/[]=
            # are visible after the call.
            return ListT("int")
        if self.use_arrays:
            return SliceT("int")
        return "int"

    # The deepest a recursive helper is called (call_args): its cost is its
    # body's cost times this many levels.
    REC_LEVELS = 7

    def gen_func(self, idx):
        self.reset_fn(1500)
        self.in_func = True
        fallible = self.use_errors and self.chance(0.5)
        rec = self.use_rec and self.chance(0.4)
        params = [("d", "int")] if rec else []
        for i in range(self.rng.randint(0, 3)):
            params.append(("p%d" % i, self.param_type()))
        f = Func("f%d" % idx, params, [], fallible, rec)
        self.cur_fn = f
        for n, t in params:
            self.scope.declare(n, t, SLICE_PARAM if isinstance(t, SliceT) else PARAM)
        self.scope.declare("chk", "int", MUT)
        body = [Decl("chk", "int", Lit(0), True)]
        if rec:
            # Base case first, so every level does at most one body's work.
            body.append(If([[Bin("<=", Var("d", "int"), Lit(0)), [self.return_stmt()], 0]], None))
        body.extend(self.initial_vars())
        body.extend(self.gen_block(self.rng.randint(1, 6), 0))
        if rec:
            body.append(self.self_call(f))
        body.append(Return(self.int_expr(3)))
        f.body = body
        f.cost = self.cost + 2
        if rec:
            f.cost *= self.REC_LEVELS
        self.cur_fn = None
        return f

    def self_call(self, f):
        """chk = mix(chk, f(d - 1, ...)): one recursive call per level, at the
        top of the body (never in a loop), so the work is linear in the depth.
        An aggregate parameter is passed on as it is."""
        args = [Bin("-", Var("d", "int"), Lit(1))]
        for n, t in f.params[1:]:
            args.append(Var(n, t) if not is_scalar(t) else self.value_expr(t, 1))
        call = Call(f.name, args)
        e = self.handle_fallible(call, f, 2)
        inner = Bin("+", Bin("*", Var("chk", "int"), Lit(31)), Bin("%", e, Lit(1000003)))
        return Assign(Var("chk", "int"), Bin("%", inner, Lit(1000000007)))

    def initial_vars(self):
        out = []
        for _ in range(self.rng.randint(2, 4)):
            ty = self.scalar_type()
            if isinstance(ty, EnumT):
                e = EnumLit(ty, self.rng.randrange(len(ty.variants)))
            elif ty in ("double", "int32", "char"):
                e = self.gen_value(ty, 0)
            else:
                e = (self.literal() if ty == "int" else BLit(self.chance(0.5))
                     if ty == "bool" else self.str_literal())
            name = self.new_name("v")
            self.scope.declare(name, ty, MUT)
            out.append(Decl(name, ty, e, True))
        if self.has_aggs():
            for _ in range(self.rng.randint(1, 2)):
                out.extend(self.agg_decl() or [])
        return out

    def gen_main(self):
        self.reset_fn(4000)
        self.in_func = False
        self.scope.declare("chk", "int", MUT)
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
        if self.use_structs:
            self.gen_structs()
            for st in self.structs:
                for i in range(self.rng.choice([0, 1, 1, 2])):
                    st.methods.append(self.gen_method(st, i))
        for i in range(self.rng.randint(0, 4)):
            self.funcs.append(self.gen_func(i))
        main = self.gen_main()
        funcs = list(self.funcs)
        if self.opaque_used:
            funcs.insert(0, Func("opaque", [("x", "int")], [Return(Var("x", "int"))]))
        return Program(self.structs, funcs, main, self.seed, self.paren_mode, self.errors,
                       self.enums)


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
                return prog, None, attempt
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
    ap.add_argument("--allow-undefined", action="store_true",
                    help="keep programs whose result the language leaves undefined")
    a = ap.parse_args()
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
