#!/usr/bin/env python3
"""Program reducer for generator findings (turf-fuzz item 4).

A finding from the generator is a whole program of a few hundred lines. This
shrinks it on the generator's own AST, not its text, keeping the oracle's
verdict (its class and signature) the same:

  * delete statements: half a body at a time, then one by one, last first;
  * replace a compound statement by the body it runs (an `if` by a branch, a
    loop by its body), and drop `elseif`/`else` branches and `compare` arms;
  * inline: replace an expression by the value it had when the program ran
    (recorded by the evaluator), or by a same-typed operand of it;
  * delete whole helpers, methods, structs, enums and error types, unused
    struct fields, and unused helper parameters (with their arguments).

Working on the AST means every candidate renders as well-formed Turf, and the
evaluator recomputes the expected result for it, so "the compiler disagrees
with the evaluator in the same way" stays a meaningful test all the way
down. A candidate the evaluator cannot run (a name its deletion left
undefined, a function that now falls off its end) is dropped without
compiling it.

Each probe compiles and runs only the configurations the verdict depends on:
-O0 for a wrong answer or a rejected program, the crashing configuration for
a compiler crash, one configuration per disagreeing group for a miscompile,
the reporting ones for a sanitizer or GC-validate report. Probes run in
parallel batches; the first interesting candidate in enumeration order wins,
so a reduction is deterministic for a given compiler.

    python3 gen/reduce.py SEED [-o min.tr] [--max-seconds 300] [--jobs 8]
                               [--loose]

--loose keeps a candidate whose verdict has the same class but a different
signature (rarely wanted: a reduction can slide from one bug to another).
"""

import argparse
import copy
import math
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "oracles"))

import tast  # noqa: E402
import evaluate as ev  # noqa: E402
import generate  # noqa: E402
import differential as D  # noqa: E402
from tast import (Lit, BLit, SLit, DLit, Conv, EnumLit, EnumT, Bin, Un, Give,  # noqa: E402
                  If, While, ForRange, ForEach, Compare, ErrCompare, RecoverRun,
                  Defer, Return)

# Statement classes: a list whose items are all of these is a body.
STMTS = (tast.Decl, tast.Assign, tast.Comp, tast.IncDec, tast.Print, If, While,
         ForRange, ForEach, tast.MethodCall, tast.Push, Defer, Compare, ErrCompare,
         RecoverRun, tast.GPut, tast.CStmt, tast.Break, tast.Continue, Return)
TYPES = (tast.StructT, tast.ArrT, tast.SliceT, tast.ListT, tast.CollT, tast.RefT, EnumT)
# Expression slots that must stay places (or a receiver): never replaced.
PLACE_KEYS = {"place", "base", "recv"}


# ------------------------------------------------------------------ paths

def bodies(prog):
    """(path, function) of every function body: a path is a list of steps,
    each an attribute name or a list index, from the Program."""
    out = [(["main", "body"], prog.main)]
    for i, f in enumerate(prog.funcs):
        out.append((["funcs", i, "body"], f))
    for i, st in enumerate(prog.structs):
        for j, m in enumerate(st.methods):
            out.append((["structs", i, "methods", j, "body"], m))
    return out


def get(root, path):
    for step in path:
        root = root[step] if isinstance(step, int) else getattr(root, step)
    return root


def put(root, path, value):
    parent = get(root, path[:-1])
    if isinstance(path[-1], int):
        parent[path[-1]] = value
    else:
        setattr(parent, path[-1], value)


def is_expr(x):
    return (type(x).__module__ == "tast" and hasattr(x, "ty") and not isinstance(x, TYPES)
            and not isinstance(x, STMTS))


def walk(node, path, out_lists, out_exprs, key=None):
    """Collect statement lists (path) and expression slots (path, expr, key)
    under node, which is a statement, an expression or a list."""
    if isinstance(node, list):
        if node and all(isinstance(s, STMTS) for s in node):
            out_lists.append(path)
        for i, x in enumerate(node):
            walk(x, path + [i], out_lists, out_exprs, key)
        return
    if isinstance(node, TYPES) or isinstance(node, (str, int, float, bool, tuple)) or node is None:
        return
    if type(node).__module__ != "tast":
        return
    if is_expr(node):
        out_exprs.append((path, node, key))
    for k, v in vars(node).items():
        if k in ("ty", "line", "decl_line", "incr_line", "ftypes"):
            continue
        walk(v, path + [k], out_lists, out_exprs, k)


