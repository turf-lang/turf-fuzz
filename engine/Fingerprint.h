// A canonical text form of one analysis result: what must be identical when
// the same input is analyzed twice in one process.
#ifndef TURF_FUZZ_FINGERPRINT_H
#define TURF_FUZZ_FINGERPRINT_H

#include "turf/Analysis.h"

#include <sstream>
#include <string>

inline std::string fingerprint(const turf::AnalysisResult &R) {
  std::ostringstream O;
  O << (R.succeeded() ? "ok" : "fail") << " decls=" << R.program().size()
    << " prelude=" << R.preludeImportCount() << '\n';
  for (const auto &D : R.diagnostics()) {
    O << (D.Level == turf::Severity::Error ? 'E' : 'W') << ' ' << D.Code << ' '
      << D.Where.Path << ':' << D.Where.Line << ':' << D.Where.Col << ' '
      << D.Message << " | " << D.Hint << '\n';
    for (const auto &Rel : D.Related)
      O << "  rel " << Rel.Where.Path << ':' << Rel.Where.Line << ':'
        << Rel.Where.Col << ' ' << Rel.Message << '\n';
  }
  return O.str();
}

inline turf::AnalysisOptions fuzzOptions(bool Flow, bool Prelude = true) {
  turf::AnalysisOptions Opt;
  Opt.InstallRoot = TURF_FUZZ_ROOT;
  Opt.RunLint = true;
  Opt.RunFlowAnalysis = Flow;
  Opt.SkipPrelude = !Prelude;
  return Opt;
}

#endif
