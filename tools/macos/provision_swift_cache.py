import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from time import sleep


ASSETS = (
    {"identity": "webrtc", "target": "WebRTC", "location": "https://github.com/stasel/WebRTC.git",
     "revision": "44f36788497f701bc8c0306de3a1c2511d56019d", "version": "155.0.0",
     "manifest_blob": "606b1924718b33994aacd8bdba942f321dfe4a3a",
     "url": "https://github.com/stasel/WebRTC/releases/download/155.0.0/WebRTC-M155.xcframework.zip",
     "api_url": "https://api.github.com/repos/stasel/WebRTC/releases/assets/619787052",
     "size": 45249560, "sha256": "cb9adacf2d6bbf598d1d9e3e6c16d8fb97ca987b5b697746891828f9dc068ffe"},
    {"identity": "webrtc-xcframework", "target": "LiveKitWebRTC",
     "location": "https://github.com/livekit/webrtc-xcframework.git",
     "revision": "a41df9e9bcc7ab1d62131fda75c123ea391e5188", "version": "150.7871.3",
     "manifest_blob": "14cf4790e50b930b7b526fa373dc85561df47365",
     "url": "https://github.com/livekit/webrtc-xcframework/releases/download/150.7871.03/LiveKitWebRTC.xcframework.zip",
     "api_url": "https://api.github.com/repos/livekit/webrtc-xcframework/releases/assets/603769654",
     "size": 69116960, "sha256": "5727a03c32ed902da570b699e3983e3fb4bfe853ab4da045af70912a7db44557"},
    {"identity": "livekit-uniffi-xcframework", "target": "RustLiveKitUniFFI",
     "location": "https://github.com/livekit/livekit-uniffi-xcframework.git",
     "revision": "7a9bf91b1601d5c408ae0cac7dc2047b5a4f615e", "version": "0.1.9",
     "manifest_blob": "e25489ca6cd1a25012e344aee7006337a5b15641",
     "url": "https://github.com/livekit/livekit-uniffi-xcframework/releases/download/0.1.9/RustLiveKitUniFFI.xcframework.zip",
     "api_url": "https://api.github.com/repos/livekit/livekit-ffi-xcframework/releases/assets/530411144",
     "size": 5929982, "sha256": "8db93e8ccd9f22263c39855808da48a4d6b5d620d8f6e43ab4501ede847b23eb"},
)


def cache_key(url):
    if not url.isascii() or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.\-/]+", url):
        raise ValueError("only the pinned ASCII GitHub archive URLs are supported")
    return re.sub(r"[^A-Za-z0-9_]", "_", url)


def validate_pins(contents):
    pins = json.loads(contents)["pins"]
    identities = [pin["identity"] for pin in pins]
    if len(identities) != len(set(identities)):
        raise ValueError("Package.resolved contains duplicate package identities")
    indexed = {pin["identity"]: pin for pin in pins}
    for asset in ASSETS:
        pin = indexed.get(asset["identity"])
        if pin is None or pin.get("kind") != "remoteSourceControl" or pin.get("location") != asset["location"] or \
                pin.get("state") != {"revision": asset["revision"], "version": asset["version"]}:
            raise ValueError("Package.resolved differs from the verified binary manifest: " + asset["identity"])


def verified_file(path, asset):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("archive cache entries must be regular files with a single link")
    if metadata.st_size != asset["size"]:
        raise ValueError("archive size differs from the pinned manifest")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != asset["sha256"]:
        raise ValueError("archive SHA-256 differs from the pinned manifest")


