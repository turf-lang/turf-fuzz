# Issue labels for the nightly's findings. Sourced by ci/file-issues.sh and
# the engine job's issue step, not run.
#
# Every issue gets three: fuzz-finding, kind:<what went wrong> and
# compiler:<which compiler found it>. A finding seen only by compiler:master
# is a fresh regression; one with compiler:pinned too is older than the lock.
# A label that does not exist yet is created, with its colour and description,
# the first time it is needed, so the repository needs no setup.

# kind_label SIGNATURE: the kind label of a findings/<class>/<id> directory,
# a soundness/<verdict>/<case> failure, or an engine-<kind> input group.
kind_label() {
  local sig=${1#findings/}
  case $sig in
    soundness/unsound-accepted/*) echo kind:unsound ;;
    soundness/rejected-valid/*)   echo kind:rejected-valid ;;
    soundness/wrong-reason/*)     echo kind:wrong-message ;;
    soundness/safe-wrong/*)       echo kind:miscompile ;;
    soundness/crash/*)            echo kind:crash ;;
    soundness/*)                  echo kind:unsound ;;
    *)                            echo "kind:${sig%%/*}" ;;
  esac
}

# compiler_label SCOPE: process/pinned, process/master, sanitized or engine.
compiler_label() {
  case $1 in
    */pinned|engine) echo compiler:pinned ;;
    */master)        echo compiler:master ;;
    sanitized)       echo compiler:sanitized ;;
    *)               echo "compiler:${1##*/}" ;;
  esac
}

# ensure_label NAME: create NAME if this repository lacks it. Once per run.
_labels_have=""
ensure_label() {
  local name=$1 color desc
  case " $_labels_have " in *" $name "*) return 0 ;; esac
  case $name in
    fuzz-finding)            color=5319E7; desc="Found by the nightly fuzz" ;;
    kind:ice)                color=B60205; desc="The compiler reported an internal compiler error" ;;
    kind:crash)              color=B60205; desc="The compiler crashed" ;;
    kind:llvm-error)         color=B60205; desc="LLVM rejected the IR the compiler emitted" ;;
    kind:miscompile)         color=D93F0B; desc="A program compiled to the wrong behaviour" ;;
    kind:unsound)            color=D93F0B; desc="An unsafe program was accepted" ;;
    kind:hang)               color=FBCA04; desc="The compiler did not finish" ;;
    kind:leak)               color=FBCA04; desc="The compiler leaked memory" ;;
    kind:rejected-valid)     color=FBCA04; desc="A valid program was rejected" ;;
    kind:wrong-message)      color=C5DEF5; desc="A diagnostic says the wrong thing" ;;
    kind:engine-crash)       color=B60205; desc="libFuzzer: the front end crashed" ;;
    kind:engine-timeout)     color=FBCA04; desc="libFuzzer: the front end timed out" ;;
    kind:engine-oom)         color=FBCA04; desc="libFuzzer: the front end ran out of memory" ;;
    compiler:pinned)         color=0E8A16; desc="Seen at compiler.lock" ;;
    compiler:master)         color=1D76DB; desc="Seen at turflang master" ;;
    compiler:sanitized)      color=006B75; desc="Seen by the ASan + UBSan compiler from master" ;;
    *)                       color=BFDADC; desc="Found by the nightly fuzz" ;;
  esac
  gh label create "$name" --repo "$GITHUB_REPOSITORY" --color "$color" --description "$desc" \
    >/dev/null 2>&1 || true # already there, or the token cannot create labels
  _labels_have="$_labels_have $name"
}

# labels_for SIGNATURE SCOPE: the comma-separated --label value, labels ensured.
labels_for() {
  local l out=""
  for l in fuzz-finding "$(kind_label "$1")" "$(compiler_label "$2")"; do
    ensure_label "$l"; out="${out:+$out,}$l"
  done
  echo "$out"
}
