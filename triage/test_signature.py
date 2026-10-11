"""Canned-output checks for triage/signature.py.

Run from the repository root:   python3 -m unittest
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from triage import signature as S  # noqa: E402


def frames(*names, base=0x55d0a0000000, start=0):
    return "\n".join(
        f"    #{i + start} 0x{base + 0x1000 * i:x} in {n} (build_san/turfc+0x{0x1700000 + i * 16:x}) "
        f"(BuildId: fffdc848fc2ca435df10e036446ade05ffb67154)"
        for i, n in enumerate(names))


ASAN_UAF = """\
=================================================================
==4242==ERROR: AddressSanitizer: heap-use-after-free on address 0x60200000eff0 at pc 0x55d0 bp 0x7ffc sp 0x7ffc
READ of size 8 at 0x60200000eff0 thread T0
    #0 0x7f11aa in __asan_memcpy (/usr/lib/libclang_rt.asan.so+0x3d1c9)
    #1 0x55d0b1 in turf::TypeChecker::checkCall(turf::CallExpr*, int) /home/u/turflang/src/TypeChecker.cpp:812:9
    #2 0x55d0b2 in turf::TypeChecker::checkExpr(turf::Expr*) /home/u/turflang/src/TypeChecker.cpp:455:12
    #3 0x55d0b3 in llvm::DenseMap<int, int>::find(int) /usr/src/debug/llvm/llvm-project-22.1.8.src/llvm/include/llvm/ADT/DenseMap.h:200:5
    #4 0x55d0b4 in turf::TypeChecker::run() /home/u/turflang/src/TypeChecker.cpp:90:3

0x60200000eff0 is located 0 bytes inside of 16-byte region [0x60200000eff0,0x60200000f000)
freed by thread T0 here:
    #0 0x7f11bb in operator delete(void*) (/usr/lib/libclang_rt.asan.so+0xdeadbeef)
    #1 0x55d0c1 in turf::Resolver::drop() /home/u/turflang/src/Resolver.cpp:77:3
"""

# Same bug, different addresses, line numbers, worker ids and prefix path.
ASAN_UAF_AGAIN = (ASAN_UAF
                  .replace("4242", "9999").replace("0x60200000eff0", "0x60300000aaa0")
                  .replace(":812:9", ":815:9").replace(":455:12", ":460:12")
                  .replace("/home/u/", "/ci/work/"))

ASAN_RECURSION = ("""\
AddressSanitizer:DEADLYSIGNAL
==1==ERROR: AddressSanitizer: stack-overflow on address 0x7ffd1 (pc 0x1 bp 0x2 sp 0x3 T0)
""" + frames(*["ParseUnary()", "ParseExpression()", "ParsePrimaryBase()", "ParsePrimary()"] * 10) + "\n")

# The same cycle entered at a different function.
ASAN_RECURSION_B = ("""\
==2==ERROR: AddressSanitizer: stack-overflow on address 0x7ffe9 (pc 0x1 bp 0x2 sp 0x3 T0)
""" + frames(*["ParsePrimary()", "ParseUnary()", "ParseExpression()", "ParsePrimaryBase()"] * 10) + "\n")

UBSAN = """\
src/Lexer.cpp:431:17: runtime error: signed integer overflow: 9223372036854775807 + 1 cannot be represented in type 'long'
    #0 0x55d0 in lexNumber() /home/u/turflang/src/Lexer.cpp:431:17
    #1 0x55d1 in gettok() /home/u/turflang/src/Lexer.cpp:600:5
