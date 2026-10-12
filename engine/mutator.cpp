// Structure-aware mutation for fuzz_analyze_tok (turf-fuzz item 5).
//
// Byte mutation spends nearly all of its executions on inputs the lexer or
// the parser rejects in the first few tokens; coverage plateaued at ~27.3k
// edges after 20 minutes. These hooks mutate Turf source the way
// tier0/mutate.py does, on tokens and lines, so most outputs still lex and
// many still parse, and the engine reaches the resolver, the type checker and
// the flow passes behind them.
//
//   LLVMFuzzerCustomMutator   a quarter of the time libFuzzer's own byte
//                             mutation (it carries the -dict= entries and the
//                             comparison operands it traced, which no token
//                             table has); otherwise 1-3 token or line
//                             operations
//   LLVMFuzzerCustomCrossOver a splice of two inputs at token boundaries, or
//                             a run of lines from one inserted into the other
//
// Deterministic for a given Seed (libFuzzer replays mutations by seed). The
// token tables are generated from the compiler's lexer at build time
// (gen_tokens.py -> turf_tokens.inc).

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <random>
#include <string>
#include <vector>

#include "turf_tokens.inc"

extern "C" size_t LLVMFuzzerMutate(uint8_t *Data, size_t Size, size_t MaxSize);

namespace {

template <size_t N> constexpr size_t len(const char *const (&)[N]) { return N; }

enum class Kind { Space, Comment, Str, Chr, Num, Id, Op };

struct Tok {
  Kind K;
  std::string Text;
};

bool isIdStart(char C) { return (C >= 'a' && C <= 'z') || (C >= 'A' && C <= 'Z') || C == '_'; }
bool isIdChar(char C) { return isIdStart(C) || (C >= '0' && C <= '9'); }
bool isDigit(char C) { return C >= '0' && C <= '9'; }

// Longest first: the lexer's multi-character operators (tier0/mutate.py's
// TOKEN_RE, plus compound assignment).
const char *const kMultiOps[] = {"<<=", ">>=", "::", "->", "=>", "==", "!=", "<=", ">=",
                                 "&&", "||", "<<", ">>", "++", "--", "+=", "-=", "*=",
                                 "/=", "%=", "&=", "|=", "^=", "..", "**", "&?"};

std::vector<Tok> tokenize(const std::string &S) {
  std::vector<Tok> Out;
  size_t I = 0, N = S.size();
  while (I < N) {
    size_t J = I;
    char C = S[I];
    Kind K;
    if (C == ' ' || C == '\t' || C == '\n' || C == '\r') {
      while (J < N && (S[J] == ' ' || S[J] == '\t' || S[J] == '\n' || S[J] == '\r'))
        ++J;
      K = Kind::Space;
    } else if (C == '/' && I + 1 < N && S[I + 1] == '/') {
      while (J < N && S[J] != '\n')
        ++J;
      K = Kind::Comment;
    } else if (C == '"' || C == '\'') {
      ++J;
      while (J < N && S[J] != C && S[J] != '\n') {
        if (S[J] == '\\' && J + 1 < N)
          ++J;
        ++J;
      }
      if (J < N && S[J] == C)
        ++J;
      K = C == '"' ? Kind::Str : Kind::Chr;
    } else if (isDigit(C)) {
      while (J < N && (isIdChar(S[J]) || S[J] == '.'))
        ++J;
      K = Kind::Num;
    } else if (isIdStart(C)) {
      while (J < N && isIdChar(S[J]))
        ++J;
      K = Kind::Id;
    } else {
      K = Kind::Op;
      J = I + 1;
      for (const char *Op : kMultiOps) {
        size_t L = std::strlen(Op);
        if (S.compare(I, L, Op) == 0) {
          J = I + L;
          break;
        }
      }
    }
    Out.push_back({K, S.substr(I, J - I)});
    I = J;
  }
  return Out;
}

std::string render(const std::vector<Tok> &T) {
  std::string S;
  for (const auto &X : T)
    S += X.Text;
  return S;
}

class Mutator {
  std::mt19937 Rng;

public:
  explicit Mutator(unsigned Seed) : Rng(Seed) {}

  size_t below(size_t N) { return N ? std::uniform_int_distribution<size_t>(0, N - 1)(Rng) : 0; }
  bool chance(double P) { return std::uniform_real_distribution<double>(0, 1)(Rng) < P; }
  template <size_t N> const char *pick(const char *const (&A)[N]) { return A[below(N)]; }

