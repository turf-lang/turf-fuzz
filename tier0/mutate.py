"""mutate.py -- the tier-0 mutators, self-contained.

Ported from two turflang files, so turf-fuzz never imports from turflang's
tree at runtime:

  * scripts/dev/fuzz/crash_fuzz.py        token-level operators, tokenizer, vocabularies
  * scripts/slm/slm_benchmark/mutate.py   the four line-level mutators SLM-001 wrote
                                          (delete a `;`, swap a type, drop a `}`,
                                          typo an identifier)

Only the mutators were taken; the SLM benchmark's corpus builder is not part
of fuzzing and stays in turflang.

The keyword list is read from the compiler's own lexer table
($TURF_ROOT/src/frontend/Lexer.cpp), so a keyword added to the language is fuzzed with
no edit here.
"""
from __future__ import annotations

import random
import re
from pathlib import Path

# ── tokens ──────────────────────────────────────────────────────────────────
TOKEN_RE = re.compile(
    r"""(?P<ws>\s+)
      | (?P<comment>//[^\n]*)
      | (?P<str>"(?:\\.|[^"\\\n])*"?)
      | (?P<chr>'(?:\\.|[^'\\\n])*'?)
      | (?P<num>\d[\w.]*)
      | (?P<id>[A-Za-z_]\w*)
      | (?P<op>::|->|=>|==|!=|<=|>=|&&|\|\||<<|>>|\+\+|--|[-+*/%&|^]=|\.\.|.)
    """,
    re.VERBOSE | re.DOTALL,
)

PUNCT = ["(", ")", "{", "}", "[", "]", ";", ",", ".", ":", "::", "->", "=", "==", "!=",
         "<", ">", "<=", ">=", "+", "-", "*", "/", "%", "&", "|", "^", "!", "?", "~",
         "<<", ">>", "&&", "||", "@", "#", "$", "\\", "`"]
NUMBERS = ["0", "1", "-1", "255", "256", "2147483647", "2147483648", "4294967296",
           "9223372036854775807", "9223372036854775808", "99999999999999999999",
           "0x", "0xFFFFFFFFFFFFFFFF", "1e308", "1e999", "0.0", "1.", ".5", "1_000",
           "0b102", "1.2.3", "08"]
STRINGS = ['""', '"\\', '"\\q"', '"\\x"', '"\\u{110000}"', "'a'", "''", "'ab'", "'\\n'"]
TYPES = ["int", "int32", "double", "bool", "char", "string", "void", "int*", "int[]",
         "&int", "&?int", "!int"]

_FALLBACK_KEYWORDS = ["fn", "struct", "enum", "if", "while", "return", "import", "pub", "mut"]


def keywords(turf_root: Path) -> list[str]:
    """The lexer's own keyword table (`{"name", TOK_...}` entries)."""
    try:
        text = (Path(turf_root) / "src" / "frontend" / "Lexer.cpp").read_text()
        words = sorted(set(re.findall(r'\{"([a-z_]+)",\s*TOK_', text)))
        if words:
            return words
    except OSError:
        pass
    return list(_FALLBACK_KEYWORDS)


def tokenize(src: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(src):
        m = TOKEN_RE.match(src, pos)
        if not m:  # unreachable (the op arm eats any char); stay total anyway
            out.append(("op", src[pos]))
            pos += 1
            continue
        out.append((m.lastgroup, m.group()))
        pos = m.end()
    return out


def render(toks: list[tuple[str, str]]) -> str:
    return "".join(t for _, t in toks)


def code_idx(toks) -> list[int]:
    return [i for i, (k, _) in enumerate(toks) if k not in ("ws", "comment")]


# ── line-level mutators (SLM-001, scripts/slm/slm_benchmark/mutate.py) ──────────
# Each takes (lines, 1-based line, extra) and returns the new line list; some
# assert a precondition (the caller treats a miss as a no-op).
def delete_semicolon(lines: list[str], line: int, extra: dict) -> list[str]:
    """Drop the FIRST ';' on a 1-based `line`."""
    idx = line - 1
    lines[idx] = lines[idx].replace(";", "", 1)
    return lines


def swap_type(lines: list[str], line: int, extra: dict) -> list[str]:
    """Replace `extra["from"]` with `extra["to"]` (whole word) on a 1-based `line`."""
    idx = line - 1
    old, new = extra["from"], extra["to"]
    pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(old) + r"(?![A-Za-z0-9_])")
    lines[idx] = pattern.sub(new, lines[idx], count=1)
    return lines


def drop_closing_brace(lines: list[str], line: int, extra: dict) -> list[str]:
    """Delete the entire 1-based `line`, which must be a lone '}'."""
    idx = line - 1
    assert lines[idx].strip() == "}", f"line {line} is not a lone '}}': {lines[idx]!r}"
    del lines[idx]
    return lines