"""
UBSAN_OTHER_NUMBERS = UBSAN.replace("9223372036854775807", "4611686018427387904").replace(":431:", ":433:")

GLIBC_ASSERT = ("turfc: /home/u/turflang/src/Codegen.cpp:1203: llvm::Value* "
                "turf::Codegen::emit(const turf::Expr*): Assertion `E && \"null expr\"' failed.\n"
                "PLEASE submit a bug report\nStack dump:\n0.\tProgram arguments: turfc x.tr\n")
GLIBC_ASSERT_AGAIN = GLIBC_ASSERT.replace(":1203:", ":1210:").replace("/home/u/", "/other/")

LLVM_ERROR = ("\x1b[1mLLVM ERROR: Internal error: a wildcard pattern reached Codegen "
              "as an expression.\x1b[0m\nPLEASE submit a bug report to ...\n")
LLVM_ERROR_NUMBERS = "LLVM ERROR: Invalid GEP into 3 elements at /tmp/x1/a.tr line 12\n"
LLVM_ERROR_NUMBERS_B = "LLVM ERROR: Invalid GEP into 17 elements at /tmp/y22/a.tr line 90\n"

ICE = "error: Internal compiler error in Codegen at line 40: bad node 7\n"

DIAG = ("\x1b[1m/tmp/x/a.tr\x1b[0m (1 error)\n\n\x1b[91m-- error at line 3, col 5 [E001 syntax error]\x1b[0m\n")

LLVM_TRACE = """\
Stack dump:
0.\tProgram arguments: build/turfc a.tr
 #0 0x7f1 llvm::sys::PrintStackTrace(llvm::raw_ostream&, int) /usr/src/debug/llvm/Signals.inc:842:22
 #1 0x7f2 SignalHandler /usr/src/debug/llvm/Signals.inc:412:31
 #2 0x7f3 (/usr/lib/libc.so.6+0x3e6f0)
 #3 0x55a1 turf::TypeChecker::unify(turf::Type*, turf::Type*) (build/turfc+0x1702361)
 #4 0x55a2 turf::TypeChecker::checkBinary(turf::BinaryExpr*) TypeChecker.cpp:0:0
 #5 0x55a3 std::function<void ()>::operator()() const /usr/include/c++/14/bits/std_function.h:591:9
 #6 0x55a4 main (build/turfc+0x1000)
