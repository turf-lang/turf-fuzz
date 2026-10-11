// Re-entrancy gate for the libFuzzer engine.
//
// libFuzzer calls the facade millions of times in one process, so any state
// that leaks from one analysis into the next turns into a false finding (or
// hides a real one). This runs every file given on the command line through
// turf::analyze() forwards, then backwards, then each file twice in a row,
// and requires the same fingerprint for a file every time. Any difference is
// a facade isolation bug in turflang, not a fuzzing result.
//
//   reentrancy_check [--no-flow] FILE.tr...
//
// Exit 0 when every file is stable, 1 otherwise (each unstable file and the
// first differing line are printed).

#include "Fingerprint.h"

#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <vector>

static std::string readFile(const std::string &P) {
  std::ifstream In(P, std::ios::binary);
  std::ostringstream S;
  S << In.rdbuf();
  return S.str();
}

static std::string firstDiff(const std::string &A, const std::string &B) {
  std::istringstream SA(A), SB(B);
  std::string LA, LB;
  while (true) {
    bool GA = static_cast<bool>(std::getline(SA, LA));
    bool GB = static_cast<bool>(std::getline(SB, LB));
    if (!GA && !GB)
      return "(identical?)";
    if (!GA)
      LA = "<end>";
    if (!GB)
      LB = "<end>";
    if (LA != LB)
      return "  first: " + LA + "\n  later: " + LB;
  }
}

int main(int argc, char **argv) {
  bool Flow = true;
  std::vector<std::string> Files;
  for (int I = 1; I < argc; ++I) {
    std::string A = argv[I];
    if (A == "--no-flow")
      Flow = false;
    else
      Files.push_back(A);
  }
  const auto Opt = fuzzOptions(Flow);

  std::map<std::string, std::string> Text, First;
  for (const auto &F : Files)
    Text[F] = readFile(F);

  auto Run = [&](const std::string &F) {
    return fingerprint(turf::analyze({{F, Text[F]}}, Opt));
  };

  int Unstable = 0;
  auto Check = [&](const std::string &F, const char *Pass) {
    std::string FP = Run(F);
    auto It = First.find(F);
    if (It == First.end()) {
      First[F] = FP;
      return;
    }
    if (It->second != FP) {
      ++Unstable;
      std::cout << "UNSTABLE " << F << " (" << Pass << ")\n"
                << firstDiff(It->second, FP) << '\n';
      It->second = FP; // report each subsequent change once more, not forever
    }
  };

  for (const auto &F : Files)
    Check(F, "forward");
  for (auto It = Files.rbegin(); It != Files.rend(); ++It)
    Check(*It, "backward");
  for (const auto &F : Files) {
    Check(F, "repeat-1");
    Check(F, "repeat-2");
  }

  std::cout << Files.size() << " files, " << Unstable << " unstable results\n";
  return Unstable ? 1 : 0;
}
