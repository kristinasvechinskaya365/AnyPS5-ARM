"""Translates the macOS fixtures to native arm64 and runs them without Rosetta.

usage: run_native_fixtures.py <build-translator> <arm64 libs directory> [work directory]"""
import argparse
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE.parents[2] / "core/relinker/relinker/tests/macos"
sys.path.insert(0, str(FIXTURES))

import guesttools
from import_fixture import fixture as import_fixture


VECTOR_FLAGS = ["-mavx2", "-mfma", "-mbmi", "-mbmi2", "-mlzcnt", "-mpopcnt"]
REQUIRED = ("import", "lifter-smoke", "vector-smoke")
COMPILED_TOOLS = ("guest_clang", "guest_clangxx", "llvm_nm", "llvm_objcopy", "guest_linker")
TIMEOUT = 30


class FixtureFailure(RuntimeError):
    def __init__(self, stage, reason):
        super().__init__(reason)
        self.stage = stage


def file_identity(path):
    path = pathlib.Path(path).resolve()
    result = {"path": str(path), "sha256": None}
    try:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1 << 20), b""):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()
    except OSError as error:
        result["error"] = str(error)
    return result


def host_identity():
    host = {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "macos": platform.mac_ver()[0], "python": platform.python_version(), "cpu": None}
    if host["system"] == "Darwin":
        try:
            result = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                host["cpu"] = result.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return host


def tool_identity(path, version=True):
    result = {"path": str(pathlib.Path(path).absolute()) if path else None,
              "resolved_path": str(pathlib.Path(path).resolve()) if path else None, "available": False, "version": None}
    if not path or not pathlib.Path(path).is_file() or not os.access(path, os.X_OK):
        result["error"] = "executable not found"
        return result
    if not version:
        result["available"] = True
        return result
    try:
        probe = subprocess.run([str(path), "--version"], capture_output=True, text=True, errors="replace", timeout=5)
        result["version"] = probe.stdout.strip() or probe.stderr.strip() or None
        result["available"] = probe.returncode == 0 and result["version"] is not None
        if not result["available"]:
            result["error"] = f"version probe exited {probe.returncode}"
    except (OSError, subprocess.TimeoutExpired) as error:
        result["error"] = str(error)
    return result


def toolchain_identity():
    llvm = pathlib.Path(guesttools.LLVM)
    paths = {"guest_clang": llvm / "clang", "guest_clangxx": llvm / "clang++", "llvm_nm": llvm / "llvm-nm",
             "llvm_objcopy": llvm / "llvm-objcopy", "guest_linker": shutil.which("ld.lld"), "native_linker": shutil.which("clang++")}
    tools = {name: tool_identity(path) for name, path in paths.items()}
    tools["reference_arch"] = tool_identity(shutil.which("arch"), version=False)
    return tools


def run_command(command, directory, stage, commands, check=True, cwd=None):
    command = [str(argument) for argument in command]
    record = {"stage": stage, "command": command, "returncode": None}
    commands.append(record)
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=TIMEOUT, cwd=cwd)
        stdout, stderr = result.stdout, result.stderr
        record["returncode"] = result.returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        stdout = getattr(error, "stdout", None) or ""
        stderr = getattr(error, "stderr", None) or ""
        stdout = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else stdout
        stderr = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
        record["error"] = str(error)
        result = None
    record["seconds"] = round(time.monotonic() - started, 3)
    for stream, contents in (("stdout", stdout), ("stderr", stderr)):
        path = directory / f"{stage}.{stream}.log"
        path.write_text(contents, encoding="utf-8")
        record[stream] = str(path)
    if result is None:
        raise FixtureFailure(stage, record["error"])
    if check and result.returncode != 0:
        raise FixtureFailure(stage, f"exit {result.returncode}: {stderr.strip()[-600:]}")
    return result


