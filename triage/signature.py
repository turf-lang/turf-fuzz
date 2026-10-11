"""signature.py -- a stable identity for "the compiler misbehaved".

Two inputs that crash the compiler in the same place must get the same
signature; two different bugs must not.  Deduping by input hash (what the
turflang original did) reports one bug a thousand times, because every mutant
hashes differently.

compute(rc, output) -> Signature | None   (None = the compiler behaved)

    rc      the process exit status as Python's subprocess reports it
            (negative = killed by that signal), or None for a hang
            (the wall-clock budget expired).
    output  stdout + stderr, merged.

What goes into the signature, by kind (the first rule that matches wins):

    sanitizer   the report type ("AddressSanitizer: stack-overflow",
                "UBSan: file.cpp: signed integer overflow: N + N ...") plus the
                top 3 in-project frames of the report's first stack.
    assert      the assertion text (and the function it sits in).
    llvm-error  the normalized "LLVM ERROR: ..." message.
    ice         the normalized "Internal compiler error" / "terminate called"
                / "what():" line.
    hang        the literal text "hang" (all hangs are one bucket).
    signal      the signal name plus the top 3 in-project frames, if the
                compiler printed a stack trace.
    exit        "exit N: <normalized first line>" for any status other than
                0 or 1 (exit 2 = the command line is wrong).

No addresses, no line numbers, no temp-file names, no bare numbers: a
signature that changes between two runs of the same crash is worthless.

A deep recursion (stack overflow) crashes at a different point of the cycle on
each input, so "top 3 frames" would fragment one bug into several.  When one
in-project function appears 4+ times among the frames, the signature is
instead the sorted set of the functions that repeat ("recursion[...]").
"""
from __future__ import annotations

import hashlib
import os
import re
import signal as _signal
from collections import Counter
from dataclasses import dataclass

ANSI = re.compile(r"\x1b\[[0-9;]*m")

TOP_FRAMES = 3
RECURSION_THRESHOLD = 4


@dataclass(frozen=True)
class Signature:
    kind: str   # sanitizer | assert | llvm-error | ice | hang | signal | exit
    text: str   # human-readable canonical form; what the hash is taken over

    @property
    def key(self) -> str:
        """12 hex digits; the findings directory name."""
        return hashlib.sha1(f"{self.kind}\n{self.text}".encode()).hexdigest()[:12]

    def __str__(self) -> str:
        return f"{self.kind}: {self.text}"


# ── normalization ───────────────────────────────────────────────────────────
_PATH = re.compile(r"(?<![\w)\]])(?:[A-Za-z]:)?(?:[\\/][^\s:'\"`()<>]+)+")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUM = re.compile(r"\b\d+\b")
_WS = re.compile(r"\s+")


def normalize(s: str, limit: int = 200) -> str:
    """Strip everything that varies between runs of one bug: ANSI, directories,
    hex addresses, bare numbers (line numbers, counts), whitespace runs."""
    s = ANSI.sub("", s[:4 * limit])  # bound the work: mutants can echo megabyte lines
    s = _PATH.sub(lambda m: re.split(r"[\\/]", m.group())[-1], s)
    s = _HEX.sub("ADDR", s)
    s = _NUM.sub("N", s)
    s = _WS.sub(" ", s).strip()
    return s[:limit]


def _first_line(out: str) -> str:
    for line in ANSI.sub("", out).splitlines():
        if line.strip():
            return line
    return ""


# ── stack frames ────────────────────────────────────────────────────────────
# "#3 0x55d0 name(args) File.cpp:12:5"   "#3 0x55d0 in name /p/File.cpp:12"
# "#3 0x55d0 name(args) (build/turfc+0x1702361)"   "#3 0x55d0 (/usr/lib/libc.so.6+0x3e6f0)"
_FRAME = re.compile(r"^\s*#(\d+)\s+0x[0-9a-fA-F]+\s*(?:in\s+)?(.*?)\s*$")
_MODULE_TAIL = re.compile(r"\s*\((?P<mod>[^()\s]+?)\+0x[0-9a-fA-F]+\)\s*$")
_FILE_TAIL = re.compile(r"\s(?P<file>\S+?):\d+(?::\d+)?\s*$")
_BUILD_ID = re.compile(r"\s*\(BuildId: [0-9a-fA-F]+\)\s*$")
_NAME_OFFSET = re.compile(r"\+0x[0-9a-fA-F]+$")
_TEMPLATE = re.compile(r"<[^<>]*>")

