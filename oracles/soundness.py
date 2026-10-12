#!/usr/bin/env python3
"""Soundness oracle for F-MEM-5 (turflang FREEZE §6.13), tier 3 of turf-fuzz.

Generates small programs at the escape boundary: a reference to a struct
from some SOURCE reaches some SINK, with some control flow WRAPPED around the
sink. Every program has a pinned verdict:

  stack source (this frame's storage, `&n` and things derived from it)
      -> rejected, E086, with the sink's own wording. Accepted is a finding
         (`unsound-accepted`; the program is then run to show what it read),
         any other rejection is `wrong-reason`.
  safe source (heap(), a reference the caller handed in)
      -> accepted, and prints exactly "42" at -O0, at -O2 and, when
         build_asan/turfc exists, under the ASan runtime with a collection on
         every allocation. Rejected is `rejected-valid`, anything else
         `safe-wrong`.

Between the escape and the read, main calls clobber(), which overwrites the
stack where the dead frame was, so a dangling read prints garbage instead of
a lucky 42.

Shapes the compiler is documented not to catch (FDN-002 Known pitfalls) or
whose verdict is an open owner decision are in GAPS; `--gaps` runs them too
and reports what each does, never as a finding. When one is decided or
closed, move it into the generated set with its verdict.

    python3 oracles/soundness.py [--jobs 8] [--gaps] [--only SUBSTR] [--keep]

Rules and sources: gen/SEMANTICS.md, "Escape boundary (F-MEM-5)".
"""

import argparse
import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import differential as D  # noqa: E402

PRELUDE = """import io;
import list::List;
import map::Map;
import stack::Stack;
import queue::Queue;

struct N { val: int, next: &?N }

fn clobber(d: int): int {
    mut buf: int[64];
    buf[0] = 0;
    mut i: int = 0;
    while i < 64 {
        buf[i] = 7777 + i;
        i = i + 1;
    }
    if d > 0 {
        return clobber(d - 1) + buf[d];
    }
    return buf[5];
}
"""

# ----------------------------------------------------------------- sources
# name -> (safe?, nullable?, decls, value). decls run at the top of the
# escaping function, after `n` (val 42) and `n2` exist; value is the
# expression handed to the sink. A nullable value only fits a sink that
# takes `&?N`.
SOURCES = {
    "addr":      (False, False, [], "&n"),
    "var":       (False, False, ["mut p: &N = &n;"], "p"),
    "nvar":      (False, True, ["mut q: &?N = &n;"], "q"),
    "give":      (False, False, [], "(n.val > 0 give &n otherwise &n2)"),
    "var2":      (False, False, ["mut p: &N = &n2;", "p = &n;"], "p"),
    "heap":      (True, False, [], "heap(n)"),
    "heapvar":   (True, False, ["mut p: &N = heap(n);"], "p"),
    "nheapvar":  (True, True, ["mut q: &?N = heap(n);"], "q"),
    "giveheap":  (True, False, [], "(n.val > 0 give heap(n) otherwise heap(n2))"),
    "given":     (True, False, [], "given"),  # a `&N` parameter from main
}

