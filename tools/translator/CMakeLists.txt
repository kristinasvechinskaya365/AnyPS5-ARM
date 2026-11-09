cmake_minimum_required(VERSION 3.20)
project(Aps5Translator C CXX ASM)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(aps5-translator src/Main.cpp src/Lifter.cpp src/LiftVector.cpp src/LiftFloat.cpp src/LiftBits.cpp src/Elf.cpp)
option(ANYPS5_USE_SYSTEM_DEPS "Use installed LLVM 22 and Capstone 5.0.9 for the standalone translator" OFF)
get_filename_component(translatorRoot "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
if(ANYPS5_USE_SYSTEM_DEPS)
    include("${translatorRoot}/cmake/SystemDeps.cmake")
    anyps5_configure_translator_system_dependencies(aps5-translator)
else()
    foreach(dependency IN ITEMS llvm-project/llvm capstone)
        if(NOT EXISTS "${translatorRoot}/3rdparty/${dependency}/CMakeLists.txt")
            message(FATAL_ERROR "Initialize native translator sources: git submodule update --init --checkout --force --depth 1 -- 3rdparty/llvm-project 3rdparty/capstone")
        endif()
    endforeach()
    set(LLVM_TARGETS_TO_BUILD AArch64 CACHE STRING "" FORCE)
    set(LLVM_ENABLE_PROJECTS "" CACHE STRING "" FORCE)
    set(LLVM_ENABLE_RUNTIMES "" CACHE STRING "" FORCE)
    foreach(feature IN ITEMS LLVM_INCLUDE_TESTS LLVM_INCLUDE_BENCHMARKS LLVM_INCLUDE_EXAMPLES LLVM_INCLUDE_DOCS
            LLVM_BUILD_TOOLS LLVM_BUILD_LLVM_DYLIB LLVM_LINK_LLVM_DYLIB LLVM_ENABLE_BINDINGS LLVM_ENABLE_OCAMLDOC
            LLVM_ENABLE_ZLIB LLVM_ENABLE_ZSTD LLVM_ENABLE_LIBXML2 LLVM_ENABLE_CURL LLVM_ENABLE_TERMINFO)
        set(${feature} OFF CACHE BOOL "" FORCE)
    endforeach()
    set(LLVM_PARALLEL_LINK_JOBS 1 CACHE STRING "Maximum concurrent LLVM links")
    add_subdirectory("${translatorRoot}/3rdparty/llvm-project/llvm" "${CMAKE_CURRENT_BINARY_DIR}/llvm" EXCLUDE_FROM_ALL)
    set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
    set(BUILD_STATIC_LIBS ON CACHE BOOL "" FORCE)
    set(CAPSTONE_ARCHITECTURE_DEFAULT OFF CACHE BOOL "" FORCE)
    set(CAPSTONE_X86_SUPPORT ON CACHE BOOL "" FORCE)
    set(CAPSTONE_BUILD_MACOS_THIN ON CACHE BOOL "" FORCE)
    set(CAPSTONE_BUILD_TESTS OFF CACHE BOOL "" FORCE)
    set(CAPSTONE_BUILD_CSTOOL OFF CACHE BOOL "" FORCE)
    set(CAPSTONE_BUILD_CSTEST OFF CACHE BOOL "" FORCE)
    set(CAPSTONE_INSTALL OFF CACHE BOOL "" FORCE)
    add_subdirectory("${translatorRoot}/3rdparty/capstone" "${CMAKE_CURRENT_BINARY_DIR}/capstone" EXCLUDE_FROM_ALL)
    target_include_directories(aps5-translator SYSTEM PRIVATE
        "${translatorRoot}/3rdparty/llvm-project/llvm/include" "${CMAKE_CURRENT_BINARY_DIR}/llvm/include")
    target_compile_definitions(aps5-translator PRIVATE __STDC_CONSTANT_MACROS __STDC_FORMAT_MACROS __STDC_LIMIT_MACROS)
    target_link_libraries(aps5-translator PRIVATE LLVMCore LLVMSupport LLVMPasses LLVMTarget LLVMTransformUtils
        LLVMAArch64CodeGen LLVMAArch64Desc LLVMAArch64Info capstone_static)
endif()

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
    add_test(NAME native_packager COMMAND ${Python3_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/../tests/test_native_packager.py)
    set_tests_properties(native_fixture_reporting native_translator_cli native_ffmpeg_fetch native_packager PROPERTIES TIMEOUT 30)
endif()