def download(asset, destination, attempts, save, timeout):
    for number in range(1, 4):
        attempt = {"number": number, "url": asset["api_url"], "accept": "application/octet-stream",
                   "http_status": None, "exit_code": None, "timed_out": False}
        attempts.append(attempt)
        save()
        command = ["curl", "--location", "--fail", "--silent", "--show-error", "--proto", "=https",
                   "--proto-redir", "=https", "--connect-timeout", "15", "--max-time", "90",
                   "--max-filesize", str(asset["size"]), "--header", "Accept: application/octet-stream",
                   "--output", str(destination), "--write-out", "%{http_code}\n%{content_type}\n%{size_download}\n",
                   asset["api_url"]]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            attempt["timed_out"] = True
            save()
            raise ValueError("archive acquisition exceeded its process time budget") from None
        attempt["exit_code"] = result.returncode
        fields = result.stdout.splitlines()
        if len(fields) != 3 or not re.fullmatch(r"\d{3}", fields[0]) or not fields[2].isdigit():
            raise ValueError("archive acquisition returned invalid HTTP diagnostics")
        attempt["http_status"] = int(fields[0])
        attempt["bytes"] = int(fields[2])
        media_type = fields[1].split(";", 1)[0].strip().lower()
        attempt["json_response"] = media_type == "application/json" or media_type.endswith("+json")
        save()
        if result.returncode == 0:
            if attempt["http_status"] != 200 or attempt["json_response"]:
                raise ValueError("GitHub asset API did not return the pinned archive")
            verified_file(destination, asset)
            attempt["sha256"] = asset["sha256"]
            save()
            return
        transient = (result.returncode == 22 and attempt["http_status"] in (502, 503, 504)) or \
            result.returncode in (6, 7, 28, 52, 56)
        attempt["transient"] = transient
        if not transient or number == 3:
            raise ValueError("archive acquisition failed; see the structured HTTP diagnostics")
        destination.unlink(missing_ok=True)
        attempt["retry_after_seconds"] = 2 ** number
        save()
        sleep(2 ** number)


def provision(package, cache, diagnostics, timeout=100):
    package = Path(package).resolve()
    cache = Path(cache).absolute()
    diagnostics = Path(diagnostics).resolve()
    diagnostics.mkdir(parents=True, exist_ok=True)
    report = {"schema": 1, "cache_path": str(cache), "inputs": {}, "assets": [], "passed": False,
              "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    def save():
        (diagnostics / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    try:
        inputs = {name: (package / name).read_bytes() for name in ("Package.swift", "Package.resolved")}
        report["inputs"] = {name: hashlib.sha256(value).hexdigest() for name, value in inputs.items()}
        validate_pins(inputs["Package.resolved"])
        keys = [cache_key(asset["url"]) for asset in ASSETS]
        if len(keys) != len({key.casefold() for key in keys}):
            raise ValueError("binary archive URLs collide in the SwiftPM cache")
        if cache.is_symlink() or cache.resolve() == package or cache.resolve() in package.parents or \
                package in cache.resolve().parents:
            raise ValueError("use a separate private cache directory")
        if cache.exists() and (not cache.is_dir() or any(path.name != "artifacts" for path in cache.iterdir())):
            raise ValueError("use a fresh cache directory or previously verified archive entries")
        cache.mkdir(mode=0o700, exist_ok=True)
        cache = cache.resolve()
        artifacts = cache / "artifacts"
        if artifacts.is_symlink() or artifacts.exists() and not artifacts.is_dir():
            raise ValueError("the archive cache directory must not be redirected")
        artifacts.mkdir(mode=0o700, exist_ok=True)
        if any(path.name not in keys for path in artifacts.iterdir()):
            raise ValueError("the archive cache contains an unexpected entry")
        for asset, key in zip(ASSETS, keys):
            destination = artifacts / key
            entry = {**asset, "cache_key": key, "path": str(destination), "attempts": [], "verified": False}
            report["assets"].append(entry)
            save()
            if destination.exists() or destination.is_symlink():
                verified_file(destination, asset)
                entry["reused"] = True
            else:
                with tempfile.NamedTemporaryFile(prefix=".download-", dir=artifacts, delete=False) as stream:
                    temporary = Path(stream.name)
                try:
                    download(asset, temporary, entry["attempts"], save, timeout)
                    if any((package / name).read_bytes() != value for name, value in inputs.items()):
                        raise ValueError("package inputs changed during archive acquisition")
                    if destination.exists() or destination.is_symlink():
                        raise ValueError("archive destination appeared during acquisition")
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
                entry["reused"] = False
            entry["verified"] = True
            save()
        if any((package / name).read_bytes() != value for name, value in inputs.items()):
            raise ValueError("package inputs changed during archive acquisition")
        report["passed"] = True
    except (OSError, ValueError, KeyError, TypeError) as error:
        report["error_type"] = type(error).__name__
        report["error"] = str(error) if isinstance(error, ValueError) else "archive acquisition prerequisites or filesystem operation failed"
    finally:
        save()
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-path", type=Path, required=True)
    parser.add_argument("--cache-path", type=Path, required=True)
    parser.add_argument("--diagnostics", type=Path, required=True)
    args = parser.parse_args()
    return provision(args.package_path, args.cache_path, args.diagnostics)


if __name__ == "__main__":
    raise SystemExit(main())
