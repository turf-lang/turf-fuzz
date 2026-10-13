#!/usr/bin/env python3
"""Differential oracle, tier 2 of turf-fuzz.

For one generated program: compile at -O0 -O1 -O2 -O3 -Os and -g, run every
binary, and run the -O0 binary again under TURF_GC_STRESS=1, TURF_GC_VALIDATE=1
and both (these are runtime environment variables of the program, not compile
flags; see AGENTS.md in the compiler tree). Compare stdout and outcome across
all configurations (F-OPT-1 / INV-OPT-1) and against the reference evaluator.

When `$TURF_ROOT/build_asan/turfc` exists (a tree configured with
-DTURF_RT_SANITIZE=ON; override with TURFC_ASAN), each program is also
compiled by it at -O0 and -O2 and run plain and under TURF_GC_STRESS=1: the
same program against the runtime and collector built with ASan + UBSan.

Failure classes
  sanitizer        the ASan/UBSan runtime reported (a use-after-free in the
                   collector or an allocation the runtime overran)
  crash            the compiler crashed, or a binary died on a signal other
                   than a Turf panic's abort, hung, or TURF_GC_VALIDATE reported
  rejected-valid   the compiler refused a program the generator considers valid
  miscompile       configurations disagree with each other
  wrong-answer     all configurations agree, evaluator disagrees. Presume a
                   generator/evaluator bug first; the verdict carries the
                   evidence needed to decide.

    python3 oracles/differential.py SEED [--keep DIR] [--allow-undefined]
"""

import argparse
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gen"))

import generate  # noqa: E402

TURF_ROOT = os.environ.get("TURF_ROOT") or os.path.join(os.path.dirname(ROOT), "turflang")
TURFC = os.environ.get("TURFC") or os.path.join(TURF_ROOT, "build", "turfc")

# A second compiler, built with -DTURF_RT_SANITIZE=ON in build_asan/: the same
# front end, but every program it compiles links libturfrt_asan (the runtime,
# collector included, under ASan + UBSan). The program's own code is not
# instrumented; what this catches is the runtime touching memory it freed or
# never owned (a block collected while reachable and then scanned, copied,
# strlen'd or printed), which a plain build survives by luck. Skipped when the
# binary is absent (see AGENTS.md in the compiler tree for building it).
TURFC_ASAN = os.environ.get("TURFC_ASAN") or os.path.join(TURF_ROOT, "build_asan", "turfc")
ASAN_ENV = {"ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0", "UBSAN_OPTIONS": "print_stacktrace=0"}

# (name, compiler, flags)
COMPILE_CONFIGS = [("O0", TURFC, ["-O0"]), ("O1", TURFC, ["-O1"]), ("O2", TURFC, ["-O2"]),
                   ("O3", TURFC, ["-O3"]), ("Os", TURFC, ["-Os"]), ("g", TURFC, ["-g"])]
# (name, binary built by, extra environment)
RUN_CONFIGS = [(n, n, {}) for n, _, _ in COMPILE_CONFIGS] + [
    ("O0+gcstress", "O0", {"TURF_GC_STRESS": "1"}),
    ("O0+gcvalidate", "O0", {"TURF_GC_VALIDATE": "1"}),
    ("O0+gcstress+gcvalidate", "O0", {"TURF_GC_STRESS": "1", "TURF_GC_VALIDATE": "1"}),
]
ASAN_COMPILE_CONFIGS = [("asanO0", TURFC_ASAN, ["-O0"]), ("asanO2", TURFC_ASAN, ["-O2"])]
ASAN_RUN_CONFIGS = [
    ("asanO0", "asanO0", ASAN_ENV),
    ("asanO0+gcstress", "asanO0", dict(ASAN_ENV, TURF_GC_STRESS="1")),
    ("asanO2+gcstress", "asanO2", dict(ASAN_ENV, TURF_GC_STRESS="1")),
]


def asan_available():
    return os.path.exists(TURFC_ASAN)


_EMIT_OBJ = {}


def emit_obj_supported(turfc=None):
    """Does this turfc take `--emit-obj` (write the object, never link)? It
    arrived with driver interface 1.6 (turflang src/driver/main.cpp); an older
    compiler, such as one built at an earlier commit to reproduce a finding,
    would reject the flag as a usage error (exit 2)."""
    turfc = turfc or TURFC
    if turfc not in _EMIT_OBJ:
        ok = False
        try:
            out = subprocess.run([turfc, "--version"], capture_output=True, text=True,
                                 timeout=30).stdout
            m = re.search(r"^interface (\d+)\.(\d+)", out, re.M)
            ok = bool(m) and (int(m.group(1)), int(m.group(2))) >= (1, 6)
        except Exception:
            pass
        _EMIT_OBJ[turfc] = ok
    return _EMIT_OBJ[turfc]