def collect(prog):
    lists, exprs = [], []
    for path, f in bodies(prog):
        lists.append(path)
        walk(f.body, path, lists, exprs)
    seen, uniq = set(), []
    for p in lists:  # a function body is found twice
        if tuple(p) not in seen:
            seen.add(tuple(p))
            uniq.append(p)
    return uniq, exprs


# ------------------------------------------------------------- literals

def literal_for(value, ty):
    """A literal expression for a value the evaluator recorded, or None."""
    if isinstance(ty, EnumT) and isinstance(value, ev.EnumV):
        return EnumLit(ty, value.k)
    if ty == "bool" and isinstance(value, bool):
        return BLit(value)
    if ty == "int" and isinstance(value, int) and not isinstance(value, bool):
        return Lit(value) if tast.INT_MIN <= value <= tast.INT_MAX else None
    if ty in ("int32", "char") and isinstance(value, int):
        return Conv(Lit(value), ty)
    if ty == "double" and isinstance(value, float) and math.isfinite(value):
        return DLit(value)
    if ty == "string" and isinstance(value, str) and all(
            c.isalnum() or c in " \t\n.-" for c in value) and len(value) < 64:
        return SLit(value)
    return None


def zero_for(ty):
    if isinstance(ty, tast.RefT):
        # The simplest reference of each kind: a fresh default node, or null.
        return tast.Null(ty) if ty.nullable else tast.Heap(tast.Default(ty.st))
    return {"int": Lit(0), "bool": BLit(False), "string": SLit(""),
            "double": DLit(0.0)}.get(ty) if isinstance(ty, str) else (
        EnumLit(ty, 0) if isinstance(ty, EnumT) else None)


def record_values(prog):
    """id(expression) -> the first value it evaluated to, in one run."""
    seen = {}
    orig = ev._Machine.ev

    def rec(self, e, env):
        v = orig(self, e, env)
        seen.setdefault(id(e), v)
        return v
    ev._Machine.ev = rec
    try:
        tast.render(prog)
        ev.evaluate(prog)
    except Exception:
        pass
    finally:
        ev._Machine.ev = orig
    return seen


# ----------------------------------------------------------- candidates
# A candidate is (label, edit): edit(prog) changes a deep copy in place.

