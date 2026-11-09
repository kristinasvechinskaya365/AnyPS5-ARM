# Native arm64 (no Rosetta): design and plan

Branch `native-arm64`. How to build and use it: [docs/native](../native/README.md). The goal is to run converted titles as arm64 processes, with no Rosetta. `main` keeps the Rosetta port ([MACOS_PORT.md](MACOS_PORT.md)).

## Where Rosetta sits today

- **Guest code:** the relinked guest x86-64 code calls host prx functions directly. dyld writes host addresses into the guest's own GOT slots, and both sides use the SysV calling convention (`APS5_VABI` is empty off Windows).
- **Host code:** host code calls guest code through plain function pointers:
  - the process entry (`Aps5StartGuest`)
  - module init/fini
  - threads (`scePthreadCreate`), Ult, Fiber
  - `atexit`, `__cxa_atexit`, thread destructors, TSD destructors, once
  - signals, C++ exception callbacks and the DWARF unwinder
  - qsort/bsearch, locale facets, guest malloc replacement
  - AvPlayer, Ngs2, Font, Agc command buffer callbacks
- **Rosetta's role:** it runs all of it, guest code and prx libraries alike, because a process has one architecture.

## Approach: translate guest code ahead of time

The relinker already decodes guest code (TLS scan, `--to-intel`). In the arm64 mode it translates every discovered guest function to arm64 at conversion time. The prx libraries build natively for arm64. A first `cmake -DCMAKE_OSX_ARCHITECTURES=arm64` build fails in only 6 of about 1250 compile steps:

- `setjmp`/`longjmp` asm
- the Fiber stack switch
- the guest TLS allocate asm
- TSC reads
- a few `__builtin_ia32_*` exports
- the 80-bit `long double` asserts

### Guest state

- **Registers:** translated code keeps the x86-64 architectural state in a per-thread `GuestState`: 16 GPRs, rip, flags, the 16 ymm registers, mxcsr, and the fs base.
- **Memory is shared:** guest memory is the host address space, so guest loads and stores are plain host loads and stores at the same addresses. No MMU is emulated, and guest pointers and host pointers are the same values.
- **Stack:** the guest stack is real memory. Guest `rsp` lives in `GuestState`, and translated functions use the native stack only for their own temporaries.

### Translation

- **Pipeline:** x86-64 is decoded with Capstone and lifted to LLVM IR, one LLVM function per guest function, operating on `GuestState`. LLVM optimizes it and emits arm64.
- **Flags:** flags are computed into `GuestState` fields; LLVM drops the dead ones.
- **SIMD:** SSE and AVX map to LLVM vector types, and 256-bit operations are lowered to two 128-bit NEON operations by LLVM.
- **Function discovery:**
  - the ELF entry, `DT_INIT`/`INIT_ARRAY`
  - exported and local symbols
  - direct call targets
  - code addresses in relocations (`R_X86_64_RELATIVE` into executable segments)
  - recursive descent over basic blocks
- **Indirect jumps:** an indirect jump inside a function dispatches over that function's known block addresses.
- **Indirect calls:** they go through a runtime lookup of guest address to native function. An unknown target stops the process with its guest address.

### Calling across the boundary

- **Guest to host:** a call whose target is outside the guest code ranges is a host function. The bridge passes rdi, rsi, rdx, rcx, r8, r9 as x0–x5, guest stack arguments 7 and 8 as x6 and x7, and xmm0–7 as v0–v7. It returns x0/x1 into rax/rdx and v0/v1 into xmm0/xmm1. Both ABIs split integer and vector arguments into separate register files, so this covers every export with up to 8 integer and 8 vector arguments passed in registers. Variadic exports differ (Apple arm64 passes variadic arguments on the stack), and so do `va_list`, structure returns (rdi vs x8) and `long double`. Those exports need per-signature bridges; there are 26 variadic `APS5_VABI` exports.
- **Host to guest:** the guest image lives in a data segment, so a host function pointer to guest code faults when it is called. The runtime's SIGBUS/SIGSEGV handler looks the address up in the function table and resumes at `aps5_enter` with the translated function in x16, so the many callback sites (the process entry through `Aps5StartGuest`, thread entries, atexit, library callbacks) need no change. `aps5_enter` builds or reuses the thread's `GuestState` and TLS block, copies the AAPCS arguments into the SysV registers and the guest stack, pushes a return address, runs the translated function, and returns rax/rdx and xmm0/xmm1 as x0/x1 and q0/q1. Faults the redirect does not own go to the previous handler (the write watch chains the same way).
- **Variadic exports:** Apple arm64 passes variadic arguments on the stack, so a host `printf(fmt, ...)` cannot read the guest's registers. On arm64 the libc exports start a System V va_list over the running guest call instead (`aps5_guest_va_start` in the runtime, `GuestVarargs.hpp` in libc) and format with the same code the Windows build uses.
- **TLS:** `fs:` accesses read `GuestState.fsBase`, which is set per thread from the guest TLS allocator. The `gs:`/pthread-key stubs of the Rosetta mode are not used.