  std::string randomToken(const std::vector<Tok> &T) {
    switch (below(7)) {
    case 0: return pick(kKeywords);
    case 1: return pick(kPunct);
    case 2: return pick(kNumbers);
    case 3: return pick(kStrings);
    case 4: return pick(kTypes);
    case 5: return pick(kNames);
    default: {  // an identifier already in the input: names that resolve
      std::vector<size_t> Ids;
      for (size_t I = 0; I < T.size(); ++I)
        if (T[I].K == Kind::Id)
          Ids.push_back(I);
      return Ids.empty() ? std::string(pick(kNames)) : T[Ids[below(Ids.size())]].Text;
    }
    }
  }

  static std::vector<size_t> code(const std::vector<Tok> &T) {
    std::vector<size_t> Out;
    for (size_t I = 0; I < T.size(); ++I)
      if (T[I].K != Kind::Space && T[I].K != Kind::Comment)
        Out.push_back(I);
    return Out;
  }

  void tokenOp(std::vector<Tok> &T) {
    auto Idx = code(T);
    if (Idx.empty()) {
      T.push_back({Kind::Op, pick(kSnippets)});
      return;
    }
    size_t I = Idx[below(Idx.size())];
    switch (below(12)) {
    case 0:  // delete
      T.erase(T.begin() + I);
      break;
    case 1:  // duplicate
      T.insert(T.begin() + I, T[I]);
      break;
    case 2: {  // swap two tokens
      size_t J = Idx[below(Idx.size())];
      std::swap(T[I], T[J]);
      break;
    }
    case 3:  // replace
      T[I] = {Kind::Op, randomToken(T)};
      break;
    case 4:  // insert a token
      T.insert(T.begin() + I, {{Kind::Op, randomToken(T)}, {Kind::Space, " "}});
      break;
    case 5: {  // swap two identifiers
      std::vector<size_t> Ids;
      for (size_t K : Idx)
        if (T[K].K == Kind::Id)
          Ids.push_back(K);
      if (Ids.size() >= 2)
        std::swap(T[Ids[below(Ids.size())]], T[Ids[below(Ids.size())]]);
      break;
    }
    case 6: {  // a boundary number
      std::vector<size_t> Nums;
      for (size_t K : Idx)
        if (T[K].K == Kind::Num)
          Nums.push_back(K);
      if (!Nums.empty())
        T[Nums[below(Nums.size())]] = {Kind::Num, pick(kNumbers)};
      break;
    }
    case 7: {  // drop a run of tokens
      size_t J = std::min(T.size(), I + 2 + below(20));
      T.erase(T.begin() + I, T.begin() + J);
      break;
    }
    case 8: {  // copy a run of tokens elsewhere
      size_t J = std::min(T.size(), I + 2 + below(20));
      std::vector<Tok> Run(T.begin() + I, T.begin() + J);
      size_t At = Idx[below(Idx.size())];
      T.insert(T.begin() + At, Run.begin(), Run.end());
      break;
    }
    case 9: {  // insert a whole construct, before a statement boundary if any
      std::string S = std::string(" ") + pick(kSnippets) + " ";
      size_t At = I;
      for (size_t K = I; K < T.size(); ++K)
        if (T[K].Text == ";" || T[K].Text == "{" || T[K].Text == "}") {
          At = K + 1;
          break;
        }
      T.insert(T.begin() + std::min(At, T.size()), {Kind::Op, S});
      break;
    }
    case 10: {  // wrap a run in a block-forming construct
      static const char *const Open[] = {"if (true) { ", "while (false) { ", "unsafe { ",
                                         "{ ", "for i in 0..2 { ", "(", "["};
      static const char *const Close[] = {" }", " }", " }", " }", " }", ")", "]"};
      size_t W = below(len(Open));
      size_t J = std::min(T.size(), I + 1 + below(15));
      T.insert(T.begin() + J, {Kind::Op, Close[W]});
      T.insert(T.begin() + I, {Kind::Op, Open[W]});
      break;
    }
    default: {  // modest nesting (deep nesting belongs to tier 0)
      static const char *const Open[] = {"(", "{", "[", "-", "!"};
      static const char *const Close[] = {")", "}", "]", "", ""};
      size_t W = below(5), N = 2 + below(30);
      std::string S;
      for (size_t K = 0; K < N; ++K)
        S += Open[W];
      for (size_t K = 0; K < N; ++K)
        S += Close[W];
      T.insert(T.begin() + I, {Kind::Op, S});
      break;
    }
    }
  }

