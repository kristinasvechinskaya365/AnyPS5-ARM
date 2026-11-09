import contextlib
import hashlib
import http.client
import io
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "macos"))
import fetch_ffmpeg as FETCH


class Response(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data)), "Content-Type": "application/octet-stream"}

    def geturl(self):
        return "https://release-assets.githubusercontent.com/package.zip?sig=secret-token"


class InterruptedResponse(Response):
    def read1(self, size=-1):
        if self.tell():
            raise http.client.IncompleteRead(b"", 1)
        return super().read1(7)


def package(architecture="arm64", extra=(), missing=()):
    files = {name: b"fixture content" for name in FETCH.REQUIRED if name not in missing}
    if "share/ffmpeg/SOURCE.txt" in files:
        files["share/ffmpeg/SOURCE.txt"] = (f"Build recipe: {FETCH.UPSTREAM}/tree/{FETCH.REVISION}\n"
                                           f"Triplet: {architecture}-osx-release\n").encode()
    buffer = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        warnings.simplefilter("ignore", UserWarning)
        for name, data, mode in [*((name, data, 0o100644) for name, data in sorted(files.items())), *extra]:
            entry = zipfile.ZipInfo(name)
            entry.external_attr = mode << 16
            archive.writestr(entry, data)
    return buffer.getvalue()


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://github.com/package.zip", code, "fixture HTTP error", headers or {}, io.BytesIO())


class FfmpegFetchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.number = 0

    def invoke(self, data=None, effects=None, sha256=None, size=None, architecture="arm64", destination=None, identity_error=None):
        self.number += 1
        data = package(architecture) if data is None else data
        self.destination = destination or self.root / f"package-{self.number}"
        report = self.root / f"report-{self.number}.json"
        manifest = {**FETCH.PACKAGES[architecture], "sha256": sha256 or hashlib.sha256(data).hexdigest(), "size": len(data) if size is None else size}
        identity = {"source_revision": "c" * 40, "gitlink_revision": FETCH.REVISION, "submodule_revision": FETCH.REVISION}
        opener = mock.Mock()
        opener.open.side_effect = effects if effects is not None else [Response(data)]
        with mock.patch.dict(FETCH.PACKAGES, {architecture: manifest}), \
                mock.patch.object(FETCH, "repository_identity", return_value=identity, side_effect=identity_error), \
                mock.patch.object(FETCH.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(FETCH.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            code = FETCH.main(["--architecture", architecture, "--destination", str(self.destination), "--report", str(report)])
        self.assertEqual(list(self.root.glob(".ffmpeg-*")), [])
        return code, json.loads(report.read_text()), opener, sleep

    def test_verified_package_is_published_with_file_digests_and_redacted_redirect(self):
        for architecture in ("arm64", "x64"):
            with self.subTest(architecture=architecture):
                code, report, opener, sleep = self.invoke(architecture=architecture)
                self.assertEqual(code, 0)
                self.assertTrue(report["passed"])
                self.assertEqual(report["stage"], "complete")
                self.assertEqual(report["architecture"], architecture)
                self.assertEqual(report["source_revision"], "c" * 40)
                self.assertEqual(opener.open.call_count, 1)
                self.assertEqual(opener.open.call_args.args[0].full_url, report["asset"]["url"])
                self.assertIsNone(opener.open.call_args.args[0].get_header("Accept"))
                self.assertEqual(report["attempts"][0]["route"], "release")
                sleep.assert_not_called()
                self.assertNotIn("secret-token", json.dumps(report))
                self.assertEqual(report["attempts"][0]["http_status"], 200)
                self.assertEqual(report["attempts"][0]["sha256"], report["asset"]["sha256"])
                self.assertEqual({entry["path"] for entry in report["files"]}, FETCH.REQUIRED)
                for entry in report["files"]:
                    actual = (self.destination / entry["path"]).read_bytes()
                    self.assertEqual(len(actual), entry["size"])
                    self.assertEqual(hashlib.sha256(actual).hexdigest(), entry["sha256"])

    def test_existing_destination_and_symlink_are_refused_without_network(self):
        existing = self.root / "existing"
        existing.mkdir()
        sentinel = existing / "keep"
        sentinel.write_text("original")
        alias = self.root / "alias"
        alias.symlink_to(existing, target_is_directory=True)
        for destination in (existing, alias):
            code, report, opener, _ = self.invoke(destination=destination)
            self.assertEqual(code, 1)
            self.assertFalse(report["passed"])
            opener.open.assert_not_called()
            self.assertEqual(sentinel.read_text(), "original")

    def test_repository_failure_prevents_download_and_publication(self):
        code, report, opener, _ = self.invoke(identity_error=FETCH.FetchError("manifest revision changed"))
        self.assertEqual(code, 1)
        self.assertEqual(report["stage"], "prerequisites")
        opener.open.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_transient_http_failure_is_recorded_and_retried(self):
        data = package()
        code, report, opener, sleep = self.invoke(data, [http_error(503, {"Retry-After": "6"}), Response(data)])
        self.assertEqual(code, 0)
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual([entry["http_status"] for entry in report["attempts"]], [503, 200])
        self.assertTrue(report["attempts"][0]["transient"])
        sleep.assert_called_once_with(6)

    def test_transient_failures_switch_to_the_pinned_asset_api(self):
        for architecture, asset_id in (("arm64", 567718242), ("x64", 567718257)):
            with self.subTest(architecture=architecture):
                data = package(architecture)
                code, report, opener, sleep = self.invoke(data, [http_error(504), http_error(502), Response(data)], architecture=architecture)
                self.assertEqual(code, 0)
                api = f"https://api.github.com/repos/KytyPS5/ext-ffmpeg-core/releases/assets/{asset_id}"
                self.assertEqual(report["asset"]["asset_id"], asset_id)
                self.assertEqual(report["asset"]["api_url"], api)
                self.assertEqual([entry["route"] for entry in report["attempts"]], ["release", "asset-api", "asset-api"])
                self.assertEqual([entry["url"] for entry in report["attempts"]], [report["asset"]["url"], api, api])
                requests = [call.args[0] for call in opener.open.call_args_list]
                self.assertEqual([request.full_url for request in requests], [report["asset"]["url"], api, api])
                self.assertEqual([request.get_header("Accept") for request in requests], [None, "application/octet-stream", "application/octet-stream"])
                self.assertTrue(all(request.get_header("Authorization") is None for request in requests))
                self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(4)])

    def test_asset_api_metadata_is_fatal_without_another_attempt(self):
        for media_type in ("application/json", "application/vnd.github+json; charset=utf-8"):
            with self.subTest(media_type=media_type):
                response = Response(b'{"name": "ffmpeg-macos-arm64.zip"}')
                response.headers["Content-Type"] = media_type
                code, report, opener, sleep = self.invoke(effects=[http_error(504), response])
                self.assertEqual(code, 1)
                self.assertEqual(opener.open.call_count, 2)
                self.assertEqual(report["attempts"][-1]["route"], "asset-api")
                self.assertEqual(report["attempts"][-1]["bytes"], 0)
                self.assertFalse(report["attempts"][-1]["transient"])
                sleep.assert_called_once_with(2)
                self.assertFalse(self.destination.exists())

    def test_asset_api_integrity_errors_are_fatal_without_another_attempt(self):
        data = package()
        for arguments in ({"sha256": "0" * 64}, {"size": len(data) - 1}, {"size": len(data) + 1}):
            with self.subTest(arguments=arguments), mock.patch.object(FETCH, "unpack") as unpack:
                code, report, opener, sleep = self.invoke(data, [http_error(504), Response(data)], **arguments)
                self.assertEqual(code, 1)
                self.assertEqual(opener.open.call_count, 2)
                self.assertEqual(report["attempts"][-1]["route"], "asset-api")
                unpack.assert_not_called()
                sleep.assert_called_once_with(2)
                self.assertFalse(self.destination.exists())

    def test_permanent_http_errors_are_not_retried(self):
        for status in (401, 403, 404, 501):
            with self.subTest(status=status):
                code, report, opener, sleep = self.invoke(effects=[http_error(status)])
                self.assertEqual(code, 1)
                self.assertEqual(report["attempts"][0]["http_status"], status)
                self.assertFalse(report["attempts"][0]["transient"])
                self.assertEqual(opener.open.call_count, 1)
                sleep.assert_not_called()
                self.assertFalse(self.destination.exists())

    def test_retries_have_a_fixed_limit_and_preserve_all_attempts(self):
        code, report, opener, sleep = self.invoke(effects=[http_error(504) for _ in range(3)])
        self.assertEqual(code, 1)
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(len(report["attempts"]), 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(4)])
        self.assertFalse(self.destination.exists())

    def test_interrupted_and_timed_out_transfers_retry_without_partial_reuse(self):
        data = package()
        for failure in (InterruptedResponse(data), urllib.error.URLError(TimeoutError("fixture timeout"))):
            with self.subTest(failure=type(failure).__name__):
                code, report, opener, _ = self.invoke(data, [failure, Response(data)])
                self.assertEqual(code, 0)
                self.assertEqual(opener.open.call_count, 2)
                self.assertTrue(report["attempts"][0]["transient"])
                self.assertEqual(report["attempts"][-1]["bytes"], len(data))

    def test_tls_verification_errors_are_not_retried(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError("fixture certificate failure"))
        code, report, opener, sleep = self.invoke(effects=[error])
        self.assertEqual(code, 1)
        self.assertFalse(report["attempts"][0]["transient"])
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_checksum_mismatch_is_fatal_without_extraction_or_retry(self):
        with mock.patch.object(FETCH, "unpack") as unpack:
            code, report, opener, sleep = self.invoke(sha256="0" * 64)
        self.assertEqual(code, 1)
        self.assertIn("SHA-256", report["error"])
        unpack.assert_not_called()
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_short_or_oversized_successful_responses_are_not_retried(self):
        data = package()
        for size in (len(data) - 1, len(data) + 1):
            code, report, opener, sleep = self.invoke(data, size=size)
            self.assertEqual(code, 1)
            self.assertFalse(report["passed"])
            self.assertEqual(opener.open.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse(self.destination.exists())

    def test_malformed_archive_with_matching_digest_is_not_retried(self):
        code, report, opener, sleep = self.invoke(b"not a ZIP archive")
        self.assertEqual(code, 1)
        self.assertEqual(report["stage"], "archive")
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_escaping_or_unsupported_paths_are_refused_before_publication(self):
        for name in ("../outside", "/include/absolute", "include/../escape", "lib//file", "lib/./file", "include\\file", "C:/file", "other/file"):
            with self.subTest(name=name):
                code, report, opener, sleep = self.invoke(package(extra=[(name, b"invalid", 0o100644)]))
                self.assertEqual(code, 1)
                self.assertEqual(report["stage"], "archive")
                self.assertFalse(self.destination.exists())
                self.assertFalse((self.root / "outside").exists())
                self.assertEqual(opener.open.call_count, 1)
                sleep.assert_not_called()

    def test_links_and_special_members_are_refused(self):
        for mode in (0o120777, 0o010644, 0o060644):
            code, report, _, _ = self.invoke(package(extra=[("include/alias", b"avcodec.h", mode)]))
            self.assertEqual(code, 1)
            self.assertEqual(report["stage"], "archive")
            self.assertFalse(self.destination.exists())

    def test_duplicates_case_collisions_and_file_parents_are_refused(self):
        for name in ("include/libavcodec/avcodec.h", "include/libavcodec/AVCODEC.H", "include/libavcodec"):
            code, report, _, _ = self.invoke(package(extra=[(name, b"invalid", 0o100644)]))
            self.assertEqual(code, 1)
            self.assertEqual(report["stage"], "archive")
            self.assertFalse(self.destination.exists())

    def test_extraction_limits_are_enforced(self):
        for limit in ("MAX_MEMBERS", "MAX_EXPANDED", "MAX_MEMBER"):
            with mock.patch.object(FETCH, limit, 1):
                code, report, _, _ = self.invoke()
            self.assertEqual(code, 1)
            self.assertEqual(report["stage"], "archive")
            self.assertFalse(self.destination.exists())

    def test_missing_required_content_or_wrong_architecture_prevents_publication(self):
        for data in (package(missing=["lib/libavcodec.a"]), package(missing=["share/ffmpeg/copyright"]), package("x64")):
            code, report, _, _ = self.invoke(data)
            self.assertEqual(code, 1)
            self.assertEqual(report["stage"], "archive")
            self.assertFalse(self.destination.exists())

    def test_https_redirects_cannot_downgrade_transport(self):
        handler = FETCH.HttpsRedirect()
        request = urllib.request.Request("https://github.com/package.zip")
        with self.assertRaisesRegex(FETCH.FetchError, "HTTPS"):
            handler.redirect_request(request, None, 302, "redirect", {}, "http://example.com/package.zip")
        try:
            handler.http_error_302(request, io.BytesIO(), 302, "Found", {"location": "file:///tmp/asset?sig=secret-token"})
        except urllib.error.HTTPError as error:
            code, report, opener, sleep = self.invoke(effects=[error])
        else:
            self.fail("non-HTTPS redirect was accepted")
        self.assertEqual(code, 1)
        self.assertNotIn("secret-token", json.dumps(report))
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_gitlink_and_submodule_identity_are_both_required(self):
        entry = f"160000 commit {FETCH.REVISION}\t3rdparty/ffmpeg-core\n"
        with mock.patch.object(FETCH.subprocess, "check_output", side_effect=[entry, FETCH.REVISION, "", "c" * 40]):
            self.assertEqual(FETCH.repository_identity(self.root), {"source_revision": "c" * 40, "gitlink_revision": FETCH.REVISION, "submodule_revision": FETCH.REVISION})
        for outputs in ([entry.replace(FETCH.REVISION, "d" * 40)], [entry, "d" * 40], [entry, FETCH.REVISION, " M CMakeLists.txt"]):
            with mock.patch.object(FETCH.subprocess, "check_output", side_effect=outputs):
                with self.assertRaises(FETCH.FetchError):
                    FETCH.repository_identity(self.root)


if __name__ == "__main__":
    unittest.main()
