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
    Set and Map locals (shared, copied, emptied, missed); user generics
    (`fn<T>` helpers and a generic struct with methods, at several types);
  * reference graphs: node structs with `&?Self` fields built with heap()
    (lists, trees, rings), `&S`/`&?S` locals, parameters, struct fields and
    List elements, null checks in every form the compiler narrows, bounded
    walks, recursive walkers, node builders, `&local` lent to a callee;
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
                  CollNew, CMeth, CIndex, TryGet, CStmt, GCall, GGet, GPut,
                  RefT, Null, Heap, AddrOf, RefCast, IsNull, has_nonnull_ref,
                  ErrArm, ErrCompare)
import evaluate as ev  # noqa: E402

BOUNDARY = [INT_MAX, INT_MIN, INT_MAX - 1, INT_MIN + 1, 2147483647, -2147483648,
            4294967296, 3037000500, 3037000499, -1, 0, 1, 2, -2]

# Scope entry kinds. Whole assignment only to MUT; element/field writes
# through MUT, SELF, VIEW and SLICE_PARAM; everything else is read-only.
#
# Two more kinds, for references. NARROWED: a `&?S` variable inside its own
# null check, seen there as a `&S`; the variable itself is hidden for as long,
# so nothing assigns it and the check stays true (SEMANTICS.md, References).
# BORROW: a `&S` parameter that may point into the caller's frame, so the
# body reads and writes through it but never stores, returns or passes on
# the reference itself except to another BORROW parameter.
#
# BIND: a payload field bound by an error pattern. Immutable all the way
# through: no field write, mutating method, push or `[]=` on it, even when it
# is a reference or a List (E060). It may be read, passed and copied to a
# `mut` variable, which may then write (SEMANTICS.md, Error payloads).
MUT, IMMUT, PARAM, SELF, VIEW, SLICE_PARAM, LOOP, NARROWED, BORROW, BIND = range(10)
WRITABLE = (MUT, SELF, VIEW, SLICE_PARAM)


def is_ref(t, nullable=None):
    return isinstance(t, RefT) and (nullable is None or t.nullable == nullable)


class Ent:
    def __init__(self, name, ty, kind):
        self.name, self.ty, self.kind = name, ty, kind


class Scope:
    """Lexical scopes of the function being generated."""

    def __init__(self):
        self.frames = [[]]
        self.hidden = [[]]  # per frame: names narrowed there

    def push(self):
        self.frames.append([])
        self.hidden.append([])

    def pop(self):
        self.frames.pop()
        self.hidden.pop()

    def declare(self, name, ty, kind):
        self.frames[-1].append(Ent(name, ty, kind))

    def narrow(self, name, st):
        """From here to the end of the innermost frame, `name` (a `&?st`
        variable) is a `&st` that nothing may assign."""
        self.hidden[-1].append(name)
        self.frames[-1].append(Ent(name, RefT(st, False), NARROWED))

    def entries(self):
        hidden = {n for h in self.hidden for n in h}
        for fr in self.frames:
            for e in fr:
                if e.name == "chk":  # only the checksum statements touch chk
                    continue
                if e.name in hidden and e.kind != NARROWED:
                    continue
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
    if isinstance(t, RefT):
        # Through a `&S` to S's fields (never to S itself: a struct cannot be
        # read by value out of a reference). A `&?S` needs a null check on a
        # variable first, so nothing is reached through one. `&S` fields only
        # point at earlier types, so this terminates.
        return (not t.nullable
                and any(reaches(ft, target, through_lists) for _, ft in t.st.fields))
    if isinstance(t, (ArrT, SliceT)):
        return reaches(t.elem, target, through_lists)
    if isinstance(t, ListT) and through_lists:
        return reaches(t.elem, target, through_lists)
    return False


# ------------------------------------------------------- compile-time folding

class Overflow(Exception):
    pass


