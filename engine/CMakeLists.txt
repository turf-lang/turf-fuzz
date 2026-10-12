cmake_minimum_required(VERSION 3.15)
project(turf-fuzz-engine LANGUAGES C CXX)

# Tier 4: coverage-guided in-process fuzzing of the compiler front end through
# the query facade (include/turf/Analysis.h). Needs clang with compiler-rt's
# libFuzzer; configure with
#   CC=clang CXX=clang++ cmake -S engine -B engine/build -G Ninja
# Codegen, -g and linking are not reachable from here (the facade never runs
# Codegen); those stay on the process tiers (tier0/, oracles/).

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  message(FATAL_ERROR "the libFuzzer engine needs clang (CC=clang CXX=clang++)")
endif()

set(TURF_ROOT "${CMAKE_SOURCE_DIR}/../../turflang" CACHE PATH "turflang (compiler) checkout")
option(TURF_FUZZ_SANITIZE "Also instrument with ASan+UBSan" ON)

# Instrument every translation unit of turfcore for coverage. LLVM itself is
# prebuilt and stays uninstrumented, which is what we want: the engine should
# steer through Turf's code, not LLVM's.
set(_fuzz_cflags -fsanitize=fuzzer-no-link -g -O1 -fno-omit-frame-pointer)
if(TURF_FUZZ_SANITIZE)
  list(APPEND _fuzz_cflags -fsanitize=address,undefined -fno-sanitize-recover=undefined)
endif()
add_compile_options(${_fuzz_cflags})
if(TURF_FUZZ_SANITIZE)
  add_link_options(-fsanitize=address,undefined)
endif()

# Front end only: no local-model runtime.
set(TURF_SMART OFF CACHE BOOL "" FORCE)
add_subdirectory(${TURF_ROOT} turflang_build EXCLUDE_FROM_ALL)

# The libFuzzer target.
# Linked against a copy of the libFuzzer runtime with its bundled C++ runtime
# made local (see fix_libfuzzer.sh), not with -fsanitize=fuzzer.
execute_process(
  COMMAND ${CMAKE_CXX_COMPILER} -print-runtime-dir
  OUTPUT_VARIABLE _rt_dir OUTPUT_STRIP_TRAILING_WHITESPACE)
set(_libfuzzer_in "${_rt_dir}/libclang_rt.fuzzer-${CMAKE_SYSTEM_PROCESSOR}.a")
if(NOT EXISTS "${_libfuzzer_in}")
  set(_libfuzzer_in "${_rt_dir}/libclang_rt.fuzzer.a")
endif()
if(NOT EXISTS "${_libfuzzer_in}")
  message(FATAL_ERROR "libFuzzer runtime not found in ${_rt_dir} (install compiler-rt)")
endif()
set(_libfuzzer_out "${CMAKE_BINARY_DIR}/libfuzzer_local.a")
add_custom_command(
  OUTPUT ${_libfuzzer_out}
  COMMAND ${CMAKE_SOURCE_DIR}/fix_libfuzzer.sh ${_libfuzzer_in} ${_libfuzzer_out}
  DEPENDS ${CMAKE_SOURCE_DIR}/fix_libfuzzer.sh ${_libfuzzer_in})
add_custom_target(libfuzzer_local DEPENDS ${_libfuzzer_out})

add_executable(fuzz_analyze fuzz_analyze.cpp)
add_dependencies(fuzz_analyze libfuzzer_local)
target_link_libraries(fuzz_analyze PRIVATE ${_libfuzzer_out} turfcore)
target_link_options(fuzz_analyze PRIVATE -fsanitize=fuzzer-no-link)
target_compile_definitions(fuzz_analyze PRIVATE TURF_FUZZ_ROOT="${TURF_ROOT}")

# Re-entrancy check: the same coverage-instrumented library, an ordinary
# main(). Analyzes a file list in two different orders and requires identical
# results per file. Gate for trusting anything fuzz_analyze reports.
add_executable(reentrancy_check reentrancy_check.cpp)
target_link_libraries(reentrancy_check PRIVATE turfcore)
target_link_options(reentrancy_check PRIVATE -fsanitize=fuzzer-no-link)
target_compile_definitions(reentrancy_check PRIVATE TURF_FUZZ_ROOT="${TURF_ROOT}")

# Structure-aware variant (item 5): the same harness plus token/line-level
# LLVMFuzzerCustomMutator and CustomCrossOver (mutator.cpp). A separate binary
# so the byte-mutation one stays as it was, to measure against. Its token
# tables and the -dict= file are generated from the compiler's lexer.
find_package(Python3 REQUIRED COMPONENTS Interpreter)
set(_tok_dir "${CMAKE_BINARY_DIR}/tokens")
add_custom_command(
  OUTPUT ${_tok_dir}/turf_tokens.inc ${_tok_dir}/turf.dict
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/gen_tokens.py ${TURF_ROOT} ${_tok_dir}
  DEPENDS ${CMAKE_SOURCE_DIR}/gen_tokens.py ${TURF_ROOT}/src/Lexer.cpp)
add_custom_target(turf_tokens DEPENDS ${_tok_dir}/turf_tokens.inc ${_tok_dir}/turf.dict)

add_executable(fuzz_analyze_tok fuzz_analyze.cpp mutator.cpp)
add_dependencies(fuzz_analyze_tok libfuzzer_local turf_tokens)
target_include_directories(fuzz_analyze_tok PRIVATE ${_tok_dir})
target_link_libraries(fuzz_analyze_tok PRIVATE ${_libfuzzer_out} turfcore)
target_link_options(fuzz_analyze_tok PRIVATE -fsanitize=fuzzer-no-link)
target_compile_definitions(fuzz_analyze_tok PRIVATE TURF_FUZZ_ROOT="${TURF_ROOT}")
