import argparse
import errno
import hashlib
import http.client
import json
from pathlib import Path, PurePosixPath
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


REVISION = "9ac4cfd195f192ed8b08566f49c28dbb48d08341"
UPSTREAM = "https://github.com/KytyPS5/ext-ffmpeg-core"
PACKAGES = {
    "arm64": {"asset_id": 567718242, "sha256": "458933af900b2bf0bf3d4422e3ae1c7aef46f671cd4af99c42ffe99d43ca9170", "size": 13316412},
    "x64": {"asset_id": 567718257, "sha256": "b92c60ac99f9b2277f62d42e7424c24bc991d5723f4fa388a5d2e452be18f40b", "size": 14864759},
}
LIBRARIES = ("avcodec", "avformat", "avutil", "avfilter", "swscale", "swresample")
REQUIRED = {*(f"lib/lib{name}.a" for name in LIBRARIES), "include/libavcodec/avcodec.h",
            "share/ffmpeg/SOURCE.txt", "share/ffmpeg/copyright"}
MAX_MEMBERS = 2048
MAX_EXPANDED = 256 * 1024 * 1024
MAX_MEMBER = 128 * 1024 * 1024
ATTEMPTS = 3


class FetchError(Exception):
    pass


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise FetchError("FFmpeg download redirected outside HTTPS")
        return super().redirect_request(request, response, code, message, headers, url)


def public_url(url):
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(report, stream, indent=2)
        stream.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def repository_identity(repo):
    def git(directory, *arguments):
        return subprocess.check_output(["git", "-C", str(directory), *arguments], text=True, stderr=subprocess.PIPE, timeout=10).strip()

    entry = git(repo, "ls-tree", "HEAD", "--", "3rdparty/ffmpeg-core")
    fields = entry.split()
    if fields != ["160000", "commit", REVISION, "3rdparty/ffmpeg-core"]:
        raise FetchError("FFmpeg gitlink differs from the verified package manifest; update the manifest for the new dependency")
    submodule = repo / "3rdparty/ffmpeg-core"
    if git(submodule, "rev-parse", "HEAD") != REVISION or git(submodule, "status", "--porcelain", "--untracked-files=no"):
        raise FetchError("FFmpeg submodule must be clean and checked out at the manifest revision")
    return {"source_revision": git(repo, "rev-parse", "HEAD"), "gitlink_revision": REVISION, "submodule_revision": REVISION}


def transient(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 429, 500, 502, 503, 504)
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(cause, ssl.SSLError):
        return False
    if isinstance(cause, (TimeoutError, ConnectionError, http.client.IncompleteRead)):
        return True
    return isinstance(cause, OSError) and cause.errno in (
        errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.ECONNREFUSED,
        errno.EHOSTUNREACH, errno.ENETUNREACH, socket.EAI_AGAIN)


def download(asset, archive, report, save):
    opener = urllib.request.build_opener(HttpsRedirect())
    for number in range(1, ATTEMPTS + 1):
        started = time.monotonic()
        route = "release" if number == 1 else "asset-api"
        url = asset["url"] if number == 1 else asset["api_url"]
        headers = {} if number == 1 else {"Accept": "application/octet-stream"}
        attempt = {"number": number, "route": route, "url": url, "accept": headers.get("Accept"), "bytes": 0, "http_status": None}
        report["attempts"].append(attempt)
        save()
        digest = hashlib.sha256()
        retry = False
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response, archive.open("wb") as output:
                attempt["http_status"] = response.status
                attempt["response_url"] = public_url(response.geturl())
                attempt["content_length"] = response.headers.get("Content-Length")
                attempt["content_type"] = response.headers.get("Content-Type")
                if response.status != 200:
                    raise FetchError(f"unexpected FFmpeg HTTP status {response.status}")
                media_type = (attempt["content_type"] or "").split(";", 1)[0].strip().lower()
                if route == "asset-api" and (media_type == "application/json" or media_type.endswith("+json")):
                    raise FetchError("GitHub asset API returned metadata instead of package bytes")
                while block := response.read1(1024 * 1024):
                    attempt["bytes"] += len(block)
                    if attempt["bytes"] > asset["size"]:
                        raise FetchError("FFmpeg download exceeds the pinned package size")
                    output.write(block)
                    digest.update(block)
                    if time.monotonic() - started > 60:
                        raise TimeoutError("FFmpeg download exceeded its transfer time budget")
            attempt["sha256"] = digest.hexdigest()
        except (OSError, urllib.error.URLError, http.client.IncompleteRead, FetchError) as error:
            if isinstance(error, urllib.error.HTTPError):
                attempt["http_status"] = error.code
                attempt["retry_after"] = error.headers.get("Retry-After") if error.headers else None
                error.close()
            cause = error.reason if isinstance(error, urllib.error.URLError) else error
            attempt["error_type"] = type(error).__name__
            attempt["cause_type"] = type(cause).__name__
            attempt["os_errno"] = getattr(cause, "errno", None)
            attempt["transient"] = transient(error)
            archive.unlink(missing_ok=True)
            retry = attempt["transient"] and number < ATTEMPTS
            if not retry:
                raise FetchError(f"FFmpeg download failed after {number} attempt(s); see the HTTP diagnostics") from error
        finally:
            attempt["seconds"] = round(time.monotonic() - started, 3)
            save()
        if retry:
            delay = 2 ** number
            if str(attempt.get("retry_after", "")).isdigit():
                delay = min(10, max(delay, int(attempt["retry_after"])))
            attempt["retry_delay_seconds"] = delay
            save()
            time.sleep(delay)
            continue
        if attempt["bytes"] != asset["size"] or attempt["sha256"] != asset["sha256"]:
            raise FetchError("FFmpeg package size or SHA-256 differs from the pinned manifest")
        return


