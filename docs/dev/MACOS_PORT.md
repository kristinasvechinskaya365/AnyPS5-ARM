# Apple Silicon (macOS) port: fork status

This fork combines the community macOS port from [mugurc/AnyPS5 `macos-port`](https://github.com/mugurc/AnyPS5/tree/macos-port) with the current runtime, launcher delivery fixes and the optional [native ARM64 translator](NATIVE_ARM64.md). The default installable app uses the Rosetta engine; the translator remains a separate experimental build.

Guest code stays x86-64 and runs under Rosetta 2. The relinker writes Mach-O (`--macos`), the prx libraries build as x86-64 dylibs, and Vulkan runs through MoltenVK. [Apple describes general-purpose Rosetta support through macOS 27](https://developer.apple.com/news/?id=w5ngl9k2). The later exception for older unmaintained games does not establish support for this engine.

For installation, use the [packaged app](#installing-a-packaged-app). The build instructions below are for development. The workflow builds the complete engine and universal launcher, packages their runtime dependencies, hides the source build directories and SDK, and verifies both launcher architectures and the installed copy. The [macOS validation workflow](https://github.com/kristinasvechinskaya365/AnyPS5-ARM/actions/workflows/macos-validation.yml) runs the Mac gates in sequence to reduce simultaneous hosted-runner requests and requires every gate to succeed. An ARM64 engine compile and the native translator fixtures are separate checks; neither is a game or GPU compatibility result.

## Build

```sh
git submodule update --init --recursive
cmake -S . -B build-mac -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES=x86_64 -DBUILD_TESTING=ON
ninja -C build-mac all libs
```

Requirements:

- Xcode command-line tools
- Rosetta 2
- Ninja
- the [Vulkan SDK](https://vulkan.lunarg.com/sdk/home#mac) (universal loader and MoltenVK)
- for the guest fixtures only: `brew install llvm lld`

## Run

```sh
build-mac/core/relinker/relinker --macos --to-rosetta source/eboot.bin out/eboot
python3 tools/package_macos_app.py --relinked out --game source --libs build-mac/core/libs/libs --vulkan ~/VulkanSDK/<version>/macOS Title.app
```

## Historical upstream measurements

The following measurements were inherited from the community port, reported on an M3 Pro with macOS 26.6, Vulkan SDK 1.4.363.0 and MoltenVK. They are not results from the current fork or an M2 Mac. Current regression evidence is attached to the exact commits in this fork's [Actions runs](https://github.com/kristinasvechinskaya365/AnyPS5-ARM/actions).

| Check | Result |
|---|---|
| Full build (all targets and 115 prx libraries) | builds |
| `macos_fixtures`: argv, imports, TLS, C++ exceptions, C cleanup, threads, modules, TLS across modules, `--to-intel` | 10/10 run under Rosetta |
| Breakout guest (video out, pad, audio out), relinked and packaged as `.app` | runs at about 60 fps |
| ctest, full suite on MoltenVK | 832 tests: 775 pass, 43 skip features the device lacks (64-bit atomics, Float64, mesh shaders), 14 fail |
| Dreaming Sarah (PPSA02929), relinked with `--macos --to-rosetta`, packaged as `.app` and opened from Finder | runs the animated main menu at 60 fps (about 18 fps before the write watch) |

The 14 tests that fail (macOS 26.1, MoltenVK 1.4.2):

- **Apple GPU or Metal limits (4):** `f32_dot2_rounding` needs the Float64 capability, `f32_constant_factor_tininess` gets a product flushed to zero where RDNA2 keeps the smallest normal, `float16_rounding` gets a differently rounded `v_mul_f16` product, and `pixel_interlock` needs fragment shader interlock, which SPIRV-Cross cannot write as MSL.
- **A product that should give `-0.0` gives `+0.0` (4):** `f32_denormal_flush`, `f32_output_modifier`, `packed_alu`, `sdwa_float_selectors`. See [TechnicalDebt](TechnicalDebt.md).
- **Host platform (5):** `agc_thread_priority_refused_rt`, `agc_thread_priority_refused_high` and `agc_driver_pm4_priority_refused` expect a refused priority request to throw, `agc_shared_write` cannot inspect mapping lifetimes, and `check_skips` passes from the repository root but not from the working directory ctest gives it.
- **`patched_libraries`:** loads all patched libraries and exceeds the 300 s test timeout only when the whole suite runs in parallel after a rebuild; it passes alone.

The historical run excluded `macos_fixtures` after an `int3` exit hung under Rosetta. The current foundation gate requires repeated fixture executions and retains their exit status and logs; that historical exclusion is not an accepted result for current builds.

Changes on top of the merged port:

- [GuestMemory.cpp](../../core/libs/prx/libSceAgcDriver/Execution/src/GuestMemory.cpp) and [DynamicLoader.cpp](../../core/libs/prx/libkernel/Module/src/DynamicLoader.cpp) compile on macOS. `main` had added Linux-only code to both after the port was based.
- When the device has no `robustness2.nullDescriptor` (MoltenVK), the empty slots of the typed image heaps bind zeroed [placeholder images](../../core/libs/prx/libSceAgcDriver/Graphics/src/PlaceholderImages.cpp). Before this, every draw and dispatch failed on macOS.
- Shader stages without host subgroups (MoltenVK: vertex and tessellation evaluation) run each invocation as a one-lane wave instead of failing validation.
- Guest thread destructors register through `_tlv_atexit`. libSystem's `__cxa_thread_atexit` returns void, and reading its result as an int freed a live registration twice.
- `dlsym`'s default scope finds the main program through dyld.
- Guest `mprotect` rounds to 16 KiB pages relative to the guest image's load bias. dyld only aligns a slide to 4 KiB, so rounding the absolute address protected the wrong pages (SIGBUS in Dreaming Sarah). Guest segments get maxprot rwx, and the image's no-access padding segments are registered.
- Without storage MSAA, multisampled image dimensions are emitted single-sample. This matches the single-sample images the driver creates; Dreaming Sarah's first shader clears an MSAA surface with `image_store`.
- Merged upstream #1653: no fragment barycentric on MoltenVK. SPIRV-Cross cannot write `PerVertexKHR` to MSL.
- MoltenVK has no `VK_EXT_depth_range_unrestricted`. A draw whose viewport depth leaves [0, 1] is drawn with [0, 1] when the depth cannot be observed (Unity's stencil clears in Kitaria Fables); see [TechnicalDebt](TechnicalDebt.md). Depth-tested draws with such a range still fail, with the range in the message.
- `sceKernelGetModuleInfoForUnwind` and `sceSysmoduleGetModuleInfoForUnwind` report the `eh_frame` tables of the relinked images, so a title's bundled `libc.prx` can catch C++ exceptions (Kitaria Fables' managed exceptions).
- macOS has a guest [write watch](../../core/libs/prx/libc/src/GuestWriteWatch.cpp). Without it the driver compared guest memory before every draw, a third of Dreaming Sarah's frame time; it write-protects watched pages and records the first write to each in a fault handler. See [TechnicalDebt](TechnicalDebt.md) for its costs and limits.
- Packaged titles start from Finder: `Contents/MacOS/app0` links to the game folder, where the executable loads its guest modules, and the soft open-file limit (256 for apps Finder opens) is raised when direct memory runs out of descriptors.
- MoltenVK's Metal argument buffers are enabled through `VK_EXT_layer_settings`. Together with upstream #1580 (merged here), they lift the 31 storage buffers per stage limit.
- Test fixes for macOS:
  - case-insensitive APFS in `guest_path_case`
  - the portability loader in `oversized_descriptor_sets`
  - the fixtures' temporary directory, about 720 MiB per run, is now cleaned up
  - `ProcessParametersFixture` gives a test executable process parameters through its own `__APS5DATA,__meta` section, laid out as the relinker writes it, so `dl_iterate_phdr` lists them as the main image's `PT_SCE_PROCPARAM`
- The AGC driver creates the GPU mutex before itself. Its destructor at exit locks that mutex, and macOS's `pthread_mutex_lock` fails on a destroyed one.
- libSceJson2 converts a number that `from_chars` reports out of range with `strtod_l` in the C locale. libc++'s stream extraction fails on every `ERANGE`, so an underflow to a denormal or zero was rejected as an overflow.
- When the AGC driver memory cannot be mapped at 0xF_E0000000, the libSceAgc static buffer functions throw.
- A positioned write on an `O_APPEND` regular file (`pwrite`, `pwritev`, AIO writes) appends and leaves the offset unchanged, as on FreeBSD; XNU's `pwrite` writes at the offset. See [libkernel debt](debt/libkernel.md).
- `sceKernelGetModuleList` and the module info calls also list the `.prx` libraries built from this repository, reporting their Mach-O segments, as Linux lists them through `dl_iterate_phdr`.
- `dlerror` keeps the whole message. dyld's errors list every path tried and were cut at 512 bytes, before the hint about the relinker's `--module-dir` option.

## Feasibility probes

[tools/macos/spikes](../../tools/macos/spikes) holds the probes used to choose this route:

- gs TSD reads
- AVX2, FMA and BMI2
- JIT code
- fs base: unavailable under Rosetta
- the guest arena layout
- MoltenVK features

Run them with `run.sh [libMoltenVK.dylib]`.

## Installing a packaged app

Use the app archive and checksum file from the same successful macOS build. Keep
`install.sh`, `AnyPS5-macOS.zip` and `SHA256SUMS` together, then run:

```sh
sh install.sh --archive AnyPS5-macOS.zip --checksums SHA256SUMS
```

The installer uses macOS's built-in tools and defaults to `~/Applications/AnyPS5.app`.
Successful `main` runs of [macOS validation](https://github.com/kristinasvechinskaya365/AnyPS5-ARM/actions/workflows/macos-validation.yml) carry the complete app artifact for 90 days. A manual run of [macOS installable app](https://github.com/kristinasvechinskaya365/AnyPS5-ARM/actions/workflows/macos-app.yml) on `main` rebuilds an expired package. The package records the highest minimum macOS version required by any bundled Mach-O slice, with a floor of 14.0. The installer requires macOS 14 or newer, checks that declared minimum system version,
compatible executable and code-signature integrity, and preserves an existing app
until its replacement is ready. Failed placement restores the previous app; if the
destination prevents restoration, the error names the preserved recovery copy.
`--destination DIR` selects another writable Applications folder. `--open` opens the
app after installation; it stays closed by default. `--help` lists the options.

An independently obtained archive checksum can replace the checksum file:

```sh
sh install.sh --archive AnyPS5-macOS.zip --sha256 SHA256
```

Replace `SHA256` with all 64 hexadecimal digits. A missing, duplicated or mismatched
`AnyPS5-macOS.zip` entry in `SHA256SUMS` is an error. The first installation trusts the
chosen GitHub build or release, or the separately supplied digest. An ad hoc code
signature checks bundle integrity; it does not identify the publisher or provide
Apple notarization. Browser quarantine and Gatekeeper settings are preserved.

After the fork publishes `AnyPS5-macOS.zip` and `SHA256SUMS` as release assets, a
checkout can install that release with:

```sh
sh tools/macos/install.sh --repository kristinasvechinskaya365/AnyPS5-ARM
```

This resolves the latest release once and downloads both files using that tag.
`--version TAG` selects a published tag explicitly. The default repository is this
fork; `--repository` or `ANYPS5_REPOSITORY` selects another repository. A missing fork
release does not fall back to an upstream download. Release self-updates retain the
launcher's trusted-signature checks described in [SIGNED_UPDATES](SIGNED_UPDATES.md).

The launcher can run natively on Apple Silicon. Games using the x86-64 engine need
Rosetta; the installer reports when it is missing. Install it separately with
`softwareupdate --install-rosetta`, following Apple's prompts. A packaged app and its
installation tests do not establish that a particular game or GPU workload works.