### Hard parts, in order of risk

1. **Non-local control flow:** guest `setjmp`/`longjmp`, C++ exception landing pads (a guest unwinder resuming mid-function), and the Fiber and Ult context switches. Translated functions nest native frames, so a guest transfer to an older frame must also unwind native frames. The plan is to resume through the thread's outermost guest entry, or to switch to a dispatch-loop model if that proves insufficient.
2. **Memory ordering:** x86 is TSO and arm64 is weakly ordered. The hardware TSO mode of Apple silicon has no public interface. Guest loads and stores can be emitted as acquire and release (`LDAPR`/`STLR`); that costs throughput, so it is a translation option.
3. **Coverage:** every instruction the titles use must be lifted exactly. Differential references must match the AMD guest contract; Rosetta is not an oracle for cases where its results disagree with that contract.
4. **Tooling at conversion time:** the converter would need LLVM and a linker, about 100 MB. A direct arm64 emitter is the alternative if that size is a problem.

## Building and running the translator

The translator builds on its own because it needs LLVM and Capstone. The fixture gate requires a native arm64 Python process on macOS, LLVM 22, Capstone, the Apple SDK and the patched arm64 `libc.prx` from this branch. The vector reference runs as x86-64 under Rosetta; the translated executables run directly as arm64. Homebrew packages LLD separately from LLVM.

```sh
brew install llvm@22 lld@22 capstone cmake ninja
export ANYPS5_LLVM_BIN="$(brew --prefix llvm@22)/bin"
export PATH="$(brew --prefix lld@22)/bin:$PATH"
export SDKROOT="$(xcrun --sdk macosx --show-sdk-path)"
cmake -S tools/translator -B build-translator -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DLLVM_DIR="$(brew --prefix llvm@22)/lib/cmake/llvm" \
  -DCAPSTONE_INCLUDE_DIR="$(brew --prefix capstone)/include" \
  -DCAPSTONE_LIBRARY="$(brew --prefix capstone)/lib/libcapstone.a"
cmake --build build-translator --target aps5-translator aps5-translator-runtime --parallel 2
ctest --test-dir build-translator --output-on-failure --no-tests=error \
  -R '^native_(fixture_reporting|translator_cli|ffmpeg_fetch)$'
python3 tools/macos/fetch_ffmpeg.py --architecture arm64 \
  --destination "$PWD/build-ffmpeg-arm64" --report diagnostics/ffmpeg-acquisition.json
cmake -S . -B build-arm64 -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
  -DCMAKE_APPLE_SILICON_PROCESSOR=arm64 -DCMAKE_OSX_ARCHITECTURES=arm64 \
  -DFFMPEG_PREBUILT_DIR:PATH="$PWD/build-ffmpeg-arm64"
cmake --build build-arm64 --target core/libs/libs/libc.prx --parallel 2
python3 tools/translator/tests/run_native_fixtures.py build-translator build-arm64/core/libs/libs \
  diagnostics/native --report diagnostics/native/report.json
```

The root configure needs the branch's submodules. The `core/libs/libs/libc.prx` Ninja target builds libc and the NID patcher, then patches and signs the arm64 library without building all PRX modules. The unpatched library is not a substitute.