"""


class SignatureTests(unittest.TestCase):
    def test_behaved(self):
        self.assertIsNone(S.compute(0, ""))
        self.assertIsNone(S.compute(1, DIAG))

    def test_sanitizer_frames_only_in_project_no_lines(self):
        sig = S.compute(1, ASAN_UAF)
        self.assertEqual(sig.kind, "sanitizer")
        self.assertEqual(
            sig.text,
            "AddressSanitizer: heap-use-after-free @ "
            "turf::TypeChecker::checkCall,turf::TypeChecker::checkExpr,turf::TypeChecker::run")
        self.assertNotIn("812", sig.text)
        self.assertNotIn("0x", sig.text)

    def test_sanitizer_stable_across_addresses_and_lines(self):
        self.assertEqual(S.compute(1, ASAN_UAF).key, S.compute(134, ASAN_UAF_AGAIN).key)

    def test_sanitizer_ignores_the_freed_by_stack(self):
        self.assertNotIn("Resolver", S.compute(1, ASAN_UAF).text)

    def test_recursion_collapses_entry_point(self):
        a, b = S.compute(1, ASAN_RECURSION), S.compute(1, ASAN_RECURSION_B)
        self.assertEqual(a.key, b.key)
        self.assertIn("recursion[ParseExpression,ParsePrimary,ParsePrimaryBase,ParseUnary]", a.text)

    def test_ubsan(self):
        sig = S.compute(1, UBSAN)
        self.assertEqual(sig.kind, "sanitizer")
        self.assertTrue(sig.text.startswith("UBSan: Lexer.cpp: signed integer overflow: N + N"), sig.text)
        self.assertIn("lexNumber,gettok", sig.text)
        self.assertEqual(sig.key, S.compute(1, UBSAN_OTHER_NUMBERS).key)

    def test_assertion(self):
        sig = S.compute(-6, GLIBC_ASSERT)
        self.assertEqual(sig.kind, "assert")
        self.assertEqual(sig.text, 'llvm::Value* turf::Codegen::emit: E && "null expr"')
        self.assertIn("null expr", sig.text)
        self.assertNotIn("1203", sig.text)
        self.assertEqual(sig.key, S.compute(-6, GLIBC_ASSERT_AGAIN).key)

    def test_llvm_error_strips_ansi(self):
        sig = S.compute(-6, LLVM_ERROR)
        self.assertEqual(sig.kind, "llvm-error")
        self.assertEqual(sig.text, "LLVM ERROR: Internal error: a wildcard pattern reached "
                         "Codegen as an expression.")

    def test_llvm_error_normalizes_numbers_and_paths(self):
        self.assertEqual(S.compute(-6, LLVM_ERROR_NUMBERS).key, S.compute(-6, LLVM_ERROR_NUMBERS_B).key)
        self.assertNotIn("/tmp", S.compute(-6, LLVM_ERROR_NUMBERS).text)

    def test_ice(self):
        sig = S.compute(1, ICE)
        self.assertEqual(sig.kind, "ice")
        self.assertIn("Internal compiler error", sig.text)

    def test_terminate_with_what(self):
        out = ("terminate called after throwing an instance of 'std::out_of_range'\n"
               "  what():  vector::_M_range_check: __n (which is 5) >= this->size() (which is 3)\n")
        sig = S.compute(-6, out)
        self.assertEqual(sig.kind, "ice")
        self.assertIn("what()", sig.text)
        self.assertNotIn("5", sig.text.replace("N", ""))

    def test_hang(self):
        sig = S.compute(None, "")
        self.assertEqual((sig.kind, sig.text), ("hang", "hang"))

    def test_signal_with_frames(self):
        sig = S.compute(-11, LLVM_TRACE)
        self.assertEqual(sig.kind, "signal")
        self.assertEqual(sig.text, "SIGSEGV @ turf::TypeChecker::unify,turf::TypeChecker::checkBinary,main")

    def test_signal_status_forms(self):
        self.assertEqual(S.compute(-6, "").text, "SIGABRT")
        self.assertEqual(S.compute(139, "").text, "SIGSEGV")  # shell-style 128+11

    def test_signal_distinct_from_assert(self):
        self.assertNotEqual(S.compute(-6, "").key, S.compute(-6, GLIBC_ASSERT).key)

    def test_exit_2_first_line(self):
        sig = S.compute(2, "\n\x1b[1merror: unknown option '--zzz' (2 given)\x1b[0m\nusage: ...\n")
        self.assertEqual(sig.kind, "exit")
        self.assertEqual(sig.text, "exit 2: error: unknown option '--zzz' (N given)")

    def test_distinct_bugs_distinct_keys(self):
        keys = {S.compute(*a).key for a in [
            (1, ASAN_UAF), (1, ASAN_RECURSION), (1, UBSAN), (-6, GLIBC_ASSERT),
            (-6, LLVM_ERROR), (1, ICE), (None, ""), (-11, LLVM_TRACE), (2, "error: x\n")]}
        self.assertEqual(len(keys), 9)

    def test_huge_lines_are_fast(self):
        # A mutant can make the compiler echo a 200 KB source line.  Every regex
        # holds the GIL, so a quadratic one stalls all fuzzer threads (it did).
        import time
        big = "  1 | fn main(): int { return " + "(" * 200000 + "1" + ")" * 200000 + " }\n"
        for out in (big * 5, big + "runtime error: x " + big, big + "Assertion `a' failed" + big,
                    big + "Internal error: z\n", "#0 0x1 " + "a" * 200000 + "\n" + big):
            t = time.time()
            for rc in (0, 1, -11):
                S.compute(rc, out)
            self.assertLess(time.time() - t, 2.0)

    def test_ubsan_without_file_prefix(self):
        sig = S.compute(1, "runtime error: load of null pointer of type 'int'\n")
        self.assertEqual(sig.text, "UBSan: load of null pointer of type 'int'")

    def test_parse_frame_forms(self):
        self.assertEqual(S.parse_frame("  #3 0x1f foo::bar(int) File.cpp:12:5"), ("foo::bar", True))
        self.assertEqual(S.parse_frame("  #3 0x1f in foo::bar<int>(int) /home/u/turflang/src/F.cpp:12"),
                         ("foo::bar", True))
        self.assertEqual(S.parse_frame("  #3 0x1f in foo() (build/turfc+0x12) (BuildId: ab12)"), ("foo", True))
        self.assertEqual(S.parse_frame("  #3 0x1f  (/usr/lib/libc.so.6+0x3e6f0)"), ("", False))
        self.assertEqual(S.parse_frame("  #3 0x1f abort (/usr/lib/libc.so.6+0x3e6f0)"), ("abort", False))
        self.assertIsNone(S.parse_frame("not a frame"))


if __name__ == "__main__":
    unittest.main()