# Frames that are never "the project": LLVM, the C++ library, sanitizer
# runtimes, the C library.  Matched on the function name.
_FOREIGN_NAME = (
    "llvm::", "clang::", "lld::", "std::", "__gnu_cxx::", "__cxxabiv1::", "__",
    "_ZN4llvm", "_ZNSt", "_ZSt", "_ZN9__gnu_cxx", "___interceptor", "operator ",
    "pthread_",
)
_FOREIGN_EXACT = {"SignalHandler", "abort", "raise", "gsignal", "killpg", "kill"}
_FOREIGN_PATH = ("/usr/", "llvm", "compiler-rt", "libsanitizer", "libstdc++", "/opt/")
_PROJECT_MODULES = ("turfc", "turf", "turf-lsp", "turf-api-tests", "turf-fuzz-target")


def _function_name(raw: str) -> str:
    """`ns::f<T>(int, char const*)` -> `ns::f`; offsets, templates and the
    argument list are noise."""
    n = _NAME_OFFSET.sub("", raw.strip())
    n = n.replace("(anonymous namespace)", "ANON")
    while True:
        m = _TEMPLATE.sub("", n)
        if m == n:
            break
        n = m
    n = n.split("(", 1)[0].strip()
    n = re.sub(r"\s*\[clone [^\]]*\]$", "", n)
    return n


def parse_frame(line: str):
    """-> (function, is_project) or None when `line` is not a stack frame."""
    m = _FRAME.match(line)
    if not m:
        return None
    rest = _BUILD_ID.sub("", m.group(2))
    file = module = None
    t = _MODULE_TAIL.search(rest)
    if t:
        module, rest = t.group("mod"), rest[: t.start()]
    else:
        t = _FILE_TAIL.search(rest)
        if t:
            file, rest = t.group("file"), rest[: t.start()]
    if rest.startswith("(") and module is None:  # "(mod+0x..)" with no name
        return ("", False)
    name = _function_name(rest)
    if not name:
        return ("", False)
    if name in _FOREIGN_EXACT or any(name.startswith(p) for p in _FOREIGN_NAME):
        return (name, False)
    if file is not None:
        if "/" not in file and "\\" not in file:
            return (name, True)  # basename only: the project's own symbolizer output
        return (name, not any(p in file for p in _FOREIGN_PATH))
    if module is not None:
        return (name, os.path.basename(module) in _PROJECT_MODULES)
    return (name, False)


def first_stack(text: str, start: int = 0) -> list[str]:
    """In-project function names of the first contiguous run of frame lines at
    or after character offset `start`."""
    frames: list[str] = []
    started = False
    for line in text[start:].splitlines():
        p = parse_frame(line)
        if p is None:
            if started:
                break
            continue
        started = True
        name, mine = p
        if mine:
            frames.append(name)
    return frames


def frames_text(frames: list[str]) -> str:
    """Canonical text for a frame list (see the module docstring)."""
    if not frames:
        return ""
    window = frames[:64]
    counts = Counter(window)
    if max(counts.values()) >= RECURSION_THRESHOLD:
        cycle = sorted(f for f, c in counts.items() if c >= 2)[:6]
        return "recursion[" + ",".join(cycle) + "]"
    return ",".join(frames[:TOP_FRAMES])


# ── per-kind extractors ─────────────────────────────────────────────────────
_SANITIZER = re.compile(
    r"AddressSanitizer|UndefinedBehaviorSanitizer|runtime error:|LeakSanitizer|"
    r"ThreadSanitizer|MemorySanitizer")