COMPILE_TIMEOUT = 120
RUN_TIMEOUT = 10

PANIC_RE = re.compile(r"panic: runtime check: (.+?) \(([^():]+):(\d+)\)")
# Stdlib panics (sys::panic) use other shapes: `prog.tr:12: List.pop: ...`
# with the caller's line, or a bare `List[]: index out of bounds` with none
# (line 0). See gen/SEMANTICS.md, Output.
STDLIB_PANIC_RE = re.compile(r"^(?:prog\.tr:(\d+): )?((?:List|Map|Set|Stack|Queue|strings)[\w.\[\]=]*: .+)$", re.M)
COMPILER_CRASH_RE = re.compile(
    r"Internal compiler error|LLVM ERROR|Assertion .* failed|AddressSanitizer|"
    r"Stack dump|PLEASE submit a bug report|UndefinedBehaviorSanitizer|"
    r"terminate called|Segmentation fault", re.I)
SANITIZER_RE = re.compile(
    r"(ERROR: AddressSanitizer: [^\n]*|SUMMARY: (?:Address|UndefinedBehavior)Sanitizer: [^\n]*"
    r"|[^\n]*: runtime error: [^\n]*)")
ERROR_CODE_RE = re.compile(r"\[(E\d{3})")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def lock_commit():
    try:
        for line in open(os.path.join(ROOT, "compiler.lock")):
            if line.startswith("commit"):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def tree_head():
    try:
        return subprocess.run(["git", "-C", TURF_ROOT, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return "unknown"


# --------------------------------------------------------------------- running

def _run(cmd, cwd, env=None, timeout=60):
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout.decode("latin-1"), p.stderr.decode("latin-1"), False
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("latin-1")
        err = (e.stderr or b"").decode("latin-1")
        return None, out, err, True


class Outcome:
    """What one run of one binary did, reduced to what must be identical."""

    def __init__(self, rc, stdout, stderr, timed_out):
        self.rc, self.stdout, self.stderr, self.timed_out = rc, stdout, stderr, timed_out
        self.gc_validate = [l for l in stderr.splitlines() if l.startswith("TURF_GC_VALIDATE:")]
        m = PANIC_RE.search(stderr)
        san = SANITIZER_RE.search(stderr)
        if timed_out:
            self.kind, self.detail = "timeout", ()
        elif san:
            # Whatever the exit status: a sanitizer report is the outcome.
            self.kind, self.detail = "sanitizer", (san.group(1).strip(),)
        elif rc is not None and rc >= 0:
            self.kind, self.detail = "exit", (rc,)
        elif rc == -signal.SIGABRT and m:
            self.kind, self.detail = "trap", (m.group(1), int(m.group(3)))
        elif rc == -signal.SIGABRT and STDLIB_PANIC_RE.search(stderr):
            s = STDLIB_PANIC_RE.search(stderr)
            self.kind, self.detail = "trap", (s.group(2), int(s.group(1) or 0))
        else:
            self.kind, self.detail = "signal", (-rc,)

    def key(self):
        return (self.stdout, self.kind, self.detail)

    def short(self):
        if self.kind == "exit":
            what = "exit %d" % self.detail
        elif self.kind == "trap":
            what = "trap %s line %d" % self.detail
        elif self.kind == "signal":
            what = "signal %d (%s)" % (self.detail[0], _signame(self.detail[0]))
        elif self.kind == "sanitizer":
            what = "sanitizer: %s" % self.detail[0][:120]
        else:
            what = "timeout"
        return "%s, stdout %d bytes" % (what, len(self.stdout))


def _signame(n):
    try:
        return signal.Signals(n).name
    except ValueError:
        return "?"


def expected_key(res):
    if res.kind == "exit":
        return (res.stdout, "exit", (res.code,))
    return (res.stdout, "trap", (res.trap, res.line))


class Verdict:
    def __init__(self, seed):
        self.seed = seed
        self.cls = None          # None means clean
        self.sig = ""            # dedupe signature
        self.summary = ""
        self.compile = {}        # config -> (rc, output)
        self.runs = {}           # config -> Outcome
        self.expected = None
        self.expected_text = ""
        self.note = ""

    def ok(self):
        return self.cls is None


def _compile_diag(output):
    plain = ANSI_RE.sub("", output)
    codes = ERROR_CODE_RE.findall(plain)
    msg = ""
    lines = plain.splitlines()
    for i, l in enumerate(lines):
        if "error at" in l:
            for m in lines[i + 1:i + 6]:
                if m.strip():
                    msg = m.strip()
                    break
            break
    return (codes[0] if codes else "?"), msg


def check_source(seed, source_path, workdir, expected, quick=False, asan=True, only=None,
                 compile_only=False):
    """Run all configurations on source_path (copied to workdir/prog.tr).
    asan adds the ASan-runtime configurations when that compiler exists; with
    quick it is the -O0 binary plus those (the GC/ownership view of a seed).
    only, a list of run configuration names, overrides both: just those runs
    and the compiles they need (the reducer's cheap re-check of a verdict).
    compile_only compiles with `--emit-obj` (no link) and runs nothing: for a
    verdict decided by the compiler alone (rejected-valid, a compiler crash),
    where a linked binary would never be run."""
    v = Verdict(seed)
    v.expected = expected
    v.expected_text = generate.format_expected(expected)
    prog = os.path.join(workdir, "prog.tr")
    if os.path.abspath(source_path) != os.path.abspath(prog):
        shutil.copy(source_path, prog)

    binaries = {}
    compile_configs = COMPILE_CONFIGS[:1] if quick else COMPILE_CONFIGS
    run_configs = RUN_CONFIGS[:1] if quick else RUN_CONFIGS
    if asan and asan_available():
        compile_configs = compile_configs + ASAN_COMPILE_CONFIGS
        run_configs = run_configs + ASAN_RUN_CONFIGS
    if only is not None:
        every_run = RUN_CONFIGS + ASAN_RUN_CONFIGS
        run_configs = [r for r in every_run if r[0] in only]
        needed = {r[1] for r in run_configs} | {n for n in only
                                                  if n in {c[0] for c in COMPILE_CONFIGS + ASAN_COMPILE_CONFIGS}}
        compile_configs = [c for c in COMPILE_CONFIGS + ASAN_COMPILE_CONFIGS if c[0] in needed]
    if compile_only:
        run_configs = []
    for name, compiler, flags in compile_configs:
        out = "bin_" + name
        if compile_only and emit_obj_supported(compiler):
            flags = flags + ["--emit-obj"]
        rc, so, se, to = _run([compiler, "prog.tr", "-o", out] + flags, workdir, timeout=COMPILE_TIMEOUT)
        text = so + se
        v.compile[name] = (rc, text if rc != 0 or to else "")
        if to:
            v.cls, v.sig = "crash", "compile timeout"
            v.summary = "compile timed out at %s" % name
            return v
        if rc is not None and (rc >= 128 or rc < 0 or COMPILER_CRASH_RE.search(text)):
            v.cls = "crash"
            first = COMPILER_CRASH_RE.search(text)
            v.sig = "compile crash rc=%s %s" % (rc, first.group(0) if first else "")
            v.summary = "compiler crashed at %s (rc %s)" % (name, rc)
            return v
        if rc != 0:
            code, msg = _compile_diag(text)
            v.cls = "rejected-valid"
            v.sig = "rejected %s %s" % (code, re.sub(r"\d+", "N", msg))
            v.summary = "%s rejected at %s: %s %s" % (os.path.basename(source_path), name, code, msg)
            return v
        binaries[name] = os.path.join(workdir, out)

    for name, built, extra in run_configs:
        env = dict(os.environ)
        env.update(extra)
        # ASan plus a collection per allocation is slow: three times the budget.
        limit = RUN_TIMEOUT * 3 if built.startswith("asan") else RUN_TIMEOUT
        rc, so, se, to = _run([binaries[built]], workdir, env=env, timeout=limit)
        v.runs[name] = Outcome(rc, so, se, to)

    # 0. a sanitizer report from the runtime, in any configuration
    san = [(n, o) for n, o in v.runs.items() if o.kind == "sanitizer"]
    if san:
        n, o = san[0]
        v.cls = "sanitizer"
        v.sig = "sanitizer " + re.sub(r"0x[0-9a-f]+|\d+", "N", o.detail[0])
        v.summary = "%s: %s" % (",".join(x for x, _ in san), o.detail[0][:200])
        return v

    # 1. configurations disagree (a crash in some of them is still a miscompile)
    keys = {n: o.key() for n, o in v.runs.items()}
    groups = {}
    for n, k in keys.items():
        groups.setdefault(k, []).append(n)
    if len(groups) > 1:
        v.cls = "miscompile"
        ordered = sorted(groups.values(), key=lambda g: (-len(g), g))
        # The signature is coarse on purpose: stdout may hold run-to-run garbage
        # (uninitialized reads), so group by outcome kind/status, not by bytes.
        kg = {}
        for n, o in v.runs.items():
            kg.setdefault((o.kind, o.detail), []).append(n)
        parts = sorted(",".join(sorted(g)) + "=" + k[0] + str(k[1][:1]) for k, g in kg.items())
        agree = [n for n, o in v.runs.items() if o.key() == expected_key(expected)]
        v.sig = "miscompile " + " | ".join(parts) + " evaluator-agrees=" + ",".join(sorted(agree))
        v.summary = " vs ".join("{%s}: %s" % (",".join(sorted(g)), v.runs[g[0]].short()) for g in ordered)
        return v

    # 2. every configuration agrees but died on a signal / hung, or GC validate spoke
    bad = [(n, o) for n, o in v.runs.items() if o.kind in ("signal", "timeout")]
    if bad:
        v.cls = "crash"
        o0 = bad[0][1]
        v.sig = "runtime %s%s" % (o0.kind, o0.detail)
        v.summary = "all configurations: %s" % o0.short()
        return v
    gv = [(n, o) for n, o in v.runs.items() if o.gc_validate]
    if gv:
        v.cls = "crash"
        v.sig = "gc-validate " + re.sub(r"\d+", "N", gv[0][1].gc_validate[0])
        v.summary = "%s: %s" % (gv[0][0], gv[0][1].gc_validate[0])
        return v

    # 3. all agree; compare to the evaluator (nothing to compare when
    # compile_only ran no binary: compiling cleanly is the whole verdict)
    if not v.runs:
        return v
    got = next(iter(v.runs.values()))
    if got.key() != expected_key(expected):
        v.cls = "wrong-answer"
        ek = expected_key(expected)
        v.sig = "wrong-answer expected %s%s got %s%s" % (ek[1], ek[2][:1], got.kind, got.detail[:1])
        v.summary = "expected %s, stdout %d bytes; compiler (all configs) gave %s" % (
            expected.describe(), len(expected.stdout), got.short())
        v.note = ("all %d configurations agree and disagree with the evaluator: "
                  "presume a generator/evaluator bug until minimized against the docs" % len(v.runs))
    return v


def sig_hash(cls, sig):
    return hashlib.sha1((cls + "\n" + sig).encode()).hexdigest()[:10]


def check_seed(seed, workdir, allow_undefined=False, quick=False, asan=None):
    if asan is None:
        asan = not quick  # --quick stays the one-binary generator check
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    prog, res, attempt = generate.generate(seed, allow_undefined)
    path = os.path.join(workdir, "prog.tr")
    with open(path, "w") as f:
        f.write(prog.source)
    if res is None:  # undefined by the language: only the configs can be compared
        class _Any:
            kind, code, trap, line, stdout = "exit", 0, "", 0, ""
            def describe(self): return "undefined"
        res = _Any()
    v = check_source(seed, path, workdir, res, quick, asan)
    v.attempt = attempt
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=int)
    ap.add_argument("--keep", help="work in (and keep) this directory")
    ap.add_argument("--allow-undefined", action="store_true")
    ap.add_argument("--quick", action="store_true", help="-O0 only (generator debugging)")
    ap.add_argument("--asan", action="store_true",
                    help="with --quick: also the ASan-runtime configurations")
    ap.add_argument("--no-asan", action="store_true", help="never the ASan-runtime configurations")
    a = ap.parse_args()
    wd = a.keep or tempfile.mkdtemp(prefix="tfz_", dir=os.path.join(ROOT, "work")
                                    if os.path.isdir(os.path.join(ROOT, "work")) else None)
    asan = False if a.no_asan else True if a.asan else None
    v = check_seed(a.seed, wd, a.allow_undefined, a.quick, asan)
    print("seed %d: %s" % (a.seed, v.cls or "ok"))
    if v.cls:
        print("  " + v.summary)
        if v.note:
            print("  " + v.note)
        print("  sig: " + v.sig)
        print("  dir: " + wd)
    elif not a.keep:
        shutil.rmtree(wd, ignore_errors=True)
    sys.exit(1 if v.cls else 0)


if __name__ == "__main__":
    main()
