// libFuzzer target: one input = one Turf source file, run through the
// compiler front end (lex, parse, resolve, type check, lint, flow analysis)
// via turf::analyze().
//
// What counts as a finding: anything libFuzzer itself reports -- a sanitizer
// error, abort() (an assertion or llvm::report_fatal_error), a timeout, or the
// target calling exit() (the facade must never end its host process; turf-lsp
// would die the same way).
//
// TURF_FUZZ_CHECK_DETERMINISM=1 analyzes every input twice and aborts if the
// two results differ: a leak of state between analyses, which would otherwise
// make every other finding of this engine untrustworthy.

#include "Fingerprint.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>

static bool CheckDeterminism = false;

extern "C" int LLVMFuzzerInitialize(int *, char ***) {
  const char *E = std::getenv("TURF_FUZZ_CHECK_DETERMINISM");
  CheckDeterminism = E && *E == '1';
  return 0;
}

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size) {
  // The compiler's front end has a 2000-level nesting cap; deeper inputs are
  // rejected early, but huge inputs only slow the engine down.
  if (Size > 64 * 1024)
    return -1;
  std::string Text(reinterpret_cast<const char *>(Data), Size);
  static const auto Opt = fuzzOptions(/*Flow=*/true);

  auto R = turf::analyze({{"fuzz.tr", Text}}, Opt);
  if (CheckDeterminism) {
    std::string A = fingerprint(R);
    std::string B = fingerprint(turf::analyze({{"fuzz.tr", Text}}, Opt));
    if (A != B) {
      std::fprintf(stderr, "NONDETERMINISTIC ANALYSIS\n--- first\n%s--- second\n%s",
                   A.c_str(), B.c_str());
      std::abort();
    }
  }
  return 0;
}