# ------------------------------------------------------------------- sinks
# name -> dict(params, ret, stmt(value), tail, call(args), read, takes,
#              msg). `takes` is "ref" (a `&N` only), or "nref" (`&?N` or a
# `&N`, which widens). msg is pinned in E086's text.
SINKS = {
    "return": dict(ret="&N", stmt="return {v};", tail="return heap(n);", takes="ref",
                   setup="", call="mut r: &N = esc({a});",
                   read="io::printline(r.val);", msg="can't be returned"),
    "return_nullable": dict(ret="&?N", stmt="return {v};", tail="return null;", takes="nref",
                            setup="", call="mut r: &?N = esc({a});",
                            read="if (r != null) { io::printline(r.val); }",
                            msg="can't be returned"),
    "param_field": dict(params=["dst: &N"], ret="void", stmt="dst.next = {v};", tail="",
                        takes="nref", setup="mut dst: &N = heap(N::default());",
                        call="esc(dst{a});",
                        read="mut r: &?N = dst.next; if (r != null) { io::printline(r.val); }",
                        msg="can't be stored into a field"),
    "heap_field": dict(ret="&N", stmt="h.next = {v};", tail="return h;", takes="nref",
                       pre=["mut h: &N = heap(N::default());"], setup="",
                       call="mut dst: &N = esc({a});",
                       read="mut r: &?N = dst.next; if (r != null) { io::printline(r.val); }",
                       msg="can't be stored into a field"),
    "self_field": dict(method=True, ret="void", stmt="self.next = {v};", tail="", takes="nref",
                       setup="mut dst: &N = heap(N::default());", call="dst.esc({a});",
                       read="mut r: &?N = dst.next; if (r != null) { io::printline(r.val); }",
                       msg="can't be stored into a field"),
    "list_push": dict(params=["l: List<&N>"], ret="void", stmt="l.push({v});", tail="",
                      takes="ref", setup="mut l: List<&N> = List::default();",
                      call="esc(l{a});", read="mut r: &N = l[0]; io::printline(r.val);",
                      msg="can't be pushed into a collection"),
    "list_set": dict(params=["l: List<&N>"], ret="void", stmt="l[0] = {v};", tail="",
                     takes="ref",
                     setup="mut l: List<&N> = List::default(); l.push(heap(N::default()));",
                     call="esc(l{a});", read="mut r: &N = l[0]; io::printline(r.val);",
                     msg="can't be pushed into a collection"),
    "map_put": dict(params=["m: Map<int, &N>"], ret="void", stmt="m.put(1, {v});", tail="",
                    takes="ref", setup="mut m: Map<int, &N> = Map::default();",
                    call="esc(m{a});", read="mut r: &N = m[1]; io::printline(r.val);",
                    msg="can't be pushed into a collection"),
    "map_set": dict(params=["m: Map<int, &N>"], ret="void", stmt="m[1] = {v};", tail="",
                    takes="ref", setup="mut m: Map<int, &N> = Map::default();",
                    call="esc(m{a});", read="mut r: &N = m[1]; io::printline(r.val);",
                    msg="can't be pushed into a collection"),
    "stack_push": dict(params=["s: Stack<&N>"], ret="void", stmt="s.push({v});", tail="",
                       takes="ref", setup="mut s: Stack<&N> = Stack::default();",
                       call="esc(s{a});", read="mut r: &N = s.peek(); io::printline(r.val);",
                       msg="can't be pushed into a collection"),
    "queue_enqueue": dict(params=["s: Queue<&N>"], ret="void", stmt="s.enqueue({v});", tail="",
                          takes="ref", setup="mut s: Queue<&N> = Queue::default();",
                          call="esc(s{a});", read="mut r: &N = s.peek(); io::printline(r.val);",
                          msg="can't be pushed into a collection"),
    "list_nullable_push": dict(params=["l: List<&?N>"], ret="void", stmt="l.push({v});",
                               tail="", takes="nref",
                               setup="mut l: List<&?N> = List::default();",
                               call="esc(l{a});",
                               read="mut r: &?N = l[0]; if (r != null) { io::printline(r.val); }",
                               msg="can't be pushed into a collection"),
}

# ---------------------------------------------------------------- wrappers
# How the sink statement sits in the function body. None of them assigns the
# source, so the value at the sink is the source's on every path, and F-MEM-5
# applies whatever the control flow (the pass is flow-sensitive: these probe
# its merges and loops).
WRAPPERS = {
    "plain":       lambda s: [s],
    "loop_before": lambda s: ["mut k: int = 0;", "while k < 3 {", "    k += 1;", "}", s],
    "for_before":  lambda s: ["for i in 0..3 {", "    n2.val += i;", "}", s],
    "in_while":    lambda s: ["mut k: int = 0;", "while k < 2 {", "    k += 1;", "    " + s, "}"],
    "in_for":      lambda s: ["for i in 0..2 {", "    " + s, "}"],
    "in_if":       lambda s: ["if n.val > 0 {", "    " + s, "}"],
    "in_else":     lambda s: ["if n.val < 0 {", "    n2.val = 1;", "} else {", "    " + s, "}"],
    "in_block":    lambda s: ["if true {", "    mut z: int = 1;", "    " + s, "}"],
    "after_if":    lambda s: ["if n.val > 100 {", "    n2.val = 3;", "}", s],
    "in_compare":  lambda s: ["compare (n.val) {", "    matches 42:", "        " + s,
                              "    done", "    otherwise:", "        n2.val = 2;", "}"],
    "nested_loops": lambda s: ["for i in 0..2 {", "    mut k: int = 0;", "    while k < 2 {",
                               "        k += 1;", "        " + s, "    }", "}"],
}

