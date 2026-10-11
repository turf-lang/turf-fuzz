# llvm-error: LLVM ERROR: Internal error: Base is not an array or pointer

- signature: `LLVM ERROR: Internal error: Base is not an array or pointer`
- class: llvm-error
- hash: 441788e4ccb6
- occurrences: 1
- first seen: 2026-10-11 15:06:23Z
- compiler.lock commit: 8a4d754eaa7d4b5e8c017fef3cf5d02afada45b2
- checkout HEAD: 8a4d754eaa7d4b5e8c017fef3cf5d02afada45b2
- command: `/home/a2ys/turflang/turflang/build/turfc min.tr -o a.out -g`
- flags: -g
- seed file: tests/18_operator_overload/10_operator_index_get_set.tr
- fuzz seed: 20261011
- worker: 2
- minimized: yes

minimized with 96 probes.

Inputs that import sibling modules must be compiled beside them; see `seed file` and `tier0/README.md` (`--replay-beside`).

## Compiler output (tail)

```
LLVM ERROR: Internal error: Base is not an array or pointer
 #0 0x000072d239998297 llvm::sys::PrintStackTrace(llvm::raw_ostream&, int) /usr/src/debug/llvm/llvm-project-22.1.8.src/llvm/lib/Support/Unix/Signals.inc:842:22
 #1 0x000072d239995027 llvm::sys::RunSignalHandlers() /usr/src/debug/llvm/llvm-project-22.1.8.src/llvm/lib/Support/Signals.cpp:108:20
 #2 0x000072d239995027 SignalHandler /usr/src/debug/llvm/llvm-project-22.1.8.src/llvm/lib/Support/Unix/Signals.inc:412:31
 #3 0x000072d23883e6f0 (/usr/lib/libc.so.6+0x3e6f0)
 #4 0x000072d23889a11c __pthread_kill_implementation /usr/src/debug/glibc/glibc/nptl/pthread_kill.c:44:76
 #5 0x000072d23883e5d0 raise /usr/src/debug/glibc/glibc/signal/../sysdeps/posix/raise.c:27:6
 #6 0x000072d238825685 abort /usr/src/debug/glibc/glibc/stdlib/abort.c:85:3
 #7 0x000072d23955c65c llvm::report_fatal_error(llvm::Twine const&, bool) /usr/src/debug/llvm/llvm-project-22.1.8.src/llvm/lib/Support/ErrorHandling.cpp:135:10
 #8 0x000072d23986b4de (/usr/lib/libLLVM.so.22.1+0x86b4de)
 #9 0x000064c30d05d1e3 ArrayAccessExprAST::codegenLValue() /home/a2ys/turflang/turflang/src/CodegenArray.cpp:512:53
#10 0x000064c30d05e280 ArrayAccessExprAST::codegen() /home/a2ys/turflang/turflang/src/CodegenArray.cpp:644:29
#11 0x000064c30cffd6d9 BinaryExprAST::codegen() /home/a2ys/turflang/turflang/src/CodegenExpr.cpp:190:27
#12 0x000064c30d046ab1 FuncCallExprAST::codegen() /home/a2ys/turflang/turflang/src/CodegenFunc.cpp:1264:81
#13 0x000064c30d023030 BlockExprAST::codegen() /home/a2ys/turflang/turflang/src/CodegenControlFlow.cpp:246:28
#14 0x000064c30d0422f6 FuncDefExprAST::codegen() /home/a2ys/turflang/turflang/src/CodegenFunc.cpp:605:7
#15 0x000064c30cf3ac87 runCompilerMain(int, char**) /home/a2ys/turflang/turflang/src/main.cpp:1955:25
#16 0x000064c30cf3da31 main /home/a2ys/turflang/turflang/src/main.cpp:2602:33
#17 0x000072d238827781 __libc_start_call_main /usr/src/debug/glibc/glibc/csu/../sysdeps/nptl/libc_start_call_main.h:83:3
#18 0x000072d2388278b9 call_init /usr/src/debug/glibc/glibc/csu/../csu/libc-start.c:127:20
#19 0x000072d2388278b9 __libc_start_main /usr/src/debug/glibc/glibc/csu/../csu/libc-start.c:359:5
#20 0x000064c30cf329a5 _start (/home/a2ys/turflang/turflang/build/turfc+0x889a5)
```
