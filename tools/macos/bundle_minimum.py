import argparse
import json
from pathlib import Path
import re
import subprocess


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,3}){0,2}", value):
        raise ValueError(f"invalid macOS version: {value!r}")
    parts = tuple(int(part) for part in value.split("."))
    parts += (0,) * (3 - len(parts))
    if not 1 <= parts[0] <= 65535 or any(part > 255 for part in parts[1:]):
        raise ValueError(f"invalid macOS version: {value!r}")
    return parts


def formatted(value):
    return ".".join(str(part) for part in (value if value[2] else value[:2]))


def minimum_from_load_commands(output):
    records = []
    for block in re.split(r"(?m)^Load command [0-9]+\s*$", output)[1:]:
        commands = re.findall(r"(?m)^\s+cmd\s+(\S+)\s*$", block)
        if len(commands) != 1:
            raise ValueError("ambiguous Mach-O load command")
        command = commands[0]
        if command != "LC_BUILD_VERSION" and not command.startswith("LC_VERSION_MIN_"):
            continue
        if command not in ("LC_BUILD_VERSION", "LC_VERSION_MIN_MACOSX"):
            raise ValueError(f"non-macOS minimum-version metadata: {command}")
        if "Incorrect size" in block or "extends past end" in block:
            raise ValueError("invalid Mach-O minimum-version load command")
        if command == "LC_BUILD_VERSION":
            platforms = re.findall(r"(?m)^\s+platform\s+(\S+)\s*$", block)
            if len(platforms) != 1 or platforms[0] not in ("1", "MACOS"):
                raise ValueError("missing, ambiguous or non-macOS build platform")
        key = "minos" if command == "LC_BUILD_VERSION" else "version"
        minima = re.findall(r"(?m)^\s+" + key + r"\s+(\S+)\s*$", block)
        if len(minima) != 1:
            raise ValueError("missing or ambiguous Mach-O minimum macOS version")
        records.append(version(minima[0]))
    if len(records) != 1:
        raise ValueError("expected exactly one macOS minimum-version load command per slice")
    return records[0]


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    if result.returncode or result.stderr.strip():
        raise ValueError(f"{arguments[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def bundled_binaries(app):
    toolkit = app / "Contents/Resources/toolkit"
    libraries = sorted((toolkit / "libs").glob("*.prx"))
    if not libraries:
        raise ValueError("no engine libraries were packaged")
    files = [(app / "Contents/MacOS" / name, ["arm64", "x86_64"]) for name in ("AnyPS5", "anyps5-tool")]
    files.extend((app / f"Contents/Frameworks/{name}.framework/{name}", ["arm64", "x86_64"])
                 for name in ("WebRTC", "LiveKitWebRTC", "RustLiveKitUniFFI"))
    files.extend((path, ["x86_64"]) for path in [toolkit / "relinker", *libraries,
                 toolkit / "vulkan/libvulkan.1.dylib", toolkit / "vulkan/libMoltenVK.dylib"])
    return files


def inspect(app):
    app = app.resolve(strict=True)
    required = version("14.0")
    binaries = []
    for path, expected in bundled_binaries(app):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(app) or not resolved.is_file():
            raise ValueError(f"invalid bundled binary: {path}")
        command(["lipo", str(path), "-verify_arch", *expected])
        architectures = command(["lipo", "-archs", str(path)]).split()
        if (not architectures or len(architectures) != len(set(architectures)) or
                any(not re.fullmatch(r"[A-Za-z0-9_]+", architecture) for architecture in architectures) or
                not set(expected).issubset(architectures)):
            raise ValueError(f"invalid or missing architectures in {path.name}: {architectures}")
        minima = {}
        for architecture in architectures:
            try:
                minimum = minimum_from_load_commands(command(["otool", "-arch", architecture, "-l", str(path)]))
            except ValueError as error:
                raise ValueError(f"{path.relative_to(app)} ({architecture}): {error}") from error
            minima[architecture] = formatted(minimum)
            required = max(required, minimum)
        binaries.append({"path": str(path.relative_to(app)), "architectures": architectures, "minimum_macos": minima})
    return {"minimum_macos": formatted(required), "binaries": binaries}


def check_declared(report, declared):
    if version(declared) < version(report["minimum_macos"]):
        raise ValueError(f"the app declares macOS {declared}, but its bundled binaries require {report['minimum_macos']}")


def main():
    parser = argparse.ArgumentParser(description="Read every bundled Mach-O slice's minimum macOS version")
    parser.add_argument("app", type=Path)
    parser.add_argument("--declared")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = inspect(args.app)
        if args.declared is not None:
            check_declared(report, args.declared)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"bundle minimum macOS: {error}\n")
    print(report["minimum_macos"])


if __name__ == "__main__":
    main()
