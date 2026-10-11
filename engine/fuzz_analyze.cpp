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
//
// The prelude (lib/std/core) is skipped by default: analysing it is nearly
// all the cost of analysing a small input. TURF_FUZZ_PRELUDE=1 keeps it, for
// inputs that lean on bare io/math calls or primitive methods.

#include "Fingerprint.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>

static bool CheckDeterminism = false;
static bool Prelude = false;

static bool envOn(const char *Name) {
  const char *E = std::getenv(Name);
  return E && *E == '1';
}

extern "C" int LLVMFuzzerInitialize(int *, char ***) {
  CheckDeterminism = envOn("TURF_FUZZ_CHECK_DETERMINISM");
  Prelude = envOn("TURF_FUZZ_PRELUDE");
  return 0;
}

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size) {
  // The compiler's front end has a 2000-level nesting cap; deeper inputs are
  // rejected early, but huge inputs only slow the engine down.
  if (Size > 64 * 1024)
    return -1;
  std::string Text(reinterpret_cast<const char *>(Data), Size);
  static const auto Opt = fuzzOptions(/*Flow=*/true, Prelude);

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