# Wrappers that put the sink inside a branch or a loop body.
NESTED = {"in_while", "in_for", "in_if", "in_else", "in_block", "in_compare", "nested_loops"}

# ------------------------------------------------------- known open shapes
# (name, what it is, escaping function + main body). The value read should be
# 42; a dangling reference reads something else. None is a finding here:
# each is a documented FDN-002 residual or an open owner question (see
# findings/unsound/escape-boundary/report.md).
GAPS = [
    ("may_merge_cond", "one path Stack, the other Heap (owner: may vs must)",
     "fn esc(c: int): &N {\n    mut n: N = N::default();\n    n.val = 42;\n"
     "    mut p: &N = heap(n);\n    if c > 0 {\n        p = &n;\n    }\n    return p;\n}\n",
     "mut r: &N = esc(1);"),
    ("may_merge_give", "give with a Stack and a Heap branch (owner: may vs must)",
     "fn esc(c: int): &N {\n    mut n: N = N::default();\n    n.val = 42;\n"
     "    return (c > 0 give &n otherwise heap(n));\n}\n", "mut r: &N = esc(1);"),
    ("payload", "a frame reference in an error payload (not a listed sink)",
     "error E1 { P(r: &N) }\nfn fail(): !int {\n    mut n: N = N::default();\n    n.val = 42;\n"
     "    return E1::P(&n);\n}\nfn esc(c: int): &N {\n"
     "    mut v: int = fail() recover as err run {\n        compare (err) {\n"
     "            matches E1::P(r):\n                return r;\n            done\n"
     "            otherwise:\n                return heap(N::default());\n        }\n"
     "        return heap(N::default());\n    };\n    return heap(N::default());\n}\n",
     "mut r: &N = esc(1);"),
    ("call_result_base", "field store through a call result (Unknown base)",
     "fn mk(): &N {\n    return heap(N::default());\n}\nfn esc(c: int): &N {\n"
     "    mut n: N = N::default();\n    n.val = 42;\n    mut h: &N = mk();\n"
     "    h.next = &n;\n    mut r: &?N = h.next;\n    if (r != null) {\n"
     "        return (r to &N);\n    }\n    return h;\n}\n", "mut r: &N = esc(1);"),
    ("struct_copy", "documented: inside a by-value struct copy",
     "struct H { head: &?N }\nfn esc(c: int): &N {\n    mut n: N = N::default();\n"
     "    n.val = 42;\n    mut h: H = H::default();\n    h.head = &n;\n"
     "    mut g: H = h;\n    mut r: &?N = g.head;\n    if (r != null) {\n"
     "        mut s: &N = (r to &N);\n        return heap(N::default());\n    }\n"
     "    return heap(N::default());\n}\n"
     "fn esc2(c: int): H {\n    mut n: N = N::default();\n    n.val = 42;\n"
     "    mut h: H = H::default();\n    h.head = &n;\n    return h;\n}\n",
     "mut h: H = esc2(1);\n    mut q: &?N = h.head;\n    mut r: &N = heap(N::default());\n"
     "    if (q != null) {\n        r = (q to &N);\n    }"),
    ("via_call", "documented: through an intermediate call",
     "fn id(p: &N): &N {\n    return p;\n}\nfn esc(c: int): &N {\n    mut n: N = N::default();\n"
     "    n.val = 42;\n    return id(&n);\n}\n", "mut r: &N = esc(1);"),
    ("array_element", "documented: &arr[i]",
     "fn esc(c: int): &N {\n    mut a: N[2] = [N::default(), N::default()];\n"
     "    a[0].val = 42;\n    return &a[0];\n}\n", "mut r: &N = esc(1);"),
    ("heap_of_struct_with_ref", "documented: heap(m) where m holds &n",
     "fn esc(c: int): &N {\n    mut n: N = N::default();\n    n.val = 42;\n"
     "    mut m: N = N::default();\n    m.next = &n;\n    mut h: &N = heap(m);\n"
     "    mut r: &?N = h.next;\n    if (r != null) {\n        return (r to &N);\n    }\n"
     "    return h;\n}\n", "mut r: &N = esc(1);"),
    ("defer_false_positive", "rejected, though the return value is computed before the defer",
     "fn esc(c: int): &N {\n    mut n: N = N::default();\n    n.val = 42;\n"
     "    mut p: &N = heap(n);\n    defer p = &n;\n    return p;\n}\n", "mut r: &N = esc(1);"),
]


