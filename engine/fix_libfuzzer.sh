#!/usr/bin/env bash
# Make a copy of clang's libFuzzer runtime in which only libFuzzer's own
# symbols are global.
#
# Why: some distributions (Arch's compiler-rt 22, at least) ship
# libclang_rt.fuzzer-<arch>.a as one pre-linked fuzzer.o that bundles
# libc++abi with GLOBAL symbols: operator new/delete, the fundamental typeinfo
# objects and __dynamic_cast. Linked into a libstdc++ program (turfcore and
# LLVM both are), those override libstdc++'s for the whole process:
# libc++abi's __dynamic_cast walks libstdc++'s __vmi_class_type_info and
# segfaults on the first dynamic_cast through a multiple-inheritance class
# (ParseVisibilityPrefixed, on the very first input), and ASan reports an
# alloc-dealloc-mismatch inside libLLVM's static initializers before main().
#
#   fix_libfuzzer.sh IN.a OUT.a
#
# Copies IN to OUT unchanged when nothing leaks.
set -euo pipefail
in=$1 out=$2
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Global symbols that are libFuzzer's interface or its own namespaced code.
# Everything else defined globally is the bundled C++ runtime.
keep_re='^(main|LLVMFuzzer|__libfuzzer|__sanitizer|__sancov|_ZN6fuzzer|_ZNK6fuzzer|_ZN8__Fuzzer|_ZNK8__Fuzzer|_ZNSt8__Fuzzer|_ZNKSt8__Fuzzer|_ZTVN6fuzzer|_ZTSN6fuzzer|_ZTIN6fuzzer)'

nm -g --defined-only "$in" | awk 'NF==3 {print $3}' | sort -u > "$tmp/all"
grep -E "$keep_re" "$tmp/all" > "$tmp/keep" || true
if [ "$(wc -l < "$tmp/all")" = "$(wc -l < "$tmp/keep")" ]; then
  cp "$in" "$out"
  exit 0
fi
objcopy --keep-global-symbols="$tmp/keep" "$in" "$out"
