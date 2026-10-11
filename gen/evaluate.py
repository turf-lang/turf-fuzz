"""Reference evaluator for the generator's AST (tast.py).

Independent of the compiler: it encodes the language rules recorded in
SEMANTICS.md and nothing else. The programs must have been rendered first
(tast.render) so statements carry their source line.

evaluate(program) -> Result
    Result.stdout   the exact bytes the program prints
    Result.kind     'exit' | 'trap'
    Result.code     exit status for 'exit' (low 8 bits of main's value)
    Result.trap     'Integer overflow' | 'Division by zero' | 'Modulo by zero' (+ ' in compound assignment') for 'trap'
    Result.line     source line the trap reports

Raises Undefined for programs whose result the language does not define
(see SEMANTICS.md, 'INT_MIN % -1'), and Timeout past a step budget.
"""

from tast import (INT_MIN, INT_MAX, Lit, BLit, Var, Un, Bin, Call, Give, Decl,
                  Assign, Comp, IncDec, Print, If, While, Break, Continue,
                  Return)

STEP_LIMIT = 3_000_000


class Undefined(Exception):
    pass


class Timeout(Exception):
    pass


class Trap(Exception):
    def __init__(self, kind, line):
        self.kind, self.line = kind, line


class _Ret(Exception):
    def __init__(self, v):
        self.v = v


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class Result:
    def __init__(self, stdout, kind, code=0, trap=None, line=0):
        self.stdout, self.kind, self.code, self.trap, self.line = stdout, kind, code, trap, line

    def describe(self):
        if self.kind == "exit":
            return "exit %d" % self.code
        return "trap %s line %d" % (self.trap, self.line)


def _in_range(v):
    return INT_MIN <= v <= INT_MAX


def _tdiv(a, b):
    """Division truncating toward zero."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


class _Machine:
    def __init__(self, program):
        self.funcs = {f.name: f for f in program.funcs}
        self.out = []
        self.steps = 0
        self.line = 0  # line of the statement / branch being evaluated

    def tick(self):
        self.steps += 1
        if self.steps > STEP_LIMIT:
            raise Timeout()

    def trap(self, kind):
        raise Trap(kind, self.line)

    # ----------------------------------------------------------- expressions
    def ev(self, e, env):
        self.tick()
        if isinstance(e, (Lit, BLit)):
            return e.v
        if isinstance(e, Var):
            return env[e.name]
        if isinstance(e, Un):
            v = self.ev(e.e, env)
            if e.op == "!":
                return not v
            if v == INT_MIN:
                self.trap("Integer overflow")
            return -v
        if isinstance(e, Bin):
            return self.binary(e, env)
        if isinstance(e, Call):
            args = [self.ev(a, env) for a in e.args]  # left to right
            return self.call(self.funcs[e.fn], args)
        if isinstance(e, Give):
            return self.ev(e.a, env) if self.ev(e.c, env) else self.ev(e.b, env)
        raise TypeError(e)

    def binary(self, e, env):
        op = e.op
        if op == "&&":
            return bool(self.ev(e.l, env)) and bool(self.ev(e.r, env))
        if op == "||":
            return bool(self.ev(e.l, env)) or bool(self.ev(e.r, env))
        a = self.ev(e.l, env)
        b = self.ev(e.r, env)
        return self.apply(op, a, b)

    def apply(self, op, a, b, compound=False):
        if op == "+":
            r = a + b
        elif op == "-":
            r = a - b
        elif op == "*":
            r = a * b
        elif op in ("/", "%"):
            if b == 0:
                self.trap(("Division by zero" if op == "/" else "Modulo by zero")
                          + (" in compound assignment" if compound else ""))
            if a == INT_MIN and b == -1:
                if op == "/":
                    self.trap("Integer overflow")
                raise Undefined("INT_MIN %% -1")
            q = _tdiv(a, b)
            r = q if op == "/" else a - q * b
            return r
        elif op == "==":
            return a == b
        elif op == "!=":
            return a != b
        elif op == "<":
            return a < b
        elif op == ">":
            return a > b
        elif op == "<=":
            return a <= b
        elif op == ">=":
            return a >= b
        else:
            raise ValueError(op)
        if not _in_range(r):
            self.trap("Integer overflow")
        return r

    # ------------------------------------------------------------ statements
    def call(self, f, args):
        env = dict(zip(f.params, args))
        saved = self.line
        try:
            self.block(f.body, env)
        except _Ret as r:
            self.line = saved
            return r.v
        raise AssertionError("function %s fell off its end" % f.name)

    def block(self, stmts, env):
        for s in stmts:
            self.stmt(s, env)

    def stmt(self, s, env):
        self.tick()
        self.line = getattr(s, "line", self.line)
        if isinstance(s, Decl):
            env[s.name] = self.ev(s.e, env)
        elif isinstance(s, Assign):
            env[s.name] = self.ev(s.e, env)
        elif isinstance(s, Comp):
            rhs = self.ev(s.e, env)
            env[s.name] = self.apply(s.op, env[s.name], rhs, True)
        elif isinstance(s, IncDec):
            env[s.name] = self.apply("+" if s.op == "++" else "-", env[s.name], 1)
        elif isinstance(s, Print):
            v = self.ev(s.e, env)
            if isinstance(v, bool):
                v = 1 if v else 0
            self.out.append("%d\n" % v)
        elif isinstance(s, If):
            for cond, body, line in s.branches:
                self.line = line
                if self.ev(cond, env):
                    self.block(body, env)
                    return
            if s.els is not None:
                self.block(s.els, env)
        elif isinstance(s, While):
            self.line = s.decl_line
            env[s.counter] = 0
            while True:
                self.line = s.line
                self.tick()
                if not env[s.counter] < s.bound:
                    break
                self.line = s.incr_line
                env[s.counter] = self.apply("+", env[s.counter], 1)
                try:
                    self.block(s.body, env)
                except _Break:
                    break
                except _Continue:
                    continue
        elif isinstance(s, Break):
            raise _Break()
        elif isinstance(s, Continue):
            raise _Continue()
        elif isinstance(s, Return):
            raise _Ret(self.ev(s.e, env))
        else:
            raise TypeError(s)


def evaluate(program):
    m = _Machine(program)
    try:
        v = m.call(program.main, [])
    except Trap as t:
        return Result("".join(m.out), "trap", trap=t.kind, line=t.line)
    return Result("".join(m.out), "exit", code=v & 0xFF)