  std::string lineOp(const std::string &Src) {
    std::vector<std::string> L;
    size_t P = 0;
    while (P < Src.size()) {
      size_t E = Src.find('\n', P);
      E = E == std::string::npos ? Src.size() : E + 1;
      L.push_back(Src.substr(P, E - P));
      P = E;
    }
    if (L.empty())
      return Src;
    size_t N = below(L.size());
    switch (below(7)) {
    case 0:
      L.erase(L.begin() + N);
      break;
    case 1:
      L.insert(L.begin() + N, L[N]);
      break;
    case 2:
      std::swap(L[N], L[below(L.size())]);
      break;
    case 3: {  // delete the first ';' on the line
      size_t K = L[N].find(';');
      if (K != std::string::npos)
        L[N].erase(K, 1);
      break;
    }
    case 4: {  // swap a type name for another, whole word
      static const char *const Ty[] = {"int", "int32", "double", "bool", "char", "string", "void"};
      std::string From = Ty[below(7)], To = Ty[below(7)];
      size_t K = L[N].find(From);
      if (K != std::string::npos && (K == 0 || !isIdChar(L[N][K - 1])) &&
          (K + From.size() >= L[N].size() || !isIdChar(L[N][K + From.size()])))
        L[N].replace(K, From.size(), To);
      break;
    }
    case 5: {  // typo an identifier: swap its first two letters
      auto T = tokenize(L[N]);
      std::vector<size_t> Ids;
      for (size_t K = 0; K < T.size(); ++K)
        if (T[K].K == Kind::Id && T[K].Text.size() >= 2 && isIdStart(T[K].Text[1]))
          Ids.push_back(K);
      if (!Ids.empty()) {
        std::string &S = T[Ids[below(Ids.size())]].Text;
        std::swap(S[0], S[1]);
        L[N] = render(T);
      }
      break;
    }
    default: {  // move a line elsewhere
      std::string X = L[N];
      L.erase(L.begin() + N);
      L.insert(L.begin() + below(L.size() + 1), X);
      break;
    }
    }
    std::string Out;
    for (auto &X : L)
      Out += X;
    return Out;
  }

  std::string mutate(std::string S) {
    size_t Rounds = 1 + below(3);
    for (size_t R = 0; R < Rounds; ++R) {
      if (chance(0.3)) {
        S = lineOp(S);
      } else {
        auto T = tokenize(S);
        tokenOp(T);
        S = render(T);
      }
    }
    return S;
  }
};

size_t emit(const std::string &S, uint8_t *Out, size_t Max) {
  size_t N = std::min(S.size(), Max);
  std::memcpy(Out, S.data(), N);
  return N;
}

} // namespace

extern "C" size_t LLVMFuzzerCustomMutator(uint8_t *Data, size_t Size, size_t MaxSize,
                                          unsigned int Seed) {
  Mutator M(Seed);
  if (M.chance(0.25))
    return LLVMFuzzerMutate(Data, Size, MaxSize);
  std::string Out = M.mutate(std::string(reinterpret_cast<char *>(Data), Size));
  if (Out.empty() || Out.size() > MaxSize)  // nothing left, or too big to cut cleanly
    return LLVMFuzzerMutate(Data, Size, MaxSize);
  return emit(Out, Data, MaxSize);
}

extern "C" size_t LLVMFuzzerCustomCrossOver(const uint8_t *Data1, size_t Size1,
                                            const uint8_t *Data2, size_t Size2,
                                            uint8_t *Out, size_t MaxOutSize,
                                            unsigned int Seed) {
  Mutator M(Seed);
  std::string A(reinterpret_cast<const char *>(Data1), Size1);
  std::string B(reinterpret_cast<const char *>(Data2), Size2);
  std::string R;
  if (M.chance(0.5)) {
    // Prefix of A's tokens, suffix of B's.
    auto TA = tokenize(A), TB = tokenize(B);
    size_t I = M.below(TA.size() + 1), J = M.below(TB.size() + 1);
    std::vector<Tok> T(TA.begin(), TA.begin() + I);
    T.insert(T.end(), TB.begin() + J, TB.end());
    R = render(T);
  } else {
    // A run of B's lines inserted at a line boundary of A: a function or a
    // statement block moved between programs whole.
    auto lines = [](const std::string &S) {
      std::vector<size_t> Starts{0};
      for (size_t K = 0; K < S.size(); ++K)
        if (S[K] == '\n')
          Starts.push_back(K + 1);
      return Starts;
    };
    auto LA = lines(A), LB = lines(B);
    size_t At = LA[M.below(LA.size())];
    size_t From = M.below(LB.size());
    size_t To = std::min(LB.size() - 1, From + 1 + M.below(12));
    std::string Run = B.substr(LB[From], LB[To] - LB[From]);
    if (Run.empty() || Run.back() != '\n')
      Run += "\n";
    R = A.substr(0, At) + Run + A.substr(At);
  }
  return emit(R, Out, MaxOutSize);
}