def compiled_guest(directory, source, flags):
    guesttools.compile(str(HERE / source), str(directory / "main.o"), extra=["-x", "c", *flags])
    names = guesttools.symbols(str(directory / "main.o"), "-u")
    guesttools.nidify(str(directory / "main.o"), str(directory / "main.nid.o"))
    guesttools.stub_library(names, str(directory / "libc.prx"), "libc.prx")
    guesttools.link_executable([str(directory / "main.nid.o")], [str(directory / "libc.prx")], str(directory / "eboot.elf"))


def host_reference(directory, tools, commands):
    binary = directory / "vector-host"
    run_command([tools["guest_clang"]["path"], "-arch", "x86_64", "-O0", *VECTOR_FLAGS,
                 "-DMAIN_WRAPPER", HERE / "vector_smoke.c", "-o", binary], directory, "reference-build", commands)
    result = run_command([tools["reference_arch"]["path"], "-x86_64", binary], directory, "reference-run", commands, check=False)
    output = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{16}", output) or result.returncode != (int(output, 16) & 0x7f):
        raise FixtureFailure("reference-run", f"reference did not produce its checksum and matching exit code: exit {result.returncode}, output {output!r}")
    return {"exit_code": result.returncode, "stdout": output}


def check_arm64_executable(path):
    with path.open("rb") as file:
        header = file.read(32)
    if len(header) != 32 or struct.unpack_from("<I", header)[0] != 0xFEEDFACF or \
            struct.unpack_from("<I", header, 4)[0] != 0x0100000C or struct.unpack_from("<I", header, 12)[0] != 2:
        raise FixtureFailure("native-architecture", "linked fixture is not a thin arm64 Mach-O executable")


def translate_and_run(build, libraries, directory, expected, tools, cpu, result):
    commands = result["commands"]
    run_command([build / "aps5-translator", directory / "eboot.elf", directory / "guest.o", "--cpu", cpu],
                directory, "translate", commands)
    (directory / "libs").mkdir()
    shutil.copy2(libraries / "libc.prx", directory / "libs/libc.prx")
    run_command([tools["native_linker"]["path"], "-arch", "arm64", directory / "guest.o", build / "libaps5-translator-runtime.a",
                 directory / "libs/libc.prx", "-Wl,-rpath,@executable_path/libs", "-o", directory / "eboot"], directory, "native-link", commands)
    check_arm64_executable(directory / "eboot")
    result["executable"] = file_identity(directory / "eboot")
    executed = run_command([directory / "eboot"], directory, "native-run", commands, check=False, cwd=directory)
    actual = {"exit_code": executed.returncode, "stdout": executed.stdout.strip()}
    result["actual"] = actual
    if actual != expected:
        raise FixtureFailure("compare", f"actual {actual!r}, expected {expected!r}")


