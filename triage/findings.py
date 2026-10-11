"""findings.py -- one directory per signature.

    findings/<class>/<sig-hash>/
        input.tr     the first input that produced this signature
        min.tr       the minimized reproducer (same file as input.tr until minimized)
        report.md    the signature, the turfc command line, flags, the commit
                     from compiler.lock, and an `occurrences` counter

The directory IS the dedupe: when the signature's directory already exists, a
new hit only bumps `occurrences` in report.md.  Nothing is overwritten, so the
first (and later minimized) reproducer is never lost.

Two-phase so a long minimization cannot race with, or be lost to, a kill:

    f = Findings(root)
    if f.claim(cls, sig, input_src, meta):     # True: we created the directory
        small = ...minimize...
        f.finish(cls, sig, small, meta)
    # False: it existed; the counter was bumped; do not minimize again.

`record()` does both for callers with nothing to minimize.

`meta` keys (all optional): command (list or str), flags, seed_file, fuzz_seed,
worker, output (compiler output), note.
"""
from __future__ import annotations

import datetime
import os
import re
import shlex
import subprocess
import threading
from pathlib import Path

try:  # cross-process counter safety on POSIX; threads are covered by _LOCK
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from .signature import Signature

REPO = Path(__file__).resolve().parents[1]
_LOCK = threading.Lock()
_COUNT = re.compile(r"^- occurrences: (\d+)$", re.M)


def read_lock(repo: Path = REPO) -> dict[str, str]:
    """compiler.lock: `key = value` lines, `#` comments."""
    out: dict[str, str] = {}
    try:
        for line in (repo / "compiler.lock").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def checkout_head(turf_root: Path | None) -> str:
    """HEAD of the checkout actually under test (may differ from the lock)."""
    if not turf_root:
        return ""
    try:
        return subprocess.run(["git", "-C", str(turf_root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class Findings:
    def __init__(self, root: Path | str | None = None, repo: Path = REPO,
                 turf_root: Path | str | None = None):
        self.root = Path(root) if root else repo / "findings"
        self.repo = repo
        self.lock = read_lock(repo)
        self.turf_head = checkout_head(Path(turf_root)) if turf_root else ""

    # ── paths ───────────────────────────────────────────────────────────────
    def dir_for(self, cls: str, sig: Signature) -> Path:
        return self.root / cls / sig.key

    def exists(self, cls: str, sig: Signature) -> bool:
        return self.dir_for(cls, sig).is_dir()

    # ── dedupe ──────────────────────────────────────────────────────────────
    def bump(self, cls: str, sig: Signature) -> int:
        """Add one to the occurrence counter; returns the new value (0 if the
        report is missing)."""
        report = self.dir_for(cls, sig) / "report.md"
        with _LOCK:
            try:
                with open(report, "r+", encoding="utf-8") as fh:
                    if fcntl:
                        fcntl.flock(fh, fcntl.LOCK_EX)
                    text = fh.read()
                    m = _COUNT.search(text)
                    if not m:
                        return 0
                    n = int(m.group(1)) + 1
                    text = text[:m.start()] + f"- occurrences: {n}" + text[m.end():]
                    fh.seek(0)
                    fh.truncate()
                    fh.write(text)
                    return n
            except OSError:
                return 0

    # ── writing ─────────────────────────────────────────────────────────────
    def claim(self, cls: str, sig: Signature, input_src: str, meta: dict | None = None) -> bool:
        d = self.dir_for(cls, sig)
        with _LOCK:
            d.parent.mkdir(parents=True, exist_ok=True)
            try:
                d.mkdir()  # atomic: exactly one creator
            except FileExistsError:
                created = False
            else:
                created = True
        if not created:
            self.bump(cls, sig)
            return False
        (d / "input.tr").write_text(input_src)
        (d / "min.tr").write_text(input_src)  # replaced by finish(); never missing
        self._write_report(d, cls, sig, meta or {}, minimized=False)
        return True

    def finish(self, cls: str, sig: Signature, min_src: str, meta: dict | None = None) -> None:
        d = self.dir_for(cls, sig)
        with _LOCK:  # keep a concurrent bump() from being lost
            (d / "min.tr").write_text(min_src)
            self._write_report(d, cls, sig, meta or {}, minimized=True, keep_count=True)

    def record(self, cls: str, sig: Signature, input_src: str, min_src: str | None = None,
               meta: dict | None = None) -> bool:
        """claim + finish. True = new finding, False = duplicate (counter bumped)."""
        if not self.claim(cls, sig, input_src, meta):
            return False
        if min_src is not None:
            self.finish(cls, sig, min_src, meta)
        return True

    def _write_report(self, d: Path, cls: str, sig: Signature, meta: dict, minimized: bool,
                      keep_count: bool = False) -> None:
        old = d / "report.md"
        count = 1
        if keep_count and old.exists():
            m = _COUNT.search(old.read_text())
            if m:
                count = int(m.group(1))
        cmd = meta.get("command", "")
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(shlex.quote(str(c)) for c in cmd)
        lock = self.lock
        lines = [
            f"# {cls}: {sig.text[:100]}",
            "",
            f"- signature: `{sig.text}`",
            f"- class: {cls}",
            f"- hash: {sig.key}",
            f"- occurrences: {count}",
            f"- first seen: {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M:%SZ}",
            f"- compiler.lock commit: {lock.get('commit', '(none)')}",
        ]
        if self.turf_head:
            note = "" if self.turf_head == lock.get("commit") else "  (DIFFERS from compiler.lock)"
            lines.append(f"- checkout HEAD: {self.turf_head}{note}")
        lines += [
            f"- command: `{cmd}`",
            f"- flags: {meta.get('flags') or '(none)'}",
        ]
        for key, label in (("seed_file", "seed file"), ("fuzz_seed", "fuzz seed"), ("worker", "worker")):
            if meta.get(key) not in (None, ""):
                lines.append(f"- {label}: {meta[key]}")
        lines.append(f"- minimized: {'yes' if minimized else 'no (min.tr is the unreduced input)'}")
        if meta.get("note"):
            lines += ["", str(meta["note"])]
        lines += ["", "Inputs that import sibling modules must be compiled beside them; "
                      "see `seed file` and `tier0/README.md` (`--replay-beside`).",
                  "", "## Compiler output (tail)", "", "```",
                  str(meta.get("output", ""))[-3000:].rstrip(), "```", ""]
        tmp = d / ".report.md.tmp"
        tmp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(tmp, old)
