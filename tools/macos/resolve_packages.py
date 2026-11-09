import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from time import sleep


TRANSIENT_DOWNLOAD = re.compile(
    r"error: failed downloading 'https://[^'\s]+' which is required by binary target '[^'\r\n]+': "
    r"badResponseStatusCode\((502|503|504)\)"
)


def error_diagnostics(output):
    return [line.strip() for line in output.splitlines() if re.search(r"\b(?:error|fatal):", line)]


def transient_download_failure(output):
    errors = [line for line in error_diagnostics(output) if line != "error: fatalError"]
    return bool(errors) and all(TRANSIENT_DOWNLOAD.fullmatch(line) for line in errors)


def resolve_packages(package, diagnostics, timeout=300):
    package = Path(package).resolve()
    diagnostics = Path(diagnostics).resolve()
    diagnostics.mkdir(parents=True, exist_ok=True)
    command = ["swift", "package", "--package-path", str(package), "--force-resolved-versions", "resolve"]
    data = {"schema": 1, "command": command, "inputs": {}, "attempts": [], "passed": False}
    try:
        try:
            original = {name: (package / name).read_bytes() for name in ("Package.swift", "Package.resolved")}
        except OSError as error:
            data["error"] = str(error)
            return 1
        data["inputs"] = {name: hashlib.sha256(contents).hexdigest() for name, contents in original.items()}
        for index in range(3):
            log = diagnostics / f"attempt-{index + 1}.log"
            attempt = {"number": index + 1, "log": str(log), "exit_code": None, "timed_out": False,
                       "transient_download_failure": False, "inputs_unchanged": False}
            data["attempts"].append(attempt)
            print(f"Resolving pinned Swift packages, attempt {index + 1}/3; log: {log}", flush=True)
            try:
                with log.open("wb") as output:
                    process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        attempt["timed_out"] = True
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=10)
                    attempt["exit_code"] = process.returncode
            except OSError as error:
                data["error"] = str(error)
                return 1
            text = log.read_text(encoding="utf-8", errors="replace")
            print(text, end="" if text.endswith("\n") else "\n", flush=True)
            try:
                attempt["inputs_unchanged"] = all((package / name).read_bytes() == contents
                                                  for name, contents in original.items())
            except OSError as error:
                data["error"] = str(error)
                return 1
            if not attempt["inputs_unchanged"]:
                data["error"] = "package inputs changed during resolution"
                return 1
            if attempt["timed_out"]:
                data["error"] = "package resolution timed out; tests were not run"
                return 124
            if process.returncode == 0 and not error_diagnostics(text):
                data["passed"] = True
                return 0
            attempt["transient_download_failure"] = process.returncode == 1 and transient_download_failure(text)
            if not attempt["transient_download_failure"]:
                data["error"] = "package resolution failed without an exclusively transient binary-download error"
                return 1
            if index < 2:
                delay = (10, 30)[index]
                attempt["retry_after_seconds"] = delay
                print(f"Retrying binary dependency acquisition after {delay} seconds", flush=True)
                sleep(delay)
        data["error"] = "binary dependency acquisition failed after three attempts"
        return 1
    finally:
        (diagnostics / "report.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-path", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    args = parser.parse_args()
    return resolve_packages(args.package_path, args.diagnostics)


if __name__ == "__main__":
    sys.exit(main())