Root configuration also enters the existing FFmpeg dependency before selecting the libc build target. `fetch_ffmpeg.py` checks its gitlink and clean submodule revision against the pinned macOS package manifest, verifies the archive size and SHA-256, validates regular-file paths and required library/license metadata, and publishes a fresh directory atomically. It refuses an existing destination. The helper records each HTTP attempt and makes at most three attempts for selected transient HTTP or transport errors. It starts with the release URL; after a transient failure, remaining attempts use the same pinned asset through GitHub's asset API with `Accept: application/octet-stream`, without authentication. Certificate, metadata, checksum and archive-validation failures are final. The workflow bounds the acquisition step to six minutes and binds the successful report to `FFMPEG_PREBUILT_DIR` in CMake's cache. The helper also accepts `--architecture x64` for the existing x86-64 package. Its unit tests require no network. This selects the same pinned package through the upstream CMake interface without changing media-library linkage or third-party sources.

The runner requires `import`, `lifter-smoke`, `vector-smoke`, `cmpxchg-alias`, `cmpxchg-amd-register`, `lea-address-size` and `leave-operand-size`. Every case receives a pass, fail or skip result; any failed or skipped required case makes the command fail. Missing guest tools and a failed reference compilation cannot reduce the required count. Each invocation creates a new directory under the supplied work directory and retains its logs and executables. The JSON report records the host, tool versions, translator build information, input binary SHA-256 digests, each stage's output and the required result counts. Guest fixture construction still uses the shared `guesttools.py` helpers; their subprocess output is in the invoking job's log. Binary hashes identify the inputs actually used and do not imply a source commit.

`cmpxchg-alias` uses fixed x86 instructions for 60 accumulator-destination cases across 8-, 16-, 32- and 64-bit operands, including an AH source. Its reference runs under Rosetta without vector compiler flags. The separate fast reference gate requires both O0 and O1 results before building the translator.

`cmpxchg-amd-register` compiles the same source with `-DCMPXCHG_AMD_REGISTERS` for 576 separate-register successes and failures. It checks full register values and CF/PF/ZF/SF/OF. The expected checksum comes from `cmpxchg_amd_model.py`, an independent Python integer model whose source digest and execution log are recorded. This is a model executed on the Mac, not an AMD CPU executed there. The 60 and 576 cases partition the original 636 cases without dropping inputs or flags. Native AMD EPYC 9V74 runs with GCC 14.2 at O0 and O1 give `ca6fbb61f3358b09` for aliases and `475cd741284f3476` for separate registers; the latter matches the Python model.

