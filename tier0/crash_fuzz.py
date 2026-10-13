#!/usr/bin/env python3
"""crash_fuzz.py -- tier 0: black-box crash fuzzing of the TurfLang compiler.

Port of turflang's scripts/fuzz/crash_fuzz.py, with signature dedupe.

The bar: invalid input must produce diagnostics, never a crash.  A compiler
that exits 0 or 1 on a mutated program has behaved; anything else is a finding:

  * a sanitizer report (AddressSanitizer / UBSan "runtime error:"),
  * an LLVM assertion, "LLVM ERROR", "Stack dump", "Internal compiler error",
    an uncaught C++ exception,
  * killed by a signal (segfault, abort),
  * a hang (the wall-clock budget), unless the UNMUTATED seed hangs too
    (a slow test, not a bug),
  * any exit status other than 0/1 (exit 2 = the fuzzer built a bad command line).

Seeds are turflang's own programs ($TURF_ROOT/{tests,showcase,examples}); the
mutations are token-level and line-level (tier0/mutate.py).  A seed that
imports a sibling module still finds it: each seed directory is mirrored once
as a farm of symlinks in a scratch directory and the mutant is written beside
them.  tests/99_known_bugs (documented crashers), tests/custom, tests/api,
project directories (turf.toml) and `*_module.tr` companions are not seeds.

Dedupe is by SIGNATURE (triage/signature.py), not input hash: a hit whose
signature already has a findings/<class>/<hash>/ directory only bumps the
counter in its report.md and is not minimized again.  New signatures are
minimized (triage/minimize.py, same signature required) and written by
triage/findings.py.

  crash_fuzz.py --seconds 120 --jobs 8 --opt random
  crash_fuzz.py --turfc $TURF_ROOT/build_san/turfc --opt random
  crash_fuzz.py --replay findings/signal/0123456789ab/min.tr

Exit: 0 = no NEW finding; 1 = at least one new signature; 2 = harness failure.
"""
from __future__ import annotations

import argparse
import mmap
import os
import random
import shutil
import signal as _signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True

import mutate  # noqa: E402  (tier0/mutate.py)
from triage import findings as findings_mod  # noqa: E402
from triage import minimize as minimize_mod  # noqa: E402
from triage import signature as sigmod  # noqa: E402

SEED_ROOTS = ("tests", "showcase", "examples")
SKIP_DIRS = (("tests", "99_known_bugs"), ("tests", "custom"), ("tests", "api"))
OPTS = ["-O0", "-O1", "-O2", "-O3", "-Os", "-g"]
ASAN_STACK_MB = 65536 // 1024  # ASan frames overflow 8 MB before the parser's nesting cap


def turf_root() -> Path:
    return Path(os.environ.get("TURF_ROOT") or ROOT.parent / "turflang").resolve()


# ── running the compiler ────────────────────────────────────────────────────
def is_asan_binary(path: str) -> bool:
    """True if the binary links the AddressSanitizer runtime (needs a big stack)."""
    try:
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return mm.find(b"__asan_init") >= 0
    except (OSError, ValueError):
        return False


class Runner:
    def __init__(self, turfc: str, timeout: float, stack_mb: int, emit_obj: bool = False):
        self.turfc, self.timeout, self.stack_mb = turfc, timeout, stack_mb
        # --emit-obj: compile everything (codegen and LLVM's backend included)
        # but never run the system linker, which cost about half of every
        # compile and is not what this tier fuzzes.
        self.emit_obj = emit_obj
        self.env = dict(os.environ)
        self.env.setdefault("ASAN_OPTIONS", "detect_leaks=0:abort_on_error=0")
        # UBSan prints no stack unless asked; the signature wants the frames.
        self.env.setdefault("UBSAN_OPTIONS", "print_stacktrace=1")

    def command(self, src: Path, outbin: Path, opt: str) -> list[str]:
        cmd = [self.turfc, str(src), "-o", str(outbin)] + ([opt] if opt else [])
        if self.emit_obj:
            cmd.append("--emit-obj")
        if self.stack_mb:
            # Soft limit only, in a shell that then becomes the compiler.
            cmd = ["sh", "-c", f'ulimit -S -s {self.stack_mb * 1024} && exec "$@"', "sh"] + cmd
        return cmd

    def run(self, src: Path, outbin: Path, opt: str):
        """-> (rc | None for a hang, merged output)."""
        p = subprocess.Popen(self.command(src, outbin, opt), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=self.env, cwd=src.parent,
                             start_new_session=True)
        try:
            raw, _ = p.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, _signal.SIGKILL)
            except OSError:
                p.kill()
            p.communicate()
            return None, "timeout"
        return p.returncode, raw.decode("utf-8", "replace")

    def signature(self, src: Path, outbin: Path, opt: str):
        rc, out = self.run(src, outbin, opt)
        return rc, out, sigmod.compute(rc, out)