def typo_identifier(lines: list[str], line: int, extra: dict) -> list[str]:
    """Swap the first two characters of `extra["name"]` on a 1-based `line`
    (the result must still start like an identifier)."""
    idx = line - 1
    name = extra["name"]
    assert len(name) >= 2, "need at least 2 chars to transpose"
    typo = name[1] + name[0] + name[2:]
    assert typo[0].isalpha() or typo[0] == "_", f"{name!r} -> {typo!r} starts with a digit"
    pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
    lines[idx] = pattern.sub(typo, lines[idx], count=1)
    return lines


LINE_MUTATORS = {
    "delete_semicolon": delete_semicolon,
    "swap_type": swap_type,
    "drop_closing_brace": drop_closing_brace,
    "typo_identifier": typo_identifier,
}


# ── mutation operators ──────────────────────────────────────────────────────
class Mutator:
    def __init__(self, rng: random.Random, vocab: list[str], kw: list[str]):
        self.rng = rng
        self.kw = kw
        self.vocab = vocab
        self.line_mut = LINE_MUTATORS

    def random_token(self) -> str:
        r = self.rng
        pool = r.choice([self.kw, PUNCT, NUMBERS, STRINGS, TYPES, self.vocab or PUNCT])
        return r.choice(pool)

    def token_op(self, toks):
        r = self.rng
        idx = code_idx(toks)
        if not idx:
            return toks
        i = r.choice(idx)
        op = r.choice(["delete", "dup", "swap", "replace", "insert", "truncate",
                       "swap_ident", "number", "nest", "wrap_chain", "drop_run"])
        toks = list(toks)
        if op == "delete":
            del toks[i]
        elif op == "dup":
            toks.insert(i, toks[i])
        elif op == "swap":
            j = r.choice(idx)
            toks[i], toks[j] = toks[j], toks[i]
        elif op == "replace":
            toks[i] = ("op", self.random_token())
        elif op == "insert":
            toks[i:i] = [("op", self.random_token()), ("ws", " ")]
        elif op == "truncate":
            toks = toks[:i]
        elif op == "swap_ident":
            ids = [k for k in idx if toks[k][0] == "id"]
            if len(ids) >= 2:
                a, b = r.sample(ids, 2)
                toks[a], toks[b] = toks[b], toks[a]
        elif op == "number":
            nums = [k for k in idx if toks[k][0] == "num"]
            if nums:
                toks[r.choice(nums)] = ("num", r.choice(NUMBERS))
        elif op == "nest":
            # Depth is the classic stack-overflow / quadratic-time shape.
            opener, closer = r.choice([("(", ")"), ("{", "}"), ("[", "]"), ("-", ""), ("!", "")])
            n = r.choice([50, 300, 2000, 20000])
            toks[i:i] = [("op", opener * n + closer * n)]
        elif op == "wrap_chain":
            # Depth built from FINISHED pieces: `((x--...--)--...--)--...--`. Each
            # piece is inside the parser's per-construct limits, the tree is
            # their sum (found by hand after the nesting ops missed it).
            piece = r.choice(["--", "++", ".a", "[0]"])
            n, levels = r.choice([100, 700, 1500]), r.choice([2, 3, 10])
            e = toks[i][1]
            for _ in range(levels):
                e = "(" + e + piece * n + ")"
            toks[i] = (toks[i][0], e)
        elif op == "drop_run":
            j = min(len(toks), i + r.randint(2, 40))
            del toks[i:j]
        return toks

    def line_op(self, src: str) -> str:
        r = self.rng
        lines = src.splitlines(keepends=True)
        if not lines:
            return src
        kind = r.choice(["delete_line", "dup_line", "swap_lines", "delete_semicolon",
                         "swap_type", "drop_closing_brace", "typo_identifier"])
        n = r.randrange(len(lines))
        try:
            if kind == "delete_line":
                del lines[n]
            elif kind == "dup_line":
                lines.insert(n, lines[n])
            elif kind == "swap_lines":
                m = r.randrange(len(lines))
                lines[n], lines[m] = lines[m], lines[n]
            elif kind == "swap_type":
                a, b = r.sample(TYPES[:7], 2)
                lines = self.line_mut["swap_type"](lines, n + 1, {"from": a, "to": b})
            elif kind == "typo_identifier":
                ids = re.findall(r"[A-Za-z_]\w+", lines[n])
                if ids:
                    lines = self.line_mut["typo_identifier"](lines, n + 1, {"name": r.choice(ids)})
            else:
                lines = self.line_mut[kind](lines, n + 1, {})
        except (AssertionError, IndexError):
            pass  # the SLM mutators assert their precondition; a miss is a no-op
        return "".join(lines)

    def mutate(self, src: str) -> str:
        for _ in range(self.rng.choice([1, 1, 1, 2, 3, 5])):
            if self.rng.random() < 0.3:
                src = self.line_op(src)
            else:
                src = render(self.token_op(tokenize(src)))
        return src