The register model follows the [AMD64 APM Volume 3, CMPXCHG](https://www.scs.stanford.edu/~zyedidia/docs/x86/amd-manual-v3.pdf): equality writes the source to the destination, inequality writes the destination to the accumulator, and unconditional writeback is specified for memory operands. Preserving the destination's high 32 bits after a failed 32-bit register comparison is inferred from these conditional writes and verified on the EPYC host. The [Intel instruction reference](https://www.intel.com/content/dam/www/public/us/en/documents/manuals/64-ia-32-architectures-software-developer-vol-2a-manual.pdf) also assigns the destination on failure; the model does not claim that the AMD register behavior is universal across x86 vendors.

Rosetta cannot supply the register fixture's expected flags in the measured environment. In [run 42559229368](https://github.com/kristinasvechinskaya365/AnyPS5-ARM/actions/runs/42559229368), both O0 and O1 references returned the correct registers for an 8-bit failed comparison with accumulator 0 and destination 1, but returned CF/PF/SF all clear instead of all set. The same instruction wrappers from the downloaded Clang 22 reference matched all 636 original cases when executed on the AMD host. This observed flag disagreement is separate from the 32-bit vendor writeback distinction.

`lea-address-size` checks 2,800 cases: 28 LEA forms over ten base values and ten index values. It covers 32- and 64-bit address calculations, 16-, 32- and 64-bit destinations, scaled indices, signed displacements, index-only addressing, and preservation of CF/PF/ZF/SF/OF. FS/GS prefixes on LEA leave the result unchanged. These are arithmetic address checks; the fixture does not load memory through configured FS/GS bases. The [Intel instruction reference, LEA table](https://www.intel.com/content/dam/www/public/us/en/documents/manuals/64-ia-32-architectures-software-developer-vol-2a-manual.pdf) specifies that a 32-bit effective address is zero-extended for a 64-bit destination; a 16-bit address size cannot be encoded in 64-bit mode. [Volume 1, section 3.3.7](https://www.intel.com/content/dam/develop/external/us/en/documents-tps/253665-sdm-vol-11.pdf) specifies wrapping the effective address before adding the segment base. The fixture independently checks the numerical result and flags, then compares its checksum with Rosetta. AMD EPYC 9V74 runs with GCC 14.2 at O0 through O3 produce `e294d24a144bdbf2`, exit 114. The fast reference gate requires both O0 and O1 Rosetta results for this fixture and the accumulator-alias fixture before building the translator.

`leave-operand-size` checks 3,072 cases: eight LEAVE encodings, four frame alignments, twelve saved words and eight flag-producing comparisons. It checks the full restored RBP and RSP, including preservation of RBP's upper 48 bits for the 16-bit operand, and CF/PF/ZF/SF/OF. Every frame address must exceed 4 GiB. Only after checking the full registers does the checksum remove address-dependent upper bits. The [Intel LEAVE instruction reference](https://cdrdv2-public.intel.com/812383/253666-sdm-vol-2a.pdf) separates stack-address size from operand size; [Volume 1, section 3.3.7.1](https://cdrdv2-public.intel.com/819711/253665-sdm-vol-1.pdf) fixes the long-mode stack pointer at 64 bits regardless of instruction prefixes. The fixture includes `66`, `67`, their combination, and REX prefixes with W clear or set. REX.W takes precedence over `66`; `67` does not truncate the stack address. Native AMD EPYC 9V74 runs with GCC 14.2 at O0 through O3 produce `aa69bd1b27fa6a0e`, exit 14. The fast gate also requires its O0 and O1 Rosetta references before building the translator. Fault delivery and 16-bit PUSH/POP support are outside this fixture.

The LEAVE repair follows the separation checked in [QEMU's `gen_LEAVE`](https://github.com/qemu/qemu/blob/71f25c75417d31c214ba2cd26b273c114fc99987/target/i386/tcg/emit.c.inc) and [`mo_pushpop`/`mo_stacksize`](https://github.com/qemu/qemu/blob/71f25c75417d31c214ba2cd26b273c114fc99987/target/i386/tcg/translate.c): operand width controls the load and BP write, while stack width controls the address and RSP update. No QEMU source or tests are included in this project.

The paired native gate uses the current fixtures, runtime and patched libc with baseline translator `795aa430be2bbb60054c0ad2e0a0812c81f91318`. It requires the LEAVE operand-size mismatch as a completed comparison, six other passing fixtures and no skips. The candidate must pass all seven fixtures. The earlier [CMPXCHG paired run](https://github.com/kristinasvechinskaya365/AnyPS5-ARM/actions/runs/42559976665) reproduced both original register-write failures, and the [LEA paired run](https://github.com/kristinasvechinskaya365/AnyPS5-ARM/actions/runs/42564001817) reproduced the address-size failure. Those fixtures remain required passes in the current pair.

`aps5-translator --build-info` reports the linked LLVM version, Capstone API version, target triple, CPU and optimization level. The default CPU remains `apple-m1`. `--cpu apple-m2` is optional on both the translator and the fixture runner; the translator rejects CPU names unsupported by its LLVM build before reading the guest or creating output. Selecting a CPU does not establish instruction, ABI or memory-order correctness beyond the fixtures exercised.

`aps5-translator <eboot.elf> <guest.o>` writes one arm64 object with the lifted functions, the guest image and the function table. The image's relocated slots are symbolic, so the host linker binds the guest's imports to the NID exports of the arm64 prx libraries. Linking it with `libaps5-translator-runtime.a` and the needed `.prx` files gives a native executable.

## Milestones

1. ✅ The prx libraries build for arm64. The x86-only parts throw until they are ported.
2. ✅ The smallest fixture (`import_fixture`: `puts` and `exit` through the GOT) runs as an arm64 process.
2b. ✅ DOOM (homebrew, `PPSA99666`) plays natively: launcher, attract demo and E1M1 at the game's 35 FPS, rendered through AGC, with no Rosetta in the process ([screenshot](../games/img/doom-native-e1m1.jpg)). What it took: SIMD/BMI lifting checked against Rosetta (`vector_smoke.c`), functions found through rip-relative code addresses and switch jump tables, guest TLS and image metadata (`__APS5DATA,__meta` for `dl_iterate_phdr`).
3. The C and C++ fixtures run: TLS, threads, modules, exceptions.
4. Breakout runs with video, pad and audio.
5. Dreaming Sarah runs.