# ── seed farms ──────────────────────────────────────────────────────────────
def seeds(root: Path) -> list[Path]:
    out = []
    for sub in SEED_ROOTS:
        for p in sorted((root / sub).rglob("*.tr")):
            rel = p.relative_to(root).parts
            if rel[:2] in SKIP_DIRS:
                continue
            if p.name.endswith("_module.tr") or (p.parent / "turf.toml").exists():
                continue
            if p.stat().st_size > 60_000:
                continue
            out.append(p)
    return out


class Farms:
    """One symlink mirror per seed directory, created lazily and shared."""

    def __init__(self, base: Path):
        self.base, self.made, self.lock = base, {}, threading.Lock()

    def dir_for(self, seed: Path) -> Path:
        key = seed.parent
        with self.lock:
            if key not in self.made:
                d = self.base / f"farm{len(self.made)}"
                d.mkdir()
                for f in key.iterdir():
                    if f.is_file():
                        (d / f.name).symlink_to(f)
                self.made[key] = d
            return self.made[key]


# ── replay ──────────────────────────────────────────────────────────────────
def replay(args, runner: Runner) -> int:
    src = Path(args.replay).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="turf_fuzz_replay_"))
    try:
        if args.replay_beside:
            # Compile beside the seed's sibling modules, as the fuzzer did.
            seed = Path(args.replay_beside).resolve()
            for f in seed.parent.iterdir():
                if f.is_file():
                    (tmp / f.name).symlink_to(f)
            target = tmp / "__replay.tr"
            target.write_text(src.read_text(errors="replace"))
        else:
            target = src
        rc, out, sig = runner.signature(target, tmp / "a.out", args.opt)
        if sig:
            print(f"{src}: {sig}\n  hash {sig.key}")
            print(out[-2000:])
            return 1
        print(f"{src}: ok (exit {rc})")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── the fuzzer ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60, help="wall-clock budget (default 60)")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed (default: time-based, printed)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2),
                    help="parallel compiles (default: half the cores)")
    ap.add_argument("--turfc", "--compiler", dest="turfc", default=None,
                    help="compiler under test (default $TURF_ROOT/build/turfc; "
                         "use $TURF_ROOT/build_san/turfc for the sanitized build)")
    ap.add_argument("--out", default=str(ROOT / "findings"), help="findings root (default: turf-fuzz/findings)")
    ap.add_argument("--timeout", type=float, default=20, help="per-compile hang bound (default 20s)")
    ap.add_argument("--opt", default="", help='flag passed through, e.g. -O0 or -g; "random" picks one per compile')
    ap.add_argument("--link", action="store_true",
                    help="link every program (default: --emit-obj when the compiler has it, "
                         "turfc interface 1.6+: everything but the system linker runs)")
    ap.add_argument("--stack-mb", type=int, default=None,
                    help="soft stack limit for the compiler in MB (default: 64 if the binary is "
                         "ASan-instrumented, else unchanged)")
    ap.add_argument("--replay", help="compile one file, print its signature; no fuzzing")
    ap.add_argument("--replay-beside", metavar="SEED",
                    help="with --replay: compile it in a mirror of SEED's directory (sibling imports)")
    ap.add_argument("--max-iterations", type=int, default=0, help="stop after N compiles (0 = time only)")
    ap.add_argument("--keep-dups", metavar="DIR",
                    help="also write each duplicate hit's input and output here")
    ap.add_argument("--minimize-probes", type=int, default=400)
    ap.add_argument("--minimize-seconds", type=float, default=180)
    args = ap.parse_args()

    root = turf_root()
    turfc = args.turfc or str(root / "build" / "turfc")
    if not os.access(turfc, os.X_OK):
        print(f"fuzz: compiler {turfc} not built", file=sys.stderr)
        return 2
    stack_mb = args.stack_mb if args.stack_mb is not None else (ASAN_STACK_MB if is_asan_binary(turfc) else 0)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "oracles"))
    import differential  # noqa: E402  (the interface check lives there)
    emit_obj = not args.link and differential.emit_obj_supported(turfc)
    runner = Runner(turfc, args.timeout, stack_mb, emit_obj)

    if args.replay:
        return replay(args, runner)

    pool = seeds(root)
    if not pool:
        print(f"fuzz: no seeds found under {root}/{{{','.join(SEED_ROOTS)}}}", file=sys.stderr)
        return 2
    seed = args.seed if args.seed is not None else int(time.time())
    print(f"fuzz: {len(pool)} seeds, {args.jobs} jobs, {args.seconds:.0f}s, seed {seed}, "
          f"compiler {turfc}, stack {stack_mb or 'default'}"
          f"{' MB' if stack_mb else ''}, {'--emit-obj (no link)' if emit_obj else 'linking'}, "
          f"TURF_ROOT {root}", flush=True)

    kw = mutate.keywords(root)
    vocab = sorted({w for p in pool[:300] for k, w in mutate.tokenize(p.read_text(errors="replace"))
                    if k == "id"})[:3000]
    base = Path(tempfile.mkdtemp(prefix="turf_fuzz_"))
    farms = Farms(base)
    store = findings_mod.Findings(args.out, turf_root=root)
    started = time.time()
    deadline = started + args.seconds
    stats = {"runs": 0, "ok": 0, "diag": 0, "new": 0, "dup": 0, "slow_seed": 0}
    new_findings: list[Path] = []
    lock = threading.Lock()

    def worker(w: int):
        rng = random.Random(seed * 1000 + w)
        mut = mutate.Mutator(rng, vocab, kw)
        name = f"__fuzz_w{w}.tr"
        binname = f"__fuzz_w{w}.bin"
        while time.time() < deadline:
            if args.max_iterations and stats["runs"] >= args.max_iterations:
                return
            sp = rng.choice(pool)
            # "random" spreads the optimizer and the debug-info path across the
            # run: a crash in -O3 or -g code generation needs a program that
            # gets that far, which the default level never asks for.
            opt = rng.choice(OPTS) if args.opt == "random" else args.opt
            src = mut.mutate(sp.read_text(errors="replace"))
            farm = farms.dir_for(sp)
            (farm / name).write_text(src)
            rc, out, sig = runner.signature(farm / name, farm / binname, opt)
            with lock:
                stats["runs"] += 1
                if sig is None:
                    stats["ok" if rc == 0 else "diag"] += 1
            if sig is None:
                continue
            if sig.kind == "hang":
                # A seed that is itself slow (the E079 fan-out test takes ~20s)
                # is a slow test, not a fuzz finding.
                rc0, _ = runner.run(farm / sp.name, farm / binname, opt)
                if rc0 is None:
                    with lock:
                        stats["slow_seed"] += 1
                    continue
            cls = sig.kind
            seed_rel = sp.relative_to(root)
            cmd = ([turfc, "min.tr", "-o", "a.out"] + ([opt] if opt else [])
                   + (["--emit-obj"] if emit_obj else []))
            flags = (opt or "(default)") + (f"; ulimit -S -s {stack_mb * 1024}" if stack_mb else "")
            meta = {"command": cmd, "flags": flags, "seed_file": seed_rel, "fuzz_seed": seed,
                    "worker": w, "output": out}
            if not store.claim(cls, sig, src, meta):  # existing signature: counter bumped
                with lock:
                    stats["dup"] += 1
                    n_dup = stats["dup"]
                if args.keep_dups:
                    # A signature can be generic ("generated LLVM IR is
                    # invalid"): a duplicate may be a different bug, filed
                    # under a finding that is long fixed. Keep its input.
                    d = Path(args.keep_dups)
                    d.mkdir(parents=True, exist_ok=True)
                    stem = "%s-%d" % (sig.hash, n_dup)
                    (d / (stem + ".tr")).write_text(src)
                    (d / (stem + ".txt")).write_text(
                        "seed file: %s\ncommand: %s\n\n%s" % (seed_rel, " ".join(map(str, cmd)), out))
                continue
            def probe(candidate: str, farm=farm, name=name, binname=binname, opt=opt):
                (farm / name).write_text(candidate)
                return runner.signature(farm / name, farm / binname, opt)[2]

            small, probes = minimize_mod.minimize(
                src, minimize_mod.same_signature(probe, sig),
                max_probes=args.minimize_probes, max_seconds=args.minimize_seconds)
            (farm / name).write_text(small)
            _, out2, sig2 = runner.signature(farm / name, farm / binname, opt)
            note = f"minimized with {probes} probes."
            if sig2 is None or sig2.key != sig.key:
                small, out2 = src, out  # the reduced file is flaky; keep the unreduced input
                note += " The reduced file did not reproduce on re-check; min.tr is the unreduced input."
            meta["output"] = out2 or out
            meta["note"] = note
            store.finish(cls, sig, small, meta)
            target = store.dir_for(cls, sig)
            with lock:
                stats["new"] += 1
                new_findings.append(target)
            print(f"FINDING [{cls}] {sig.text[:110]}\n         {target}  (from {seed_rel})", flush=True)

    ex = ThreadPoolExecutor(max_workers=args.jobs)
    futs = [ex.submit(worker, w) for w in range(args.jobs)]
    last = started
    while not all(f.done() for f in futs):
        time.sleep(0.5)
        if time.time() - last >= 30:
            last = time.time()
            el = last - started
            print(f"fuzz: {el:.0f}s {stats['runs']} compiles ({stats['runs'] / el:.1f}/s), "
                  f"{stats['new']} new, {stats['dup']} dup", flush=True)
    for f in futs:
        f.result()
    ex.shutdown()
    shutil.rmtree(base, ignore_errors=True)

    el = max(time.time() - started, 1e-9)
    print(f"fuzz: {stats['runs']} compiles in {el:.0f}s ({stats['runs'] / el:.1f}/s): "
          f"{stats['ok']} ok, {stats['diag']} diagnostics, "
          f"{stats['new']} new finding(s), {stats['dup']} duplicate hit(s), "
          f"{stats['slow_seed']} slow-seed hang(s) ignored; seed {seed}")
    return 1 if new_findings else 0


if __name__ == "__main__":
    sys.exit(main())