def unpack(archive, destination, architecture):
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_EXPANDED:
            raise FetchError("FFmpeg archive exceeds extraction limits")
        paths = {}
        files = set()
        for item in members:
            name = item.filename.rstrip("/")
            parts = name.split("/")
            mode = stat.S_IFMT(item.external_attr >> 16)
            expected_mode = stat.S_IFDIR if item.is_dir() else stat.S_IFREG
            if any(part in ("", ".", "..") or ":" in part for part in parts) or "\\" in name or "\0" in item.orig_filename or \
                    parts[0] not in ("include", "lib", "share") or mode not in (0, expected_mode) or \
                    item.flag_bits & 1 or item.file_size > MAX_MEMBER or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise FetchError("FFmpeg archive contains an unsafe or unsupported member")
            key = name.casefold()
            if key in paths:
                raise FetchError("FFmpeg archive contains duplicate or case-colliding paths")
            paths[key] = item
            if not item.is_dir():
                files.add(name)
        if not REQUIRED.issubset(files):
            raise FetchError("FFmpeg archive is missing required libraries, headers or license files")
        file_keys = {name.casefold() for name in files}
        if any(str(parent).casefold() in file_keys for name in paths for parent in PurePosixPath(name).parents):
            raise FetchError("FFmpeg archive uses a file as a directory")
        inventory = []
        for item in members:
            path = destination / item.filename
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with package.open(item) as source, path.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if path.stat().st_size != item.file_size:
                raise FetchError("FFmpeg archive member has an unexpected size")
            path.chmod(0o644)
            inventory.append({"path": item.filename, "size": item.file_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    source = (destination / "share/ffmpeg/SOURCE.txt").read_text(encoding="utf-8")
    if f"Build recipe: {UPSTREAM}/tree/{REVISION}\n" not in source or f"Triplet: {architecture}-osx-release\n" not in source:
        raise FetchError("FFmpeg package source revision or architecture does not match the manifest")
    return inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description="Acquire the pinned macOS FFmpeg package with verified content and a fresh destination.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--architecture", choices=PACKAGES, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    destination = args.destination.absolute()
    name = f"ffmpeg-macos-{args.architecture}.zip"
    asset = {"name": name, "url": f"{UPSTREAM}/releases/download/{REVISION[:12]}/{name}", **PACKAGES[args.architecture]}
    asset["api_url"] = f"https://api.github.com/repos/KytyPS5/ext-ffmpeg-core/releases/assets/{asset['asset_id']}"
    report = {"schema_version": 1, "dependency": "FFmpeg", "revision": REVISION, "architecture": args.architecture,
              "asset": asset, "destination": str(destination), "passed": False, "stage": "prerequisites", "attempts": [],
              "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    save = lambda: write_report(args.report, report)
    try:
        if destination.exists() or destination.is_symlink():
            raise FetchError("FFmpeg destination already exists; use a fresh destination")
        report.update(repository_identity(args.repo.resolve()))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".ffmpeg-", dir=destination.parent) as temporary:
            stage = Path(temporary)
            report["stage"] = "download"
            download(asset, stage / "package.zip", report, save)
            report["stage"] = "archive"
            report["files"] = unpack(stage / "package.zip", stage / "package", args.architecture)
            report["stage"] = "publish"
            if destination.exists() or destination.is_symlink():
                raise FetchError("FFmpeg destination appeared during acquisition")
            (stage / "package").rename(destination)
        report.update(stage="complete", passed=True)
    except (FetchError, OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    finally:
        save()
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