def arms_has(arms, etype, variant):
    return any(a.etype == etype and a.variant == variant for a in arms)


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
    if isinstance(e, (ErrIs, ErrMsg, EnumLit, EnumCount, DLit, CLit, CollNew, Null)):
        return
    if isinstance(e, (Heap, RefCast, IsNull)):
        check_foldable(e.e)
        return
    if isinstance(e, AddrOf):
        check_foldable(e.place)
        return
    if isinstance(e, (CMeth, GCall)):
        for a in e.args:
            check_foldable(a)
        return
    if isinstance(e, GGet):
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
        for a in e.args:
            check_foldable(a)
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
        # User generics: `fn<T> gN(c: int, a: T, b: T): T` helpers and one
        # generic struct `Gb0<T>`, each instantiated at several types.
        self.use_generics = r.random() < 0.35
        # Reference graphs: node structs with `&?Self` fields, built with
        # heap(), walked under null checks, tied into cycles, held in structs
        # and Lists (SEMANTICS.md, References).
        self.use_refs = r.random() < 0.4
        self.nodes = []        # node StructTs (also in self.structs)
        self.builders = []     # `fn nbK(v: int, a0: &?N, ...): &N` Funcs
        self.gfuncs = []
        self.ginsts = {}       # element type -> the StructT for Gb0<that>
        self.gstruct_used = False
        self.errors = []
        if self.use_errors:
            self.errors = [("E0", ["A", "B", "C"][:r.randint(1, 3)])]
        self.perr = None       # ("E1", [(variant, [(field, type)])]): payloads
        self.sweep = False     # generating the body of a payload sweep
        self.in_walk = False   # generating a walk body (no defer there)
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

    def gen_nodes(self):
        """Node structs for reference graphs: `val: int`, up to two plain
        fields, and 1-3 `&?Self` fields (one makes a list, more a tree or any
        graph, cycles included). A second node type may also point at the
        first, by `&?N0` and by a never-null `&N0`. Half the time a holder
        struct follows, with references among plain fields. Fields are
        shuffled, so pointer words sit at varying offsets for the collector."""
        plain = list(self.structs)
        for i in range(self.rng.randint(1, 2)):
            st = StructT("N%d" % i, [("val", "int")])
            for j in range(self.rng.randint(0, 2)):
                k = self.rng.random()
                if k < 0.5 or not plain:
                    t = self.elem_type()
                elif k < 0.75:
                    t = ArrT(self.elem_type(), self.rng.randint(1, 3))
                else:
                    t = self.pick(plain)
                st.fields.append(("x%d" % j, t))
            for j in range(self.pick([1, 1, 2, 2, 3])):
                st.fields.append(("r%d" % j, RefT(st, True)))
            if self.nodes:
                if self.chance(0.6):
                    st.fields.append(("o0", RefT(self.nodes[0], True)))
                if self.chance(0.4):
                    st.fields.append(("o1", RefT(self.nodes[0], False)))
            self.rng.shuffle(st.fields)
            self.nodes.append(st)
            self.structs.append(st)
        if self.chance(0.5):
            n = self.pick(self.nodes)
            fields = [("k", "int"), ("head", RefT(n, True))]
            if self.chance(0.5):
                fields.append(("own", RefT(n, False)))
            if self.chance(0.4):
                fields.append(("inner", n))
            if self.chance(0.3):
                fields.append(("arr", ArrT(n, self.rng.randint(1, 2))))
            self.rng.shuffle(fields)
            self.structs.append(StructT("H0", fields))

    def gen_builder(self, st, idx):
        """fn nbK(v: int, a0: &?N, ...): &N -- a new node on the heap with
        `val` and each `&?Self` field set from the arguments. heap() copies
        the local `n`, which is about to go out of scope."""
        refs = [(f, t) for f, t in st.fields if is_ref(t, True) and t.st is st]
        params = [("v", "int")] + [("a%d" % i, t) for i, (_, t) in enumerate(refs)]
        n = Var("n", st)
        body = [Decl("n", st, Default(st)), Assign(Field(n, "val"), Var("v", "int"))]
        for i, (f, t) in enumerate(refs):
            body.append(Assign(Field(n, f), Var("a%d" % i, t)))
        body.append(Return(Heap(n)))
        f = Func("nb%d" % idx, params, body)
        f.ret = RefT(st, False)
        f.cost = len(body) + 2
        return f

    def self_refs(self, st):
        """Names of st's `&?Self` fields: the edges a walk can follow."""
        return [f for f, t in st.fields if is_ref(t, True) and t.st is st]

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
        if self.structs:
            opts += ["struct"] * 3
            if self.use_arrays:
                opts += ["sarr"]
        if self.use_lists and not self.in_method:
            opts += ["list"]
            if self.nodes:
                opts += ["rlist"]
        if self.use_colls and not self.in_method:
            opts += ["coll", "coll"]
        if self.use_generics and not self.in_method:
            opts += ["gstruct", "gstruct"]
        if not opts:
            return None
        k = self.pick(opts)
        if k == "rlist":
            # References in a collection: the List's buffer is the only thing
            # keeping some nodes alive. A third of the time `&?N`, with nulls.
            return ListT(RefT(self.pick(self.nodes), self.chance(0.3)))
        if k == "gstruct":
            return self.ginst(self.scalar_type())
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
            # A `&S` of any kind is written through: the storage is not the
            # variable's own. Except a pattern binding, immutable throughout.
            if writable and (e.kind == BIND
                             or (e.kind not in WRITABLE and not is_ref(e.ty, False))):
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
        if is_ref(t, False):
            t = t.st
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
        # `self` is a pointer: read its fields, not it. A narrowed variable is
        # still a `&?S` as a whole value (ref_vars() casts it), and a borrowed
        # reference is never copied anywhere.
        return e.kind not in (SELF, NARROWED, BORROW)

    def descend_strict(self, root, t, target, depth):
        if is_ref(t, False):
            t = t.st  # fields are reached through a `&S` with no extra syntax
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
        if self.use_generics and self.chance(0.07):
            got = self.generic_expr("string", depth)
            if got is not None:
                return got
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

    # ---------------------------------------------------- user generics
    def ginst(self, t):
        """The StructT for `Gb0<t>` (one per element type): fields v: t and
        n: int. Kept out of self.structs, so it is only ever a local: never a
        field, an element or a parameter, where `Gb0::default()` would have no
        declared type to take its argument from."""
        key = repr(t)
        if key not in self.ginsts:
            self.ginsts[key] = StructT("Gb0<%s>" % tast.ty_str(t), [("v", t), ("n", "int")],
                                       base="Gb0")
        self.gstruct_used = True
        return self.ginsts[key]

    def generic_expr(self, t, depth):
        """A value of scalar type t through a generic: `gN(c, a, b)`, which
        instantiates gN at t, or `x.get()` on a `Gb0<t>` local. None if
        neither is available."""
        boxes = [e for e in self.scope.entries()
                 if isinstance(e.ty, StructT) and e.ty.base and e.ty.field_type("v") == t]
        if boxes and (not self.gfuncs or self.chance(0.4)):
            e = self.pick(boxes)
            return GGet(Var(e.name, e.ty))
        if not self.gfuncs:
            return None
        g = self.pick(self.gfuncs)
        spent = self.mult * (g.cost + 1)
        if self.cost + spent > self.cost_cap:
            return None
        a = self.gen_value(t, max(depth - 1, 0))
        b = self.gen_value(t, max(depth - 1, 0))
        if a is None or b is None:
            return None
        self.cost += spent
        return GCall(g.name, [self.gen_int(max(depth - 1, 0)), a, b], t)

    def gput_stmt(self):
        boxes = [e for e in self.scope.entries()
                 if isinstance(e.ty, StructT) and e.ty.base and e.kind == MUT]
        if not boxes:
            return None
        e = self.pick(boxes)
        v = self.value_expr(e.ty.field_type("v"), 2)
        return [GPut(Var(e.name, e.ty), v)] if v is not None else None

    def gen_gfunc(self, idx):
        """fn<T> gN(c: int, a: T, b: T): T -- a body over ints only (T has no
        operations), returning a or b. Generated before the plain helpers, so
        it calls none of them and they may all call it."""
        self.reset_fn(400)
        self.in_func = True
        f = Func("g%d" % idx, [("c", "int"), ("a", "T"), ("b", "T")], [])
        f.generic = True
        self.cur_fn = f
        self.scope.declare("c", "int", PARAM)
        self.scope.declare("chk", "int", MUT)
        body = [Decl("chk", "int", Lit(0), True)]
        body.extend(self.gen_block(self.rng.randint(1, 4), 1))
        body.append(If([[self.bool_expr(2), [Return(Var("a", "T"))], 0]], None))
        body.append(Return(Var("b", "T")))
        f.body = body
        f.cost = self.cost + 2
        self.cur_fn = None
        return f

    # ------------------------------------------------------- references
    def ref_vars(self, t):
        """Variables usable as a whole value of reference type t: one of
        exactly that type, or for a `&S` a `&?S` variable inside its null
        check, through the cast the compiler asks for there. Never `self`,
        never a borrowed parameter."""
        out = []
        for e in self.scope.entries():
            if e.ty != t or e.kind in (SELF, BORROW):
                continue
            if e.kind == NARROWED:
                out.append(RefCast(Var(e.name, RefT(t.st, True))))
            else:
                out.append(Var(e.name, t))
        return out

    def gen_ref(self, t, depth):
        """A value of reference type t. Every `&S` alternative has static type
        exactly `&S` (the two branches of a `give` must agree), and a `&?S`
        is null, a `&?S` variable or field, or any `&S` (which widens)."""
        st = t.st
        k = self.rng.random()
        if t.nullable:
            if k < 0.15:
                return Null(t)
            if k < 0.50:
                vs = self.ref_vars(t)
                if vs:
                    return self.pick(vs)
            if k < 0.72:
                got = self.place(t, "read", depth)
                if got is not None:
                    return got
            if k < 0.78:
                got = self.list_ref(t, depth)
                if got is not None:
                    return got
            return self.gen_ref(RefT(st, False), depth)
        if k < 0.40:
            vs = self.ref_vars(t)
            if vs:
                return self.pick(vs)
        if k < 0.52:
            got = self.place(t, "read", depth)
            if got is not None:
                return got
        if k < 0.72 and depth > 0:
            got = self.builder_call(st, depth)
            if got is not None:
                return got
        if k < 0.80 and depth > 0:
            return Give(self.gen_bool(depth - 1), self.gen_ref(t, depth - 1),
                        self.gen_ref(t, depth - 1))
        if k < 0.88:
            got = self.list_ref(t, depth)
            if got is not None:
                return got
        return Heap(self.gen_value(st, depth))

    def exact_ref(self, t):
        """A value whose static type is exactly t, or None. `l[i] = e` is an
        operator and operators never widen (FREEZE F-OP-1): on a List<&?N>
        neither a `&N` nor `null` is accepted there, only a `&?N`."""
        if not t.nullable:
            return self.value_expr(t, 1)
        cands = self.ref_vars(t)
        got = self.place(t, "read")
        if got is not None:
            cands.append(got)
        return self.pick(cands) if cands else None

    def builder_call(self, st, depth):
        """nbK(v, a0, ...): a fresh node whose edges are references already
        in scope (shared substructure, and cycles once one is assigned back)."""
        b = self.builders[self.nodes.index(st)]
        spent = self.mult * (b.cost + 1)
        if self.cost + spent > self.cost_cap:
            return None
        self.cost += spent
        args = [self.gen_int(depth - 1)]
        for _, pt in b.params[1:]:
            args.append(self.gen_ref(pt, depth - 1))
        return Call(b.name, args, b.ret)

    def list_ref(self, t, depth):
        """(l.length() > 0 give l[i] otherwise <same type>) for a List of t."""
        ls = [e for e in self.scope.entries() if e.ty == ListT(t)]
        if not ls:
            return None
        if t.nullable:
            # `null` has no type of its own in a `give`: the other branch has
            # to be a `&?S` variable.
            vs = self.ref_vars(t)
            if not vs:
                return None
            other = self.pick(vs)
        else:
            other = Heap(Default(t.st))
        lv = Var(self.pick(ls).name, ListT(t))
        idx = Bin("%", self.gen_int(max(depth - 1, 0)), Length(lv))
        return Give(Bin(">", Length(lv), Lit(0)), Index(lv, idx), other)

    def ref_bool(self, depth):
        """`q == null` / `q != null` on a `&?S` variable or field, or identity
        between two references to the same struct type."""
        st = self.pick(self.nodes)
        t = RefT(st, True)
        if self.chance(0.6):
            cands = [v for v in self.ref_vars(t)]
            got = self.place(t, "read", depth)
            if got is not None:
                cands.append(got)
            if cands:
                return IsNull(self.pick(cands), self.chance(0.5))
        a = self.gen_ref(RefT(st, self.chance(0.5)), 0)
        b = self.gen_ref(RefT(st, self.chance(0.5)), 0)
        if isinstance(a, Null) or isinstance(b, Null):
            return None
        return Bin(self.pick(["==", "!="]), a, b)

    def ref_stmt(self, depth):
        k = self.rng.random()
        if k < 0.22:
            return self.ref_decl()
        if k < 0.42:
            return self.ref_assign()
        if k < 0.64:
            return self.narrow_if(depth)
        if k < 0.76:
            return self.walk_stmt(depth)
        if k < 0.86:
            return self.build_stmt(depth)
        if k < 0.96:
            return self.cycle_stmt(depth)
        return self.ref_list_write()

    def ref_decl(self):
        t = RefT(self.pick(self.nodes), self.chance(0.5))
        e = self.value_expr(t, 2)
        name = self.new_name("nq" if t.nullable else "np")
        self.scope.declare(name, t, MUT)
        return [Decl(name, t, e)]

    def ref_assign(self):
        """A reference variable or field takes another reference: this is
        where chains get longer, shared and cyclic."""
        t = RefT(self.pick(self.nodes), self.chance(0.6))
        p = self.place(t, "assign")
        if p is None:
            t = RefT(t.st, not t.nullable)
            p = self.place(t, "assign")
        if p is None:
            return None
        return [Assign(p, self.value_expr(t, 2))]

    def ref_touch(self, name, st):
        """A statement that dereferences the narrowed variable `name`."""
        val = Field(Var(name, RefT(st, False)), "val")
        if self.chance(0.5):
            return Assign(val, Bin("%", self.int_expr(2), Lit(1000)))
        if self.in_method:
            return Print(val)
        inner = Bin("+", Bin("*", Var("chk", "int"), Lit(31)), Bin("%", val, Lit(1000003)))
        return Assign(Var("chk", "int"), Bin("%", inner, Lit(1000000007)))

    def narrow_if(self, depth):
        """if (q != null) { ...q is a &S here... } [else { ... }], also as
        `q != null && cond`, and inverted: if (q == null) { } else { ... }.
        Only a variable is ever narrowed (E081 for anything else)."""
        cands = [e for e in self.scope.entries()
                 if is_ref(e.ty, True) and e.kind in (MUT, PARAM)]
        if not cands or depth >= 3:
            return None
        e = self.pick(cands)
        v, st = Var(e.name, e.ty), e.ty.st
        narrow = (e.name, st)
        n = self.rng.randint(1, 3)
        if self.chance(0.25):
            return [If([[IsNull(v, False), self.gen_block(self.rng.randint(0, 2), depth + 1), 0]],
                       self.gen_block(n, depth + 1, narrow))]
        cond = IsNull(v, True)
        if self.chance(0.3):
            # The right side of `&&` runs only when the left held: q is a &S there.
            self.scope.push()
            self.scope.narrow(e.name, st)
            extra = self.bool_expr(2)
            self.scope.pop()
            cond = Bin("&&", cond, extra)
        body = self.gen_block(n, depth + 1, narrow)
        els = self.gen_block(self.rng.randint(0, 2), depth + 1) if self.chance(0.3) else None
        return [If([[cond, body, 0]], els)]

    def walk_stmt(self, depth):
        """mut w: &?N = start; mut c: int = 0;
        while (w != null && c < K) { c++; ...; w = w.rJ; }
        The counter bounds the walk on a cycle. `continue` skips the step, so
        the next turn sees the same node; that is fine and still bounded."""
        if depth >= 2:
            return None
        st = self.pick(self.nodes)
        t = RefT(st, True)
        start = self.value_expr(t, 1)
        w = self.new_name("nw")
        self.scope.declare(w, t, MUT)
        bound = self.rng.randint(1, 6)
        saved = self.mult
        self.mult *= bound
        c = self.new_name("c")
        self.scope.declare(c, "int", LOOP)
        self.in_loop += 1
        # No defer in a walk body: it would run after the step `w = w.rJ`,
        # when `w` may be null (E081 since turflang 2f49c7e).
        saved_walk, self.in_walk = self.in_walk, True
        body = self.gen_block(self.rng.randint(1, 3), depth + 1, (w, st))
        self.in_walk = saved_walk
        self.in_loop -= 1
        self.mult = saved
        step = Assign(Var(w, t), Field(Var(w, RefT(st, False)), self.pick(self.self_refs(st))))
        return [Decl(w, t, start),
                While(c, bound, self.rng.randint(0, 2), body + [step], Var(w, t))]

    def build_stmt(self, depth):
        """for r in 0..K { q = nbK(e, q, ...); } -- grows a chain (a list when
        there is one edge, a degenerate tree or a DAG with more) from heap()
        nodes. A fresh `q` starts at null."""
        if depth >= 2:
            return None
        st = self.pick(self.nodes)
        b = self.builders[self.nodes.index(st)]
        t = RefT(st, True)
        out = []
        vs = [e.name for e in self.scope.entries() if e.ty == t and e.kind == MUT]
        if vs and self.chance(0.6):
            q = self.pick(vs)
        else:
            q = self.new_name("nq")
            self.scope.declare(q, t, MUT)
            out.append(Decl(q, t, Null(t)))
        k = self.rng.randint(1, 6)
        spent = self.mult * k * (b.cost + 3)
        if self.cost + spent > self.cost_cap:
            return out or None
        self.cost += spent
        saved = self.mult
        self.mult *= k
        r = self.new_name("r")
        self.scope.push()
        self.scope.declare(r, "int", LOOP)
        args = [self.int_expr(2), Var(q, t)]
        for _, pt in b.params[2:]:
            args.append(self.value_expr(pt, 0))
        self.scope.pop()
        self.mult = saved
        out.append(ForRange(r, 0, k, [Assign(Var(q, t), Call(b.name, args, b.ret))]))
        return out

    def cycle_stmt(self, depth):
        """Make a cycle on purpose; random assignments alone almost never
        close one (measured: none in 113 programs). Either tie two references
        in scope together (`x.rJ = y;`, a self-loop when y is x), or build a
        ring: a tail node, a chain consed onto it, and the tail's edge set
        back to the head, so a walk along that edge never reaches null.
        A cycle with no reference from outside is garbage as a whole, which
        only a tracing collector frees."""
        st = self.pick(self.nodes)
        t, tn = RefT(st, False), RefT(st, True)
        edges = self.self_refs(st)
        roots = [e.name for e in self.scope.entries() if e.ty == t and e.kind != BIND]
        vals = self.ref_vars(t)
        if roots and vals and self.chance(0.4):
            x = Var(self.pick(roots), t)
            out = [Assign(Field(x, self.pick(edges)), self.pick(vals))]
            if self.chance(0.5):  # and back: a two-node cycle unless x is y
                y = self.pick(roots)
                back = [v for v in vals]
                out.append(Assign(Field(Var(y, t), self.pick(edges)), self.pick(back)))
            return out
        if depth >= 2:
            return None
        b = self.builders[self.nodes.index(st)]
        k = self.rng.randint(0, 5)
        spent = self.mult * (k + 1) * (b.cost + 3)
        if self.cost + spent > self.cost_cap:
            return None
        self.cost += spent
        tail = self.new_name("np")
        head = self.new_name("nq")
        out = [Decl(tail, t, Heap(Default(st)))]
        self.scope.declare(tail, t, MUT)
        out.append(Decl(head, tn, Var(tail, t)))
        self.scope.declare(head, tn, MUT)
        saved = self.mult
        self.mult *= max(k, 1)
        r = self.new_name("r")
        self.scope.push()
        self.scope.declare(r, "int", LOOP)
        # The builder sets the J-th `&?Self` field from its J-th reference
        # argument: the chain runs along the first, edges[0] in field order.
        args = [self.int_expr(2), Var(head, tn)]
        for _, pt in b.params[2:]:
            args.append(self.value_expr(pt, 0))
        self.scope.pop()
        self.mult = saved
        out.append(ForRange(r, 0, k, [Assign(Var(head, tn), Call(b.name, args, b.ret))]))
        out.append(Assign(Field(Var(tail, t), edges[0]), Var(head, tn)))
        return out

    def ref_list_write(self):
        """l[i].val = e on a List<&N>: written through the element (the
        element is a reference, so this is not E098)."""
        ls = [e for e in self.scope.entries()
              if isinstance(e.ty, ListT) and is_ref(e.ty.elem, False) and e.kind != BIND]
        if not ls:
            return None
        e = self.pick(ls)
        lv = Var(e.name, e.ty)
        idx = Bin("%", self.int_expr(2), Length(lv))
        w = Assign(Field(Index(lv, idx), "val"), self.int_expr(2))
        return [If([[Bin(">", Length(lv), Lit(0)), [w], 0]], None)]

    def addr_arg(self, st):
        """&s for a struct local s of this frame, or a borrowed parameter
        passed on: an argument for a parameter the callee does not keep."""
        cands = [AddrOf(Var(e.name, e.ty)) for e in self.scope.entries()
                 if e.ty is st and e.kind == MUT]
        cands += [Var(e.name, e.ty) for e in self.scope.entries()
                  if e.kind == BORROW and e.ty == RefT(st, False)]
        return self.pick(cands) if cands else None

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
            # The key before the name is declared: it must not mention it.
            key = self.expr_ok(lambda: self.coll_key("int"))
            name = self.new_name("b")
            self.scope.declare(name, "bool", MUT)
            return [Decl(name, "bool", CMeth(c, "remove", [key], "bool"))]
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
        if self.use_generics and self.chance(0.07):
            got = self.generic_expr("double", depth)
            if got is not None:
                return got
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
        if depth > 0 and self.use_generics and self.chance(0.1):
            got = self.generic_expr(t, depth)
            if got is not None:
                return got
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
        if self.use_generics and self.chance(0.07):
            got = self.generic_expr("int", depth)
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
        if self.use_generics and self.chance(0.06):
            got = self.generic_expr("bool", depth)
            if got is not None:
                return got
        if self.nodes and self.chance(0.1):
            got = self.ref_bool(depth)
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
        if isinstance(t, RefT):
            return self.gen_ref(t, depth)
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
            args.append(Lit(self.rng.randint(0, callee.rec_max)))
            params = params[1:]
        for n, t in params:
            a = None
            if isinstance(t, SliceT):
                a = self.slice_arg(t)
            elif isinstance(t, ListT):
                a = self.list_arg(t)
            elif n in callee.borrows and self.chance(0.8):
                a = self.addr_arg(t.st)  # `&s`: into this frame, for the call only
            if a is None and not isinstance(t, (SliceT, ListT)):
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
        if isinstance(ty, RefT):
            return Null(ty) if ty.nullable else Heap(Default(ty.st))
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

    def gen_block(self, n, depth, narrow=None):
        """n statements in a scope of their own. narrow = (name, struct): the
        block is the checked side of `name != null`, and opens by using it."""
        self.scope.push()
        out = []
        if narrow is not None:
            self.scope.narrow(*narrow)
            out.append(self.ref_touch(*narrow))
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
        return (self.use_structs or self.use_arrays or self.use_lists or self.use_colls
                or self.use_generics or self.use_refs)

    def gen_stmt(self, depth):
        r = self.rng
        self.spend()
        if self.cost > self.cost_cap * 2:
            return [self.mix()]
        if self.has_aggs() and self.chance(0.35):
            got = self.agg_stmt(depth)
            if got:
                return got
        if self.use_defer and not self.in_defer and not self.in_walk and self.chance(0.06):
            got = self.defer_stmt()
            if got:
                return [got]
        if self.use_errors and not self.in_method and not self.in_run and self.chance(0.1):
            got = self.recover_run(depth)
            if got:
                return got
        if self.perr is not None and not self.in_method and not self.in_run and self.chance(0.1):
            got = (self.payload_sweep(depth) if self.chance(0.4)
                   else self.recover_run(depth, payload=True))
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
        if self.cur_fn is not None and self.cur_fn.generic:
            return Return(Var(self.pick(["a", "b"]), "T"))  # the only T values there are
        if self.cur_fn is not None and self.cur_fn.fallible and self.chance(0.5):
            return Return(self.err_lit())
        return Return(self.int_expr(2))

    def err_lit(self):
        msg = None
        if self.use_strings and self.chance(0.4):
            msg = self.str_expr(1)
        if self.perr is not None and self.chance(0.6):
            got = self.payload_lit(msg)
            if got is not None:
                return got
        name, variants = self.errors[0]
        return ErrLit(name, self.pick(variants), msg)

    def gen_payload_error(self):
        """error E1 { P0(x0: T, ...), ..., [Q] }: variants carrying data. The
        fields are heap values where the program has them (strings, structs
        holding strings and references, Lists, references), so the error
        value is what keeps them alive on its way up through `try`."""
        variants = []
        for i in range(self.rng.randint(1, 4)):
            fields = [("x%d" % j, self.payload_type())
                      for j in range(self.pick([1, 1, 2, 3]))]
            variants.append(("P%d" % i, fields))
        if self.chance(0.5):
            variants.append(("Q", []))
        self.perr = ("E1", variants)

    def payload_type(self):
        opts = ["int", "int"]
        if self.use_strings:
            opts += ["string"] * 3
        if self.structs:
            opts += [self.pick(self.structs)] * 3
        if self.use_lists:
            opts += [ListT("int")] * 2
            if self.use_strings:
                opts.append(ListT("string"))
        if self.nodes:
            opts += [RefT(self.pick(self.nodes), self.chance(0.5))] * 2
        if self.enums:
            opts.append(self.pick(self.enums))
        if self.use_numeric:
            opts.append("double")
        return self.pick(opts)

    def payload_lit(self, msg, variant=None):
        """E1::Pk(a0, a1[, msg]): one argument per field, in order. A List
        field takes a List in scope (shared with it), a copy, or a fresh
        `List::default()`, whose type comes from the field."""
        name, variants = self.perr
        v, fields = variant or self.pick(variants)
        args = []
        for _, ft in fields:
            if isinstance(ft, ListT):
                a = self.list_arg(ft) if self.chance(0.7) else None
                if a is None:
                    a = ListNew(ft)
            else:
                a = self.value_expr(ft, 1)
            if a is None:
                return None
            args.append(a)
        return ErrLit(name, v, msg, args, [ft for _, ft in fields])

    def bind_touch(self, name, ty):
        """Print something read from a bound payload field."""
        v = Var(name, ty)
        if ty in ("int", "bool", "string", "double"):
            return Print(v)
        if isinstance(ty, EnumT):
            return Print(EnumTo(v, "int"))
        if isinstance(ty, ListT):
            return Print(Length(v))
        if is_ref(ty, True):
            return Print(IsNull(v, self.chance(0.5)))
        if is_ref(ty, False):
            return Print(Field(v, "val"))
        for target in ("int", "string", "bool"):
            got = self.descend(v, ty, target, 1) if reaches(ty, target) else None
            if got is not None and got is not v:
                return Print(got)
        return None

    def err_compare(self, err, depth):
        """compare (err) { matches E1::Pk(b0, _, 3) if guard: ... done ...
        otherwise: ... } -- the only way a payload is read. Each variant
        appears once, or twice when its first arm can fail (a literal
        sub-pattern or a guard) and a catch-all arm for it follows."""
        ename, variants = self.perr
        pool = [(ename, v, fields) for v, fields in variants]
        pool += [(self.errors[0][0], v, []) for v in self.errors[0][1]]
        self.rng.shuffle(pool)
        arms = []
        for etype, v, fields in pool[:self.rng.randint(1, min(4, len(pool)))]:
            refutable = True
            while refutable:
                self.scope.push()
                n = len(fields)
                rest = n > 0 and self.chance(0.2)
                if rest:
                    n = self.rng.randint(0, n - 1)
                pats, bound, refutable = [], [], False
                for j in range(n):
                    ft = fields[j][1]
                    k = self.rng.random()
                    if ft == "int" and k < 0.15 and not arms_has(arms, etype, v):
                        pats.append(("lit", Lit(self.rng.randint(-2, 3))))
                        refutable = True
                    elif k < 0.85:
                        b = self.new_name("pb")
                        self.scope.declare(b, ft, BIND)
                        pats.append(("bind", b, ft))
                        bound.append((b, ft))
                    else:
                        pats.append(("skip",))
                guard = None
                if self.chance(0.25) and not arms_has(arms, etype, v):
                    guard = self.bool_expr(2)  # may read the bindings
                    refutable = True
                body = []
                if bound:
                    t = self.bind_touch(*self.pick(bound))
                    if t is not None:
                        body.append(t)
                for _ in range(self.rng.randint(0, 2)):
                    body.extend(self.gen_stmt(depth + 1))
                if self.chance(0.35):
                    body.append(self.jump())
                elif not body:
                    body.append(self.mix())
                self.scope.pop()
                arms.append(ErrArm(etype, v, pats, rest, guard, body, len(fields)))
                # A second, irrefutable arm for the same variant, sometimes.
                refutable = refutable and self.chance(0.5)
        els = self.gen_block(self.rng.randint(1, 2), depth + 1) or [self.mix()]
        return ErrCompare(err, arms, els)

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

    def recover_run(self, depth, payload=False, karg=None):
        """mut v: int = f(...) recover as err run { ...; if err == E0::X { jump }
        jump };  The block must diverge (int success type): `return`, or
        `break`/`continue` in a loop."""
        fallible = [f for f in self.funcs if f.fallible]
        if payload:
            # A callee built to fail with every payload variant (gen_func).
            fallible = [f for f in fallible if any(n == "k" for n, _ in f.params)]
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
        if karg is not None:
            call.args[[n for n, _ in callee.params].index("k")] = karg
        self.cost += spent
        # A third of the time the plain form, `recover run { ... }`: no binding,
        # so nothing in the block can ask which error it was.
        err = None if self.chance(0.33) and not payload else self.new_name("err")
        name_, variants = self.errors[0]
        self.in_run = True
        self.scope.push()
        try:
            body = []
            if err is not None and self.use_strings and self.chance(0.4):
                body.append(Print(ErrMsg(err)))
            if self.chance(0.6):
                body.append(self.mix() if self.chance(0.5) else self.print_stmt())
            if err is not None and self.perr is not None and (payload or self.chance(0.7)):
                body.append(self.err_compare(err, depth))
            if err is not None:
                if self.perr is not None and self.chance(0.4):
                    # `==` on a variant with fields names it bare; it tests
                    # which variant, never the payload.
                    name_, variants = self.perr[0], [v for v, _ in self.perr[1]]
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
        if self.sweep:
            # Inside a payload sweep: go on to the next variant, mostly.
            return Continue() if self.chance(0.9) else Break()
        if self.in_loop and self.chance(0.35):
            return self.pick([Break, Continue])()
        return self.return_stmt()

    def payload_sweep(self, depth):
        """for r in 0..n+2 { mut v: int = fK(.., r, ..) recover as err run {
        compare (err) { ... } continue; }; chk = mix(v); }
        Calls a thrower once per `k`, so each payload variant is constructed,
        carried out and matched in one run."""
        throwers = [f for f in self.funcs
                    if f.fallible and any(n == "k" for n, _ in f.params)]
        if not throwers or depth >= 2 or self.in_run or self.in_method:
            return None
        n = len(self.perr[1]) + 2
        r = self.new_name("r")
        saved = self.mult
        self.mult *= n
        self.scope.push()
        self.scope.declare(r, "int", LOOP)
        self.in_loop += 1
        self.sweep = True
        got = self.recover_run(depth + 1, True, Var(r, "int"))
        self.sweep = False
        body = (got or []) + [self.mix()]
        self.in_loop -= 1
        self.scope.pop()
        self.mult = saved
        return [ForRange(r, 0, n, body)] if got else None

    def agg_stmt(self, depth):
        if self.nodes and self.chance(0.4):
            got = self.ref_stmt(depth)
            if got:
                return got
        if self.use_colls and self.chance(0.3):
            got = self.coll_stmt()
            if got:
                return got
        if self.use_generics and self.chance(0.2):
            got = self.gput_stmt()
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
        if (isinstance(t, ArrT) and not contains_string(t) and not has_nonnull_ref(t)
                and self.chance(0.2)):
            # `mut a: int[3];` is zero-filled, but definite assignment (F-INIT-1)
            # counts the whole array unassigned until one element is written.
            # Not with strings: an unwritten `string` element is a null
            # pointer, not "" (open turflang issue, SEMANTICS.md). Nor with a
            # `&S` field, which the same declaration leaves null.
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
            for e in self.roots(True):
                if is_ref(e.ty, False):  # what a `&S` leads to, not S itself
                    sub.extend(self._subtypes(e.ty.st)[1:])
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
        # Through a reference the receiver is the variable itself: `p.m0()`.
        refs = [e for e in self.scope.entries()
                if e.ty == RefT(st, False) and e.kind != BIND]
        if refs and self.chance(0.5):
            recv = Var(self.pick(refs).name, RefT(st, False))
        else:
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
        ls = [e for e in self.scope.entries()
              if isinstance(e.ty, ListT) and e.kind != BIND]
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
            # A reference element takes a value of exactly its type (exact_ref).
            if is_ref(e.ty.elem):
                val = self.exact_ref(e.ty.elem)
                if val is None:
                    return [Push(lv, self.value_expr(e.ty.elem, 2))]
            else:
                val = self.value_expr(e.ty.elem, 2)
            if self.chance(self.opaque_p):
                self.opaque_used = True
                return [Assign(Index(lv, Call("opaque", [self.int_expr(2)])), val)]
            idx = Bin("%", self.int_expr(2), Length(lv))
            return [If([[guard, [Assign(Index(lv, idx), val)], 0]], None)]
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
        if self.nodes and k < 0.15:
            return RefT(self.pick(self.nodes), self.chance(0.5))
        if k < 0.6 or not self.has_aggs():
            return "int"
        if k < 0.7:
            return "bool"
        if k < 0.82 and self.structs:
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
        # A walker: `fn fK(d: int, t: &?N, ...)` that returns at once on null
        # (the early-return form of a null check: `t` is a `&N` from there on)
        # and calls itself on one or more of t's edges, a recursive walk of a
        # list, a tree or, bounded by `d`, a cyclic graph.
        walker = bool(self.nodes) and self.chance(0.35)
        rec = walker or (self.use_rec and self.chance(0.4))
        params = [("d", "int")] if rec else []
        edges = []
        if walker:
            st = self.pick(self.nodes)
            params.append(("t", RefT(st, True)))
            own = self.self_refs(st)
            edges = self.rng.sample(own, self.rng.randint(1, len(own)))
        thrower = fallible and self.perr is not None and self.chance(0.85)
        if thrower:
            params.append(("k", "int"))
        for i in range(self.rng.randint(0, 3)):
            params.append(("p%d" % i, self.param_type()))
        f = Func("f%d" % idx, params, [], fallible, rec)
        # Total calls of a b-way recursion from depth rec_max stay near 40.
        f.rec_max = {0: 6, 1: 6, 2: 4, 3: 3}[len(edges)]
        f.borrows = {n for n, t in params if is_ref(t, False) and self.chance(0.5)}
        self.cur_fn = f
        for n, t in params:
            kind = (SLICE_PARAM if isinstance(t, SliceT)
                    else BORROW if n in f.borrows else PARAM)
            self.scope.declare(n, t, kind)
        self.scope.declare("chk", "int", MUT)
        body = [Decl("chk", "int", Lit(0), True)]
        if walker:
            t = Var("t", RefT(st, True))
            body.append(If([[IsNull(t, False), [self.return_stmt()], 0]], None))
            self.scope.narrow("t", st)  # for the rest of the function
        if rec:
            # Base case first, so every level does at most one body's work.
            body.append(If([[Bin("<=", Var("d", "int"), Lit(0)), [self.return_stmt()], 0]], None))
        body.extend(self.initial_vars())
        if thrower:
            # Fails with variant i when `k % (n + 1) == i`, so most calls fail
            # and every variant is reached; built after the locals, which the
            # payloads are made from.
            vs = self.perr[1]
            for i, variant in enumerate(vs):
                msg = self.str_expr(1) if self.use_strings and self.chance(0.3) else None
                lit = self.expr_ok(lambda: self.payload_lit(msg, variant), None)
                if lit is None:
                    continue
                hit = Bin("==", Bin("%", Var("k", "int"), Lit(len(vs) + 1)), Lit(i))
                body.append(If([[hit, [Return(lit)], 0]], None))
        elif fallible and self.chance(0.7):
            # One failure path that is always there; the random early returns
            # are rare.
            body.append(If([[self.bool_expr(2), [Return(self.err_lit())], 0]], None))
        body.extend(self.gen_block(self.rng.randint(1, 6), 0))
        if walker:
            for edge in edges:
                body.append(self.self_call(f, Field(Var("t", RefT(st, False)), edge)))
        elif rec:
            body.append(self.self_call(f))
        body.append(Return(self.int_expr(3)))
        f.body = body
        f.cost = self.cost + 2
        if rec:
            b = max(len(edges), 1)
            f.cost *= sum(b ** i for i in range(f.rec_max + 1))
        self.cur_fn = None
        return f

    def self_call(self, f, edge=None):
        """chk = mix(chk, f(d - 1, ...)): one recursive call per level, at the
        top of the body (never in a loop), so the work is linear in the depth.
        An aggregate parameter is passed on as it is. A walker passes one of
        its node's edges (`t.rJ`) as the next `t`, once per edge it follows."""
        args = [Bin("-", Var("d", "int"), Lit(1))]
        for n, t in f.params[1:]:
            if edge is not None and n == "t":
                args.append(edge)
            else:
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
        if self.nodes:
            for _ in range(self.rng.randint(1, 2)):
                out.extend(self.ref_decl())
        return out

    def gen_main(self):
        self.reset_fn(4000)
        self.in_func = False
        self.scope.declare("chk", "int", MUT)
        body = [Decl("chk", "int", Lit(0), True)]
        body.extend(self.initial_vars())
        if self.perr is not None and self.chance(0.8):
            body.extend(self.payload_sweep(0) or [])
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
        if self.use_refs:
            self.gen_nodes()
            # Fixed bodies that call nothing, so everything may call them.
            self.builders = [self.gen_builder(st, i) for i, st in enumerate(self.nodes)]
        if self.use_errors and self.chance(0.6):
            self.gen_payload_error()
        if self.use_generics:
            # Before every other function, so that methods, helpers and main
            # may all call them (each later generic may call the earlier ones).
            for i in range(self.rng.randint(1, 2)):
                self.gfuncs.append(self.gen_gfunc(i))
        for st in self.structs:
            for i in range(self.rng.choice([0, 1, 1, 2])):
                st.methods.append(self.gen_method(st, i))
        for i in range(self.rng.randint(0, 4)):
            self.funcs.append(self.gen_func(i))
        main = self.gen_main()
        funcs = self.gfuncs + self.builders + self.funcs
        if self.opaque_used:
            funcs.insert(0, Func("opaque", [("x", "int")], [Return(Var("x", "int"))]))
        prog = Program(self.structs, funcs, main, self.seed, self.paren_mode, self.errors,
                       self.enums)
        if self.gstruct_used:
            prog.gstructs = ["Gb0"]
        if self.perr is not None:
            prog.payload_errors = [self.perr]
        return prog


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