def stmt_deletions(prog):
    lists, _ = collect(prog)
    out = []
    for path in lists:  # halves first: big wins early
        body = get(prog, path)
        n = len(body)
        for size in (n // 2, n // 4):
            if size >= 2:
                for start in range(0, n - size + 1, size):
                    out.append(("delete %d stmts at %s" % (size, path),
                                lambda p, path=path, s=start, z=size: delete_range(p, path, s, z)))
    for path in lists:  # then one at a time, last first
        body = get(prog, path)
        for i in reversed(range(len(body))):
            out.append(("delete stmt %s[%d]" % (path, i),
                        lambda p, path=path, i=i: delete_range(p, path, i, 1)))
    return out


def delete_range(prog, path, start, size):
    body = get(prog, path)
    del body[start:start + size]


def unwraps(prog):
    """A compound statement replaced by a body it runs; branches dropped."""
    lists, _ = collect(prog)
    out = []
    for path in lists:
        body = get(prog, path)
        for i, s in enumerate(body):
            here = path + [i]
            subs = []
            if isinstance(s, If):
                subs = [br[1] for br in s.branches] + ([s.els] if s.els else [])
                if len(s.branches) > 1:
                    for j in range(1, len(s.branches)):
                        out.append(("drop elseif %s.%d" % (here, j),
                                    lambda p, h=here, j=j: get(p, h).branches.pop(j)))
                if s.els is not None:
                    out.append(("drop else %s" % here,
                                lambda p, h=here: setattr(get(p, h), "els", None)))
            elif isinstance(s, (While, ForRange, ForEach)):
                subs = [s.body]
            elif isinstance(s, Compare):
                subs = [b for _, b in s.arms] + ([s.els] if s.els else [])
                for j in range(len(s.arms)):
                    if len(s.arms) > 1 or s.els is not None:
                        out.append(("drop arm %s.%d" % (here, j),
                                    lambda p, h=here, j=j: get(p, h).arms.pop(j)))
            elif isinstance(s, ErrCompare):
                for j in range(len(s.arms)):
                    out.append(("drop err arm %s.%d" % (here, j),
                                lambda p, h=here, j=j: get(p, h).arms.pop(j)))
            elif isinstance(s, Defer):
                out.append(("undefer %s" % here,
                            lambda p, path=path, i=i: splice(p, path, i, [get(p, path)[i].stmt])))
            for k, sub in enumerate(subs):
                out.append(("unwrap %s -> body %d" % (here, k),
                            lambda p, path=path, i=i, k=k: splice(p, path, i, sub_bodies(get(p, path)[i])[k])))
    return out


def sub_bodies(s):
    if isinstance(s, If):
        return [br[1] for br in s.branches] + ([s.els] if s.els else [])
    if isinstance(s, (While, ForRange, ForEach)):
        return [s.body]
    if isinstance(s, Compare):
        return [b for _, b in s.arms] + ([s.els] if s.els else [])
    return []


def splice(prog, path, i, stmts):
    body = get(prog, path)
    body[i:i + 1] = list(stmts)


def expr_edits(prog, values):
    _, exprs = collect(prog)
    out = []
    for path, e, key in exprs:
        if key in PLACE_KEYS or isinstance(e, (Lit, BLit, SLit, DLit, EnumLit)):
            continue
        lit = literal_for(values[id(e)], e.ty) if id(e) in values else None
        if lit is None and (id(e) not in values or isinstance(e.ty, tast.RefT)):
            # Never evaluated: any value of its type does. A reference has no
            # literal for the node it pointed at; the simplest one may do.
            lit = zero_for(e.ty)
        if isinstance(e, (tast.Null, tast.Heap)) and isinstance(lit, type(e)):
            lit = None  # already as simple as it gets
        if lit is not None:
            out.append(("inline %s" % path, lambda p, path=path, lit=lit: put(p, path, copy.deepcopy(lit))))
        for attr in ("l", "r", "a", "b", "e"):
            c = getattr(e, attr, None)
            if is_expr(c) and c.ty == e.ty and isinstance(e, (Bin, Un, Give)):
                out.append(("hoist %s.%s" % (path, attr),
                            lambda p, path=path, attr=attr: put(p, path, getattr(get(p, path), attr))))
    return out


def decl_deletions(prog):
    out = []
    for i in reversed(range(len(prog.funcs))):
        out.append(("delete fn %s" % prog.funcs[i].name, lambda p, i=i: p.funcs.pop(i)))
    for i, st in enumerate(prog.structs):
        for j in reversed(range(len(st.methods))):
            out.append(("delete method %s.%s" % (st.name, st.methods[j].name),
                        lambda p, i=i, j=j: p.structs[i].methods.pop(j)))
    for i in reversed(range(len(prog.structs))):
        out.append(("delete struct %s" % prog.structs[i].name, lambda p, i=i: p.structs.pop(i)))
    for i, f in enumerate(prog.funcs):
        if f.generic or f.rec:
            continue  # a generic's params are fixed; a recursion's first is its depth
        for j in reversed(range(len(f.params))):
            out.append(("drop param %s.%s" % (f.name, f.params[j][0]),
                        lambda p, i=i, j=j: drop_param(p, i, j)))
    for i, st in enumerate(prog.structs):
        if len(st.fields) > 1:
            for j in reversed(range(len(st.fields))):
                out.append(("drop field %s.%s" % (st.name, st.fields[j][0]),
                            lambda p, i=i, j=j: p.structs[i].fields.pop(j)))
    for i, (_, variants) in enumerate(prog.errors):
        if len(variants) > 1:
            for j in reversed(range(len(variants))):
                out.append(("drop variant %d of errors[%d]" % (j, i),
                            lambda p, i=i, j=j: p.errors[i][1].pop(j)))
    for i, (_, variants) in enumerate(prog.payload_errors):
        if len(variants) > 1:
            for j in reversed(range(len(variants))):
                out.append(("drop variant %d of payload_errors[%d]" % (j, i),
                            lambda p, i=i, j=j: p.payload_errors[i][1].pop(j)))
    for attr in ("enums", "errors", "payload_errors", "gstructs"):
        for i in reversed(range(len(getattr(prog, attr)))):
            out.append(("delete %s[%d]" % (attr, i), lambda p, a=attr, i=i: getattr(p, a).pop(i)))
    return out


def drop_param(prog, i, j):
    """Delete function i's parameter j and the matching argument of every call
    to it. A body that still reads it fails in the evaluator (KeyError)."""
    f = prog.funcs[i]
    f.params.pop(j)
    for _, fn in bodies(prog):
        _, exprs = [], []
        walk(fn.body, [], [], exprs)
        for _, e, _ in exprs:
            if isinstance(e, tast.Call) and e.fn == f.name and len(e.args) > j:
                e.args.pop(j)


# ------------------------------------------------------------- validity
# Static rules the compiler enforces and the evaluator never sees, because a
# rule about code that does not run is invisible to it. Without them a
# reduction slides: deleting a walker's `if (t == null) { return 0; }` still
# gives "E081 on t", now for a good reason. Each rule is a generator invariant
# (gen/SEMANTICS.md); the original program must pass them all.

JUMPS = (Return, tast.Break, tast.Continue)


def valid(prog):
    try:
        for _, f in bodies(prog):
            declared = {n: t for n, t in getattr(f, "params", [])}
            is_method = isinstance(f, tast.Method)
            if not is_method and not (f.body and isinstance(f.body[-1], Return)):
                return False  # missing return
            check_block(f.body, declared, set(), 0)
        return True
    except Exception:  # Invalid, or a candidate too broken to check at all
        return False


class Invalid(Exception):
    pass


def narrowed_var(cond, neg):
    """The variable `cond` proves non-null when it is true (neg=True) or
    false (neg=False), or None."""
    if isinstance(cond, tast.IsNull) and cond.neg == neg and isinstance(cond.e, tast.Var):
        return cond.e.name
    if neg and isinstance(cond, Bin) and cond.op == "&&":
        return narrowed_var(cond.l, True) or narrowed_var(cond.r, True)
    return None


def check_exprs(node, declared, narrowed):
    """A `&?S` variable read as a `&S` (a field, a method, a cast) only where
    narrowed."""
    _, exprs = [], []
    walk(node, [], [], exprs)
    for _, e, _ in exprs:
        if isinstance(e, tast.Var) and isinstance(e.ty, tast.RefT) and not e.ty.nullable:
            dt = declared.get(e.name)
            if isinstance(dt, tast.RefT) and dt.nullable and e.name not in narrowed:
                raise Invalid()
        if isinstance(e, tast.RefCast) and isinstance(e.e, tast.Var) and e.e.name not in narrowed:
            raise Invalid()


def check_cond(cond, declared, narrowed):
    """check_exprs, except that the right side of `a && b` is checked with
    whatever `a` proves non-null: it only runs when `a` held."""
    if isinstance(cond, Bin) and cond.op == "&&":
        check_cond(cond.l, declared, narrowed)
        v = narrowed_var(cond.l, True)
        check_cond(cond.r, declared, narrowed | ({v} if v else set()))
    else:
        check_exprs(cond, declared, narrowed)


def check_block(stmts, declared, narrowed, loops):
    narrowed = set(narrowed)
    declared = dict(declared)
    for i, st in enumerate(stmts):
        if i > 0 and isinstance(stmts[i - 1], JUMPS):
            raise Invalid()  # unreachable (E012)
        if isinstance(st, tast.Decl):
            check_exprs(st.e, declared, narrowed) if st.e is not None else None
            declared[st.name] = st.ty
            if st.e is None:  # `mut a: T[N];` is written at once (F-INIT-1)
                nxt = stmts[i + 1] if i + 1 < len(stmts) else None
                if not (isinstance(nxt, tast.Assign) and isinstance(nxt.place, tast.Index)
                        and isinstance(nxt.place.base, tast.Var) and nxt.place.base.name == st.name):
                    raise Invalid()
            continue
        if isinstance(st, (tast.Break, tast.Continue)) and loops == 0:
            raise Invalid()
        if isinstance(st, If):
            for cond, body, _ in st.branches:
                check_cond(cond, declared, narrowed)
                v = narrowed_var(cond, True)
                check_block(body, declared, narrowed | ({v} if v else set()), loops)
            if st.els is not None:
                v = narrowed_var(st.branches[0][0], False) if len(st.branches) == 1 else None
                check_block(st.els, declared, narrowed | ({v} if v else set()), loops)
            # `if (v == null) { ...; jump }` with no else: v is a &S after it.
            if len(st.branches) == 1 and st.els is None and st.branches[0][1] \
                    and isinstance(st.branches[0][1][-1], JUMPS):
                v = narrowed_var(st.branches[0][0], False)
                if v:
                    narrowed.add(v)
            continue
        if isinstance(st, While):
            inner = narrowed | ({st.guard.name} if st.guard is not None else set())
            check_block(st.body, dict(declared, **{st.counter: "int"}), inner, loops + 1)
            continue
        if isinstance(st, ForRange):
            check_block(st.body, dict(declared, **{st.var: "int"}), narrowed, loops + 1)
            continue
        if isinstance(st, ForEach):
            check_exprs(st.base, declared, narrowed)
            d = dict(declared, **{st.var: st.base.ty.elem})
            if st.ivar:
                d[st.ivar] = "int"
            check_block(st.body, d, narrowed, loops + 1)
            continue
        if isinstance(st, Compare):
            check_exprs(st.subject, declared, narrowed)
            if isinstance(st.subject.ty, EnumT) and st.els is None:
                covered = {p.k for pats, _ in st.arms for p in pats}
                if len(covered) < len(st.subject.ty.variants):
                    raise Invalid()  # an enum compare covers every variant (E059)
            for _, body in st.arms:
                if not body:
                    raise Invalid()  # E058
                check_block(body, declared, narrowed, loops)
            if st.els is not None:
                check_block(st.els, declared, narrowed, loops)
            continue
        if isinstance(st, ErrCompare):
            for arm in st.arms:
                d = dict(declared)
                for pat in arm.pats:
                    if pat[0] == "bind":
                        d[pat[1]] = pat[2]
                check_block(arm.body, d, narrowed, loops)
            check_block(st.els, declared, narrowed, loops)
            continue
        if isinstance(st, RecoverRun):
            check_exprs(st.call, declared, narrowed)
            if not st.body or not isinstance(st.body[-1], JUMPS):
                raise Invalid()  # the block must diverge
            check_block(st.body, declared, narrowed, loops)
            declared[st.name] = "int"
            continue
        if isinstance(st, Defer):
            check_exprs(st.stmt, declared, narrowed)
            continue
        check_exprs(st, declared, narrowed)
        # Reassigning a narrowed variable ends its proof.
        if isinstance(st, tast.Assign) and isinstance(st.place, tast.Var):
            narrowed.discard(st.place.name)


# --------------------------------------------------------------- oracle

def configs_for(v):
    """The run configurations a verdict depends on."""
    if v.cls in ("wrong-answer", "rejected-valid"):
        return ["O0"]
    if v.cls == "crash" and not v.runs:  # the compiler crashed or hung
        return [list(v.compile)[-1]]
    if v.cls == "sanitizer":
        return [n for n, o in v.runs.items() if o.kind == "sanitizer"]
    if v.cls == "miscompile":
        groups = {}
        for n, o in v.runs.items():
            groups.setdefault(o.key(), n)
        return sorted(groups.values())
    if v.cls == "crash" and any(o.gc_validate for o in v.runs.values()):
        return [n for n, o in v.runs.items() if o.gc_validate][:1]
    return ["O0"]


class Oracle:
    def __init__(self, seed, only, workroot):
        self.seed, self.only, self.workroot = seed, only, workroot
        self.probes = 0
        self.check_valid = True
        self.compile_only = False  # the verdict is the compiler's alone

    def verdict(self, prog):
        """(cls, sig) for prog, or None if it breaks a generator rule or the
        evaluator cannot run it."""
        if self.check_valid and not valid(prog):
            return None
        try:
            tast.render(prog)
            res = ev.evaluate(prog)
        except Exception:  # an undefined name, a fall-through, a stray jump, ...
            return None
        self.probes += 1
        wd = tempfile.mkdtemp(prefix="red_", dir=self.workroot)
        try:
            path = os.path.join(wd, "prog.tr")
            with open(path, "w") as f:
                f.write(prog.source)
            v = D.check_source(self.seed, path, wd, res, only=self.only,
                               compile_only=self.compile_only)
            return (v.cls, v.sig)
        finally:
            shutil.rmtree(wd, ignore_errors=True)


def size(prog):
    tast.render(prog)
    return prog.source.count("\n")


def reduce(prog, oracle, target, loose, jobs, deadline, log):
    passes = [("statements", lambda p: stmt_deletions(p)),
              ("unwrap", lambda p: unwraps(p)),
              ("declarations", lambda p: decl_deletions(p)),
              ("inline", lambda p: expr_edits(p, record_values(copy.deepcopy(p))))]

    def interesting(got):
        if got is None or got[0] != target[0]:
            return False
        return loose or got == target

    def attempt(cand):
        label, edit = cand
        p = copy.deepcopy(prog_box[0])
        try:
            edit(p)
            return p if interesting(oracle.verdict(p)) else None
        except Exception:  # an edit that does not apply, or breaks the renderer
            return None

    prog_box = [prog]
    with ThreadPoolExecutor(jobs) as pool:
        changed = True
        while changed and time.time() < deadline:
            changed = False
            for pname, make in passes:
                progress = True
                while progress and time.time() < deadline:
                    progress = False
                    cands = make(prog_box[0])
                    for start in range(0, len(cands), jobs):
                        if time.time() > deadline:
                            break
                        batch = cands[start:start + jobs]
                        results = list(pool.map(attempt, batch))
                        hit = next((i for i, r in enumerate(results) if r is not None), None)
                        if hit is not None:
                            prog_box[0] = results[hit]
                            log("  %-13s %4d lines  (%s)" % (pname, size(prog_box[0]), batch[hit][0]))
                            progress = changed = True
                            break
    return prog_box[0]


def reduce_seed(seed, max_seconds=300, jobs=8, loose=False, log=lambda s: None):
    """Reduce generator seed; returns (source, summary) or (None, reason)."""
    work = os.path.join(ROOT, "work")
    os.makedirs(work, exist_ok=True)
    wd = tempfile.mkdtemp(prefix="reduce_", dir=work)
    prog, res, _ = generate.generate(seed)
    path = os.path.join(wd, "prog.tr")
    with open(path, "w") as f:
        f.write(prog.source)
    full = D.check_source(seed, path, wd, res)
    shutil.rmtree(wd, ignore_errors=True)
    if full.ok():
        return None, "the oracle finds nothing to reduce"
    only = configs_for(full)
    oracle = Oracle(seed, only, work)
    # A rejection or a compiler crash is decided before anything runs, so
    # probes compile with --emit-obj and skip the linker (about half of each
    # compile's time).
    oracle.compile_only = full.cls == "rejected-valid" or (full.cls == "crash" and not full.runs)
    if not valid(prog):
        # The rules above misjudge this program: reducing without them is
        # still useful, but the result may have slid (said in the summary).
        oracle.check_valid = False
        log("note: the original fails reduce.valid(); reducing without it")
    target = oracle.verdict(copy.deepcopy(prog))
    if target is None or target[0] != full.cls:
        return None, "%s with every configuration, but not with %s alone" % (
            full.cls, ",".join(only))
    log("seed %d: %s, %d lines; probing %s\n  sig: %s" % (
        seed, full.cls, size(prog), ",".join(only), target[1]))
    before = size(prog)
    start = time.time()
    small = reduce(prog, oracle, target, loose, max(1, min(jobs, 8)), start + max_seconds, log)
    tast.render(small)
    summary = "%d -> %d lines, %d probes, %.0fs, verdict kept on %s%s" % (
        before, size(small), oracle.probes, time.time() - start, ",".join(only),
        "" if oracle.check_valid else ", WITHOUT the validity rules")
    src = small.source.replace("// turf-fuzz gen: seed=%d" % seed,
                               "// turf-fuzz gen: seed=%d (reduced: %s)" % (seed, summary), 1)
    return src, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=int)
    ap.add_argument("-o", "--output")
    ap.add_argument("--max-seconds", type=float, default=300)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--loose", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    log = (lambda s: None) if a.quiet else (lambda s: print(s, flush=True))
    src, summary = reduce_seed(a.seed, a.max_seconds, a.jobs, a.loose, log)
    if src is None:
        sys.exit("seed %d: %s" % (a.seed, summary))
    log("done: " + summary)
    if a.output:
        with open(a.output, "w") as f:
            f.write(src)
    else:
        sys.stdout.write(src)


if __name__ == "__main__":
    main()