def build_case(src, sink, wrap):
    """(source text, expected) for one combination, or None if it does not
    type: a nullable value at a sink that takes exactly `&N`."""
    safe, nullable, decls, value = SOURCES[src]
    sk = SINKS[sink]
    if nullable and sk["takes"] == "ref":
        return None
    params = list(sk.get("params", []))
    arg_extra = ""
    if src == "given":
        params.append("given: &N")
        arg_extra = "given"
    body = ["mut n: N = N::default();", "n.val = 42;", "mut n2: N = N::default();",
            "n2.val = 43;"]
    body += sk.get("pre", []) + decls
    body += WRAPPERS[wrap](sk["stmt"].format(v=value))
    # A `return` sink in straight-line code is the function's last word; its
    # fallback after it would be unreachable (E012). Inside a branch or a
    # loop the fallback is what the other paths return.
    sink_returns = sk["stmt"].startswith("return")
    if sk["tail"] and (not sink_returns or wrap in NESTED):
        body.append(sk["tail"])
    ret = sk["ret"]
    head = ("fn N.esc(%s): %s {" if sk.get("method") else "fn esc(%s): %s {") % (
        ", ".join(params), ret)
    fn = head + "\n" + "".join("    " + l + "\n" for l in body) + "}\n"
    call = sk["call"]
    # `esc({a})` takes the extra argument alone, `esc(l{a})` after another.
    if "({a})" in call:
        args = arg_extra
    else:
        args = (", " + arg_extra) if arg_extra else ""
    main = []
    if src == "given":
        main.append("mut gn: N = N::default();")
        main.append("gn.val = 42;")
        main.append("mut given: &N = heap(gn);")
    if sk["setup"]:
        main.append(sk["setup"])
    main.append(call.format(a=args))
    main.append("clobber(3);")
    main.append(sk["read"])
    text = (PRELUDE + "\n" + fn + "\nfn main(): int {\n"
            + "".join("    " + l + "\n" for l in main) + "    return 0;\n}\n")
    return text, ("accept" if safe else "reject"), sk["msg"]


def gap_case(name, fn, main_head):
    text = (PRELUDE + "\n" + fn + "\nfn main(): int {\n    " + main_head
            + "\n    clobber(3);\n    io::printline(r.val);\n    return 0;\n}\n")
    return text


# ------------------------------------------------------------------ running

def compile_(compiler, wd, flags, out):
    return D._run([compiler, "prog.tr", "-o", out] + flags, wd, timeout=D.COMPILE_TIMEOUT)


def run_bin(wd, binary, env=None, timeout=30):
    e = dict(os.environ)
    e.update(env or {})
    rc, so, se, to = D._run([os.path.join(wd, binary)], wd, env=e, timeout=timeout)
    return rc, so, se, to


def diag(text):
    plain = D.ANSI_RE.sub("", text)
    codes = D.ERROR_CODE_RE.findall(plain)
    return (codes[0] if codes else "?"), plain