_SAN_HEADER = re.compile(r"ERROR: (?P<san>\w*Sanitizer): ?(?P<what>[^\n]*)")
# Regexes below start from a literal and look backwards by hand: a leading
# `[^\n]*` or an optional prefix makes the search quadratic on the very long
# lines a mutant can make the compiler echo (a 200 KB output once took a minute,
# and a regex holds the GIL, stalling every worker thread).
_UBSAN = re.compile(r"runtime error: (?P<msg>[^\n]*)")
_UBSAN_FILE = re.compile(r"([^\s:]+):\d+:\d+: $")
_ASSERT = re.compile(r"Assertion [`'\"](?P<cond>.*?)['\"] failed")
_ASSERT_FN = re.compile(r":\d+: (?P<fn>.*): $")
_LLVM_ERROR = re.compile(r"LLVM ERROR: (?P<msg>[^\n]*)")
_STACKDUMP = re.compile(r"PLEASE submit a bug report|^Stack dump:", re.M)
_TERMINATE = re.compile(r"terminate called[^\n]*")
_WHAT = re.compile(r"what\(\):[^\n]*")
_INTERNAL = re.compile(r"[Ii]nternal (?:compiler )?error[^\n]*")
_STDEXC = re.compile(r"std::(?:bad_alloc|out_of_range)[^\n]*")


def _sanitizer(out: str) -> Signature:
    h = _SAN_HEADER.search(out)
    if h:
        what = normalize(h.group("what"), 400)
        for cut in (" on ", " at ", " in ", " (", " thread", ";"):
            if cut in what:
                what = what.split(cut, 1)[0]
        text = f"{h.group('san')}: {what.strip()}"
        frames = first_stack(out, h.end())
    else:
        u = _UBSAN.search(out)
        if u:
            prefix = out[max(0, u.start() - 300):u.start()]
            fm = _UBSAN_FILE.search(prefix)
            f = os.path.basename(fm.group(1)) if fm else ""
            text = "UBSan: " + (f + ": " if f else "") + normalize(u.group("msg"))
            frames = first_stack(out, u.end())
        else:
            text = "sanitizer: " + normalize(_first_line(out))
            frames = first_stack(out)
    ft = frames_text(frames)
    return Signature("sanitizer", text + (" @ " + ft if ft else ""))


def _signal_name(rc: int | None) -> str:
    if rc is None:
        return "no-signal"
    n = -rc if rc < 0 else rc - 128 if rc >= 128 else 0
    if n <= 0:
        return "no-signal"
    try:
        return _signal.Signals(n).name
    except ValueError:
        return f"SIG{n}"


def _crash(rc: int | None, out: str) -> Signature:
    ft = frames_text(first_stack(out))
    return Signature("signal", _signal_name(rc) + (" @ " + ft if ft else ""))


def compute(rc: int | None, output: str) -> Signature | None:
    """The signature of one compiler run, or None if the compiler behaved
    (exit 0 or 1, no crash text)."""
    if rc is None:
        return Signature("hang", "hang")
    out = ANSI.sub("", output or "")

    if _SANITIZER.search(out):
        return _sanitizer(out)

    m = _ASSERT.search(out)
    if m:
        fm = _ASSERT_FN.search(out[max(0, m.start() - 400):m.start()].rsplit("\n", 1)[-1])
        fn = _function_name(fm.group("fn")) if fm else ""
        cond = normalize(m.group("cond"))
        return Signature("assert", (fn + ": " if fn else "") + cond)

    m = _LLVM_ERROR.search(out)
    if m:
        return Signature("llvm-error", "LLVM ERROR: " + normalize(m.group("msg")))

    m = _TERMINATE.search(out)
    if m:
        w = _WHAT.search(out, m.end())
        return Signature("ice", normalize(m.group() + (" " + w.group() if w else "")))
    for rx in (_INTERNAL, _STDEXC, _WHAT):
        m = rx.search(out)
        if m:
            return Signature("ice", normalize(m.group()))

    if rc < 0 or rc >= 128:
        return _crash(rc, out)
    if _STACKDUMP.search(out):  # a stack trace without a fatal status
        return _crash(rc, out)
    if rc not in (0, 1):
        return Signature("exit", f"exit {rc}: " + normalize(_first_line(out)))
    return None
