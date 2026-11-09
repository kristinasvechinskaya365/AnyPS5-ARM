import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
import tempfile
from pathlib import Path

from bundle_minimum import check_declared, inspect


def command(arguments, timeout=30, environment=None):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, env=environment)
    if result.returncode:
        raise ValueError(f"{arguments[0]} exited {result.returncode}: {result.stderr.strip()}")
    if environment is not None and result.stderr.strip():
        raise ValueError(f"{arguments[0]} produced unexpected diagnostics: {result.stderr.strip()}")
    return result.stdout


def verify(app, revision, repository):
    app = app.resolve(strict=True)
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("AnyPS5Commit") != revision or info.get("AnyPS5Repository") != repository:
        raise ValueError("the packaged source identity does not match the tested commit and repository")
    if info.get("CFBundleIdentifier") != "org.anyps5.launcher":
        raise ValueError("unexpected launcher bundle identifier")
    command(["codesign", "--verify", "--deep", "--strict", str(app)], timeout=120)
    minimum = inspect(app)
    declared = info.get("LSMinimumSystemVersion")
    check_declared(minimum, declared)
    toolkit = app / "Contents/Resources/toolkit"
    libraries = sorted((toolkit / "libs").glob("*.prx"))
    binaries = []
    for binary in minimum["binaries"]:
        path = app / binary["path"]
        architectures = binary["architectures"]
        command(["codesign", "--verify", "--strict", str(path)])
        dependencies = {}
        for architecture in architectures:
            dependencies[architecture] = command(["otool", "-arch", architecture, "-L", str(path)])
            for line in dependencies[architecture].splitlines():
                if not line.startswith(("\t", " ")):
                    continue
                dependency = line.strip().split(" (", 1)[0]
                if dependency.startswith("/") and not dependency.startswith(("/usr/lib/", "/System/Library/")):
                    raise ValueError(f"external runtime dependency in {path.name}: {dependency}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        binaries.append({"path": str(path.relative_to(app)), "sha256": digest,
                         "architectures": architectures, "dependencies": dependencies,
                         "minimum_macos": binary["minimum_macos"],
                         "uuids": command(["dwarfdump", "--uuid", str(path)])})
    environment = dict(os.environ)
    for name in ("DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_FALLBACK_FRAMEWORK_PATH", "ANYPS5_TOOLKIT"):
        environment.pop(name, None)
    checks = []
    with tempfile.TemporaryDirectory(prefix="anyps5-app-check-") as temporary:
        library = Path(temporary) / "empty-library"
        library.mkdir()
        for architecture in ("arm64", "x86_64"):
            output = command(["arch", "-" + architecture, str(app / "Contents/MacOS/AnyPS5"), "--verify-installation"],
                             timeout=120, environment=environment)
            report = json.loads(output)
            if report.get("repository") != repository or report.get("revision") != revision or report.get("libraryCount") != len(libraries):
                raise ValueError(f"the {architecture} launcher reported different packaged contents")
            if command(["arch", "-" + architecture, str(app / "Contents/MacOS/anyps5-tool"), "scan", str(library)],
                       environment=environment).strip():
                raise ValueError(f"the {architecture} installer reported games in an empty library")
            checks.append({"architecture": architecture, "installation": report, "empty_scan_passed": True})
    relinker = command(["arch", "-x86_64", str(toolkit / "relinker"), "--version"], environment=environment)
    if not relinker.strip():
        raise ValueError("the packaged relinker did not identify itself")
    return {"passed": True, "revision": revision, "repository": repository, "app": str(app),
            "minimum_macos": minimum["minimum_macos"], "declared_minimum_macos": declared,
            "library_count": len(libraries), "binaries": binaries, "executions": checks, "relinker": relinker}


def main():
    parser = argparse.ArgumentParser(description="Verify a complete packaged macOS launcher outside its build tree")
    parser.add_argument("app", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("--revision must be a complete Git commit SHA")
    result = {"passed": False, "revision": args.revision, "repository": args.repository}
    try:
        result = verify(args.app, args.revision, args.repository)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if not result["passed"]:
        parser.exit(1, result["error"] + "\n")
    print(f"verified {len(result['binaries'])} binaries, {result['library_count']} engine libraries and both launcher architectures")


if __name__ == "__main__":
    main()