def write_report(path, report):
    counts = {status: sum(result["status"] == status for result in report["fixtures"]) for status in ("pass", "fail", "skip")}
    counts["required"] = len(REQUIRED)
    report["counts"] = counts
    report["complete"] = [result["name"] for result in report["fixtures"]] == list(REQUIRED)
    report["passed"] = report["complete"] and counts["pass"] == len(REQUIRED)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as temporary:
        temporary.write(json.dumps(report, indent=2) + "\n")
    try:
        pathlib.Path(temporary.name).replace(path)
    finally:
        pathlib.Path(temporary.name).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run every required native arm64 fixture and write an explicit result for each.")
    parser.add_argument("build", type=pathlib.Path)
    parser.add_argument("libraries", type=pathlib.Path)
    parser.add_argument("work", nargs="?", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path, help="JSON report path (default: the new run directory's report.json)")
    parser.add_argument("--cpu", default="apple-m1", help="LLVM CPU name, validated by the translator; default: apple-m1")
    args = parser.parse_args(argv)
    build, libraries = args.build.resolve(), args.libraries.resolve()
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix="anyps5-native-", dir=args.work)).resolve()
    report_file = args.report.resolve() if args.report else work / "report.json"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    host, tools = host_identity(), toolchain_identity()
    provenance = {"host": host, "tools": tools, "requested_cpu": args.cpu, "translator": file_identity(build / "aps5-translator"),
                  "runtime": file_identity(build / "libaps5-translator-runtime.a"), "libc": file_identity(libraries / "libc.prx"),
                  "runner": file_identity(pathlib.Path(__file__)), "guesttools": file_identity(pathlib.Path(guesttools.__file__)),
                  "reference": {"execution": "x86_64 under Rosetta", "optimization": "O0", "flags": VECTOR_FLAGS}}
    report = {"schema_version": 1, "suite": "native-arm64", "required_fixtures": list(REQUIRED), "work": str(work),
              "provenance": provenance, "preflight_commands": [], "fixtures": []}
    write_report(report_file, report)
    blockers = []
    if host["system"] != "Darwin" or host["machine"] != "arm64":
        blockers.append("requires a native arm64 Python process on macOS")
    for name in ("translator", "runtime", "libc"):
        if provenance[name]["sha256"] is None:
            blockers.append(f"{name}: {provenance[name].get('error', 'file unavailable')}")
    if not os.access(build / "aps5-translator", os.X_OK):
        blockers.append("translator is not executable")
    if not tools["native_linker"]["available"]:
        blockers.append("native clang++ linker is unavailable")
    translator_error = None
    if not blockers:
        try:
            info = run_command([build / "aps5-translator", "--build-info", "--cpu", args.cpu], work, "translator-info", report["preflight_commands"])
            decoded = json.loads(info.stdout)
            if not isinstance(decoded, dict) or type(decoded.get("schema_version")) is not int or decoded["schema_version"] != 1 or \
                    decoded.get("target_cpu") != args.cpu or decoded.get("target_triple") != "arm64-apple-macosx13.0.0" or \
                    not isinstance(decoded.get("llvm_version"), str) or not decoded["llvm_version"]:
                raise ValueError("translator build info is missing or does not match the selected target")
            provenance["translator"]["build_info"] = decoded
        except (FixtureFailure, ValueError) as error:
            translator_error = str(error)
    for name in REQUIRED:
        result = {"name": name, "required": True, "status": "skip", "stage": "prerequisites", "reason": "", "commands": []}
        started = time.monotonic()
        directory = work / name
        directory.mkdir()
        try:
            needed = [] if name == "import" else list(COMPILED_TOOLS)
            if name == "vector-smoke":
                needed.append("reference_arch")
            missing = [tool for tool in needed if not tools[tool]["available"]]
            if blockers or missing:
                result["reason"] = "; ".join(blockers + (["missing tools: " + ", ".join(missing)] if missing else []))
                continue
            if translator_error:
                raise FixtureFailure("translator-info", translator_error)
            result["stage"] = "fixture-build"
            if name == "import":
                result["source"] = file_identity(FIXTURES / "import_fixture.py")
                (directory / "eboot.elf").write_bytes(import_fixture())
                expected = {"exit_code": 0, "stdout": "hello from a PS5 ELF on macOS"}
            else:
                source = "lifter_smoke.c" if name == "lifter-smoke" else "vector_smoke.c"
                result["source"] = file_identity(HERE / source)
                expected = {"exit_code": 28, "stdout": "3b0c6e4bf8726a1c"} if name == "lifter-smoke" else host_reference(directory, tools, result["commands"])
                compiled_guest(directory, source, () if name == "lifter-smoke" else VECTOR_FLAGS)
            result["expected"] = expected
            result["stage"] = "translate"
            translate_and_run(build, libraries, directory, expected, tools, args.cpu, result)
            result.update(status="pass", stage="compare")
        except FixtureFailure as error:
            result.update(status="fail", stage=error.stage, reason=str(error))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            result.update(status="fail", reason=f"{type(error).__name__}: {error}")
        finally:
            result["seconds"] = round(time.monotonic() - started, 3)
            report["fixtures"].append(result)
            write_report(report_file, report)
            print(f"{result['status'].upper()} {name} (required)" + (f": {result['reason']}" if result["reason"] else ""), flush=True)
    counts = report["counts"]
    print(f"required={counts['required']} pass={counts['pass']} fail={counts['fail']} skip={counts['skip']}; report: {report_file}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
