# The native arm64 translator (docs/dev/NATIVE_ARM64.md). It needs LLVM and Capstone, so it builds on
# its own: cmake -S tools/translator -B build-translator -DLLVM_DIR=$(brew --prefix llvm)/lib/cmake/llvm
cmake_minimum_required(VERSION 3.20)
project(Aps5Translator C CXX ASM)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(LLVM REQUIRED CONFIG)
find_path(CAPSTONE_INCLUDE_DIR capstone/capstone.h HINTS /opt/homebrew/opt/capstone/include REQUIRED)
find_library(CAPSTONE_LIBRARY NAMES libcapstone.a capstone HINTS /opt/homebrew/opt/capstone/lib REQUIRED)

llvm_map_components_to_libnames(translatorLlvmLibraries core support passes target transformutils aarch64codegen aarch64desc aarch64info)

add_executable(aps5-translator src/Main.cpp src/Lifter.cpp src/LiftVector.cpp src/LiftFloat.cpp src/LiftBits.cpp src/Elf.cpp)
target_include_directories(aps5-translator SYSTEM PRIVATE ${LLVM_INCLUDE_DIRS} ${CAPSTONE_INCLUDE_DIR})
target_compile_definitions(aps5-translator PRIVATE ${LLVM_DEFINITIONS})
target_link_libraries(aps5-translator PRIVATE ${translatorLlvmLibraries} ${CAPSTONE_LIBRARY})

# Linked into every translated executable together with the object aps5-translator writes.
add_library(aps5-translator-runtime STATIC runtime/Runtime.cpp runtime/HostBridge.S)
set_target_properties(aps5-translator-runtime PROPERTIES OSX_ARCHITECTURES arm64)

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    add_test(NAME native_fixture_reporting COMMAND ${Python3_EXECUTABLE} -m unittest discover
        -s ${CMAKE_CURRENT_SOURCE_DIR}/../tests -p test_native_fixture_runner.py)
    add_test(NAME native_translator_cli COMMAND ${Python3_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_translator_cli.py $<TARGET_FILE:aps5-translator>)
    add_test(NAME native_ffmpeg_fetch COMMAND ${Python3_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/../tests/test_ffmpeg_fetch.py)
    set_tests_properties(native_fixture_reporting native_translator_cli native_ffmpeg_fetch PROPERTIES TIMEOUT 30)
endif()