def check(name, text, expected, msg, keep_dir):
    wd = tempfile.mkdtemp(prefix="snd_", dir=keep_dir)
    with open(os.path.join(wd, "prog.tr"), "w") as f:
        f.write(text)
    rc, so, se, to = compile_(D.TURFC, wd, ["-O0"], "b0")
    out = so + se
    if to or (rc is not None and (rc >= 128 or rc < 0)) or D.COMPILER_CRASH_RE.search(out):
        return name, "crash", out[-300:], wd
    if expected == "reject":
        if rc == 0:
            r = run_bin(wd, "b0")
            got = (r[1] + r[2]).strip().replace("\n", " ")[:60]
            return name, "unsound-accepted", "ran: %s" % got, wd
        code, plain = diag(out)
        if code != "E086" or msg not in plain:
            return name, "wrong-reason", "%s %s" % (code, plain.strip().splitlines()[2:4]), wd
        shutil.rmtree(wd, ignore_errors=True)
        return name, "ok", "", None
    if rc != 0:
        code, plain = diag(out)
        return name, "rejected-valid", code, wd
    runs = [("O0", "b0", {})]
    if compile_(D.TURFC, wd, ["-O2"], "b2")[0] == 0:
        runs.append(("O2", "b2", {}))
    if D.asan_available() and compile_(D.TURFC_ASAN, wd, ["-O0"], "ba")[0] == 0:
        runs.append(("asanO0+gcstress", "ba", dict(D.ASAN_ENV, TURF_GC_STRESS="1")))
    for cfg, b, env in runs:
        rc, so, se, to = run_bin(wd, b, env, 90 if cfg.startswith("asan") else 30)
        if rc != 0 or so != "42\n" or D.SANITIZER_RE.search(se):
            return name, "safe-wrong", "%s rc=%s out=%r err=%s" % (
                cfg, rc, so[:40], se.strip()[:120]), wd
    shutil.rmtree(wd, ignore_errors=True)
    return name, "ok", "", None


def check_gap(name, what, text, keep_dir):
    wd = tempfile.mkdtemp(prefix="gap_", dir=keep_dir)
    with open(os.path.join(wd, "prog.tr"), "w") as f:
        f.write(text)
    rc, so, se, to = compile_(D.TURFC, wd, ["-O0"], "b0")
    if rc != 0:
        code, _ = diag(so + se)
        res = "rejected %s" % code
    else:
        r = run_bin(wd, "b0")
        res = "accepted, printed %s" % (r[1] + r[2]).strip().replace("\n", " ")[:40]
    shutil.rmtree(wd, ignore_errors=True)
    return name, what, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--gaps", action="store_true", help="also report the known open shapes")
    ap.add_argument("--only", help="only combinations whose name contains this")
    ap.add_argument("--keep", action="store_true", help="keep every failing case's directory")
    a = ap.parse_args()
    jobs = max(1, min(a.jobs, 8))  # owner's budget: half the cores

    work = os.path.join(ROOT, "work", "soundness")
    os.makedirs(work, exist_ok=True)
    cases = []
    for src, sink, wrap in itertools.product(SOURCES, SINKS, WRAPPERS):
        name = "%s/%s/%s" % (src, sink, wrap)
        if a.only and a.only not in name:
            continue
        got = build_case(src, sink, wrap)
        if got:
            cases.append((name,) + got)
    print("compiler: %s  asan runtime: %s" % (
        D.TURFC, D.TURFC_ASAN if D.asan_available() else "not built"))
    print("%d cases (%d reject, %d accept)" % (
        len(cases), sum(c[2] == "reject" for c in cases), sum(c[2] == "accept" for c in cases)))
    bad = []
    with ThreadPoolExecutor(jobs) as ex:
        for name, verdict, detail, wd in ex.map(lambda c: check(*c, work), cases):
            if verdict != "ok":
                bad.append((name, verdict, detail, wd))
    by = {}
    for b in bad:
        by.setdefault(b[1], []).append(b)
    print("ok: %d   %s" % (len(cases) - len(bad), ", ".join(
        "%s: %d" % (k, len(v)) for k, v in sorted(by.items())) or "no failures"))
    for name, verdict, detail, wd in bad:
        print("  %-16s %-44s %s\n      %s" % (verdict, name, detail, wd))
        if not a.keep and wd:
            pass  # kept for inspection; work/ is git-ignored
    if a.gaps:
        print("\nknown open shapes (not findings; see findings/unsound/escape-boundary):")
        for name, what, fn, mh in GAPS:
            n, w, res = check_gap(name, what, gap_case(name, fn, mh), work)
            print("  %-26s %-40s %s" % (n, res, w))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
