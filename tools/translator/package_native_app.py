"""Translates a title's eboot to arm64 and packages it as a macOS application that runs without Rosetta.

    python3 tools/translator/package_native_app.py --eboot <eboot.bin|eboot.elf> --game <dir> \\
        --libs <build-arm64/core/libs/libs> --vulkan <Vulkan SDK macOS dir> [--translator <build-translator>] <Title.app>

--eboot is the title's executable, a decrypted SELF (unwrapped here) or a plain ELF. --game is the title's
folder (sce_sys and its data), copied as app0. --libs is the arm64 prx directory and --vulkan the macOS
directory of a Vulkan SDK. aps5-translator lifts the eboot to an arm64 object; missing libraries or
imports stop packaging. The bundle has the same layout
as tools/package_macos_app.py makes for the Rosetta mode, without the x86 game and MoltenVK patches.

Guest PRX/SPRX modules are not translated and are rejected before conversion. LLVM tools are resolved
from --llvm-bin or PATH; --linker selects the native clang++ command. Existing bundles are refused."""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS))
import package_macos_app

NID = re.compile(r"_[A-Za-z0-9+-]{11}")


def resolve_tools(llvm_bin=None, linker=None):
    result = {}
    for name in ("llvm-objdump", "llvm-readelf", "llvm-nm", "clang++"):
        requested = linker if name == "clang++" and linker else str(llvm_bin / name) if llvm_bin and name != "clang++" else name
        found = shutil.which(requested)
        if found is None:
            raise RuntimeError(f"Required executable is unavailable: {requested}")
        result[name] = Path(found).resolve()
    return result


def require_moduleless_game(game):
    if not game.is_dir():
        raise RuntimeError(f"Game directory is unavailable: {game}")
    visited = set()

    def fail(error):
        raise error

    for directory, children, files in os.walk(game, followlinks=True, onerror=fail):
        metadata = Path(directory).stat()
        identity = metadata.st_dev, metadata.st_ino
        if identity in visited:
            children.clear()
            continue
        visited.add(identity)
        for name in files:
            if Path(name).suffix.lower() in (".prx", ".sprx"):
                raise RuntimeError(f"Native packaging cannot translate guest modules: {Path(directory) / name}")


def elf_of(eboot, work):
    if eboot.read_bytes()[:4] == b"\x7fELF":
        return eboot
    elf = work / "eboot.elf"
    subprocess.run([sys.executable, str(TOOLS / "unwrap_self.py"), str(eboot), str(elf)], check=True)
    return elf


def exported_names(libraries, tools):
    """The names dyld binds against. The NID patcher renames exports in the export trie only, so the
    symbol table (nm) still shows the original names."""
    names = set()
    for library in libraries:
        listing = subprocess.run([str(tools["llvm-objdump"]), "--macho", "--exports-trie", str(library)], capture_output=True, text=True, check=True).stdout
        for line in listing.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0].startswith("0x"):
                names.add(fields[1])
    return names


def needed_libraries(elf, libs, tools):
    listing = subprocess.run([str(tools["llvm-readelf"]), "-d", str(elf)], capture_output=True, text=True, check=True).stdout
    needed = re.findall(r"Shared library: \[(.+?)\]", listing)
    names = list(dict.fromkeys(["libkernel.prx", "libc.prx", *needed]))
    if any(Path(name).name != name or "\\" in name for name in names):
        raise RuntimeError("Native guest library dependency is not a plain filename")
    missing = [name for name in names if not (libs / name).is_file()]
    if missing:
        raise RuntimeError(f"Native guest libraries are unavailable: {', '.join(missing)}")
    return [libs / name for name in names]


def build_executable(elf, libs, translator, work, tools):
    libraries = needed_libraries(elf, libs, tools)
    guest = work / "guest.o"
    translated = subprocess.run([str(translator / "aps5-translator"), str(elf), str(guest)], capture_output=True, text=True)
    print(translated.stderr.strip())
    if translated.returncode != 0:
        raise RuntimeError("translation failed")
    undefined = subprocess.run([str(tools["llvm-nm"]), "-u", "--format=just-symbols", str(guest)], capture_output=True, text=True, check=True).stdout.split()
    exported = exported_names(libraries, tools)
    missing = sorted(name for name in set(undefined) - exported if NID.fullmatch(name))
    if missing:
        raise RuntimeError(f"Native imports have no export: {' '.join(name[1:] for name in missing)}")
    output = work / "relinked"
    output.mkdir()
    subprocess.run([str(tools["clang++"]), "-arch", "arm64", str(guest), str(translator / "libaps5-translator-runtime.a"),
                    *map(str, libraries), "-Wl,-rpath,@executable_path/libs", "-o", str(output / "eboot")], check=True)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eboot", required=True, type=Path)
    parser.add_argument("--game", required=True, type=Path)
    parser.add_argument("--libs", required=True, type=Path)
    parser.add_argument("--vulkan", required=True, type=Path)
    parser.add_argument("--translator", type=Path, default=Path("build-translator"))
    parser.add_argument("--llvm-bin", type=Path)
    parser.add_argument("--linker")
    parser.add_argument("bundle", type=Path)
    arguments = parser.parse_args(argv)
    require_moduleless_game(arguments.game)
    if arguments.bundle.suffix != ".app" or arguments.bundle.exists() or arguments.bundle.is_symlink():
        raise RuntimeError(f"Native output must be a new .app bundle: {arguments.bundle}")
    if arguments.bundle.resolve().is_relative_to(arguments.game.resolve()):
        raise RuntimeError("Native output must be outside the source game directory")
    tools = resolve_tools(arguments.llvm_bin, arguments.linker)
    with tempfile.TemporaryDirectory(prefix="anyps5-native-") as directory:
        work = Path(directory)
        relinked = build_executable(elf_of(arguments.eboot, work), arguments.libs, arguments.translator, work, tools)
        with tempfile.TemporaryDirectory(prefix=".anyps5-native-", dir=arguments.bundle.parent) as staging:
            bundle = Path(staging) / arguments.bundle.name
            name, identifier = package_macos_app.package(relinked, arguments.game, arguments.libs, arguments.vulkan, None, bundle, patch=False)
            bundle.rename(arguments.bundle)
    print(f"{arguments.bundle}: {name} ({identifier}), arm64")


if __name__ == "__main__":
    main()
