#!/usr/bin/env python3
"""Loop driver: generate programs, run the differential oracle, record findings.

    python3 oracles/run.py --seconds 300 --jobs 8 --seed 1000

Seeds are seed, seed+1, ...; a finding is reproduced exactly by
`python3 oracles/differential.py SEED` against the same compiler commit.

Findings go to findings/<class>/<short-hash>/ (one directory per distinct
signature, see differential.py; later occurrences only bump `count` in
meta.json). At most --cap new directories are written per class per run.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "gen"))

import differential as D  # noqa: E402
import generate  # noqa: E402

MAX_JOBS = 8  # owner's budget: half the cores, never more


def turfc_version():
    try:
        p = subprocess.run([D.TURFC, "--version"], capture_output=True, text=True, timeout=20)
        return (p.stdout + p.stderr).strip().replace("\n", " | ")
    except Exception:
        return "unknown"


def write(path, text):
    with open(path, "w", errors="replace") as f:
        f.write(text)


class Recorder:
    def __init__(self, cap, findings_dir):
        self.cap = cap
        self.dir = findings_dir
        self.new_per_class = Counter()
        self.seen = Counter()      # occurrences by class
        self.dirs = {}             # (class, hash) -> path (this run)
        self.skipped = Counter()
        self.lock = threading.Lock()
        self.lock_commit = D.lock_commit()
        self.tree_head = D.tree_head()
        self.version = turfc_version()

    def record(self, v, workdir):
        cls = v.cls
        h = D.sig_hash(cls, v.sig)
        with self.lock:
            self.seen[cls] += 1
            d = os.path.join(self.dir, cls, h)
            meta_path = os.path.join(d, "meta.json")
            if os.path.isdir(d) and os.path.exists(meta_path):
                meta = json.load(open(meta_path))
                meta["count"] = meta.get("count", 1) + 1
                meta["last_seed"] = v.seed
                json.dump(meta, open(meta_path, "w"), indent=2)
                return d, False
            if self.new_per_class[cls] >= self.cap:
                self.skipped[cls] += 1
                return None, False
            self.new_per_class[cls] += 1
            os.makedirs(d, exist_ok=True)
        # outside the lock: copy files
        shutil.copy(os.path.join(workdir, "prog.tr"), os.path.join(d, "prog.tr"))
        write(os.path.join(d, "expected.txt"), v.expected_text)
        for name, o in v.runs.items():
            write(os.path.join(d, "out_%s.txt" % name),
                  "config: %s\nresult: %s\nrc: %s\n--- stdout ---\n%s--- stderr ---\n%s" % (
                      name, o.short(), o.rc, o.stdout, o.stderr))
        for name, (rc, text) in v.compile.items():
            if rc != 0:
                write(os.path.join(d, "compile_%s.txt" % name), "rc: %s\n%s" % (rc, text))
        shutil.copy(os.path.join(ROOT, "compiler.lock"), os.path.join(d, "compiler.lock"))
        meta = {
            "class": cls, "signature": v.sig, "summary": v.summary, "note": v.note,
            "seed": v.seed, "last_seed": v.seed, "count": 1,
            "compiler_lock_commit": self.lock_commit, "compiler_tree_head": self.tree_head,
            "turfc_version": self.version,
            "found": datetime.datetime.now().isoformat(timespec="seconds"),
            "reproduce": "python3 oracles/differential.py %d" % v.seed,
            "status": "unminimized",
        }
        json.dump(meta, open(os.path.join(d, "meta.json"), "w"), indent=2)
        return d, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--cap", type=int, default=20, help="max new finding dirs per class per run")
    ap.add_argument("--findings", default=os.path.join(ROOT, "findings"))
    ap.add_argument("--quick", action="store_true", help="-O0 only (generator debugging)")
    ap.add_argument("--allow-undefined", action="store_true")
    ap.add_argument("--reduce", type=float, default=0, metavar="SECONDS",
                    help="after the run, reduce each new finding for up to SECONDS "
                         "(gen/reduce.py) and write min.tr beside prog.tr")
    ap.add_argument("--no-asan", action="store_true",
                    help="skip the ASan-runtime configurations even if build_asan/turfc exists")
    a = ap.parse_args()

    jobs = max(1, min(a.jobs, MAX_JOBS))
    if jobs != a.jobs:
        print("note: --jobs capped at %d" % MAX_JOBS)
    if not os.path.exists(D.TURFC):
        sys.exit("compiler not found: %s (set TURF_ROOT or TURFC)" % D.TURFC)

    run_root = os.path.join(ROOT, "work", "run_%d" % os.getpid())
    os.makedirs(run_root, exist_ok=True)
    rec = Recorder(a.cap, a.findings)
    print("compiler: %s  lock=%s tree=%s" % (D.TURFC, rec.lock_commit[:10], rec.tree_head[:10]))
    print("%s" % rec.version)
    asan = False if (a.no_asan or a.quick) else D.asan_available()
    print("asan runtime: %s" % (D.TURFC_ASAN if asan else
                                "off" if (a.no_asan or a.quick) else
                                "not built (%s missing): those configurations are skipped" % D.TURFC_ASAN))

    counts = Counter()
    outcomes = Counter()
    new_dirs = []
    stats_lock = threading.Lock()
    start = time.time()
    deadline = start + a.seconds

    def task(seed):
        wd = os.path.join(run_root, str(seed))
        try:
            v = D.check_seed(seed, wd, a.allow_undefined, a.quick, asan)
        except Exception as e:  # a generator/oracle bug, never a compiler finding
            shutil.rmtree(wd, ignore_errors=True)
            return seed, None, "HARNESS ERROR %s: %s" % (type(e).__name__, e)
        res = v.expected
        kind = getattr(res, "kind", "?")
        label = kind if kind == "exit" else "trap:" + res.trap
        if not v.ok():
            d, new = rec.record(v, wd)
            shutil.rmtree(wd, ignore_errors=True)
            return seed, v, (label, d, new)
        shutil.rmtree(wd, ignore_errors=True)
        return seed, v, (label, None, False)

    next_seed = a.seed
    done = 0
    last_report = start
    pending = set()
    try:
        with ThreadPoolExecutor(jobs) as ex:
            while True:
                now = time.time()
                while now < deadline and len(pending) < jobs * 2:
                    pending.add(ex.submit(task, next_seed))
                    next_seed += 1
                if not pending:
                    break
                fin, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for f in fin:
                    seed, v, info = f.result()
                    done += 1
                    if v is None:
                        counts["harness-error"] += 1
                        print("seed %d: %s" % (seed, info))
                        continue
                    label, d, new = info
                    outcomes[label] += 1
                    cls = v.cls or "ok"
                    counts[cls] += 1
                    if v.cls and new:
                        new_dirs.append(d)
                        print("[%6.0fs] NEW %s seed %d: %s\n          -> %s" % (
                            time.time() - start, v.cls, seed, v.summary[:160],
                            os.path.relpath(d, ROOT)))
                if time.time() - last_report > 30:
                    last_report = time.time()
                    el = last_report - start
                    print("[%6.0fs] %d programs (%.2f/s) %s" % (el, done, done / el, dict(counts)))
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        shutil.rmtree(run_root, ignore_errors=True)

    el = time.time() - start
    print("\n== summary ==")
    print("programs: %d in %.0fs = %.2f programs/s (%d jobs%s)" % (
        done, el, done / el if el else 0, jobs, ", -O0 only" if a.quick else ""))
    print("results:  %s" % dict(counts))
    print("expected outcomes: %s" % dict(outcomes))
    print("seeds:    %d .. %d" % (a.seed, next_seed - 1))
    for cls, n in sorted(rec.seen.items()):
        print("class %-15s occurrences %d, new dirs %d, over cap %d" % (
            cls, n, rec.new_per_class[cls], rec.skipped[cls]))
    for d in new_dirs:
        print("  " + os.path.relpath(d, ROOT))
    if a.reduce and new_dirs:
        # After the run, one at a time: each reduction uses the whole job
        # budget itself, so it never runs beside the fuzzing jobs.
        import reduce as R
        print("\n== reducing %d new finding(s), up to %.0fs each ==" % (len(new_dirs), a.reduce))
        for d in new_dirs:
            meta_path = os.path.join(d, "meta.json")
            meta = json.load(open(meta_path))
            src, summary = R.reduce_seed(meta["seed"], a.reduce, jobs)
            if src is not None:
                write(os.path.join(d, "min.tr"), src)
                meta["status"] = "reduced (min.tr): " + summary
            else:
                meta["status"] = "unminimized: " + summary
            json.dump(meta, open(meta_path, "w"), indent=2)
            print("  %s: %s" % (os.path.relpath(d, ROOT), meta["status"]))
    sys.exit(0)


if __name__ == "__main__":
    main()
