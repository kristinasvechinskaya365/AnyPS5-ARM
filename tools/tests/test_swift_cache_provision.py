import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from tools.macos import provision_swift_cache as provisioner


class SwiftCacheProvisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swift-cache-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.package = self.root / "package with spaces"
        self.package.mkdir()
        (self.package / "Package.swift").write_text("pinned root manifest\n")
        self.original_assets = copy.deepcopy(provisioner.ASSETS)
        self.assets = copy.deepcopy(self.original_assets)
        self.payloads = [f"archive bytes for {asset['target']}".encode() for asset in self.assets]
        self.pins = [{"identity": a["identity"], "kind": "remoteSourceControl", "location": a["location"],
                      "state": {"revision": a["revision"], "version": a["version"]}} for a in self.assets]
        self.write_pins()
        for asset, payload in zip(self.assets, self.payloads):
            asset.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        self.cache = self.root / "private cache"
        self.diagnostics = self.root / "diagnostics"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text("#!" + sys.executable + "\n" + textwrap.dedent("""\
            import json
            from pathlib import Path
            import sys
            import time

            root = Path(__file__).parent.parent
            calls = root / "calls.jsonl"
            index = len(calls.read_text().splitlines()) if calls.exists() else 0
            with calls.open("a") as stream:
                stream.write(json.dumps(sys.argv[1:]) + "\\n")
            response = json.loads((root / "responses.json").read_text())[index]
            destination = Path(sys.argv[sys.argv.index("--output") + 1])
            destination.write_bytes(bytes.fromhex(response.get("payload", "")))
            if "--dump-header" in sys.argv:
                headers = Path(sys.argv[sys.argv.index("--dump-header") + 1])
                headers.write_text(response.get("headers", ""))
            if response.get("change_pins"):
                (root / "package with spaces/Package.resolved").write_text("changed pins")
            print(response.get("status", "200"))
            print(response.get("type", "application/octet-stream"))
            print(len(bytes.fromhex(response.get("payload", ""))))
            print(response.get("stderr", ""), file=sys.stderr)
            time.sleep(response.get("sleep", 0))
            sys.exit(response.get("exit", 0))
            """), encoding="utf-8")
        curl.chmod(0o755)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}).start()
        patch.object(provisioner, "ASSETS", self.assets).start()
        self.sleep = patch.object(provisioner, "sleep").start()

    def write_pins(self):
        (self.package / "Package.resolved").write_text(json.dumps({"version": 2, "pins": self.pins}))

    def run_provision(self, responses=None, timeout=3):
        if responses is None:
            responses = [{"payload": payload.hex()} for payload in self.payloads]
        (self.root / "responses.json").write_text(json.dumps(responses))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = provisioner.provision(self.package, self.cache, self.diagnostics, timeout=timeout)
        report = json.loads((self.diagnostics / "report.json").read_text())
        calls_file = self.root / "calls.jsonl"
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
        return code, report, calls, output.getvalue()

    def test_original_urls_have_the_exact_ascii_swiftpm_keys(self):
        expected = [
            "https___github_com_stasel_WebRTC_releases_download_155_0_0_WebRTC_M155_xcframework_zip",
            "https___github_com_livekit_webrtc_xcframework_releases_download_150_7871_03_LiveKitWebRTC_xcframework_zip",
            "https___github_com_livekit_livekit_uniffi_xcframework_releases_download_0_1_9_RustLiveKitUniFFI_xcframework_zip",
        ]
        self.assertEqual([provisioner.cache_key(asset["url"]) for asset in self.assets], expected)
        self.assertIn("livekit-ffi-xcframework", self.assets[2]["api_url"])
        self.assertIn("livekit_uniffi_xcframework", expected[2])

    def test_all_archives_are_verified_atomically_published_and_rehashed_on_reuse(self):
        code, report, calls, _ = self.run_provision()
        self.assertEqual(code, 0, report)
        self.assertTrue(report["passed"])
        self.assertEqual(len(calls), 3)
        for asset, entry, payload, call in zip(self.assets, report["assets"], self.payloads, calls):
            self.assertTrue(entry["verified"])
            self.assertFalse(entry["reused"])
            self.assertEqual(Path(entry["path"]).read_bytes(), payload)
            self.assertEqual(call[-1], asset["url"])
            self.assertNotIn("--header", call)
            self.assertEqual(call[call.index("--proto") + 1], "=https")
            self.assertEqual(call[call.index("--proto-redir") + 1], "=https")
            self.assertEqual(call[call.index("--max-filesize") + 1], str(asset["size"]))
            self.assertNotEqual(call[call.index("--output") + 1], entry["path"])
            self.assertFalse(Path(call[call.index("--output") + 1]).exists())
        code, reused, calls, _ = self.run_provision([])
        self.assertEqual(code, 0, reused)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(item["reused"] and item["verified"] and not item["attempts"] for item in reused["assets"]))
        self.assertEqual(len(list((self.cache / "artifacts").iterdir())), 3)

    def test_pin_drift_is_rejected_before_any_download(self):
        for change in ("revision", "version", "location", "kind", "missing", "duplicate"):
            with self.subTest(change=change):
                pins = copy.deepcopy(self.pins)
                if change in ("revision", "version"):
                    pins[0]["state"][change] = "different"
                elif change in ("location", "kind"):
                    pins[0][change] = "different"
                elif change == "missing":
                    pins.pop()
                else:
                    pins.append(pins[0])
                (self.package / "Package.resolved").write_text(json.dumps({"version": 2, "pins": pins}))
                code, report, calls, _ = self.run_provision()
                self.assertEqual(code, 1)
                self.assertFalse(report["passed"])
                self.assertEqual(calls, [])
                self.assertFalse(self.cache.exists())

    def test_digest_failure_never_publishes_a_cache_entry(self):
        wrong = b"x" * self.assets[0]["size"]
        code, report, calls, _ = self.run_provision([{"payload": wrong.hex()}])
        self.assertEqual(code, 1)
        self.assertIn("SHA-256", report["error"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(list((self.cache / "artifacts").iterdir()), [])
        self.sleep.assert_not_called()

    def test_short_download_or_metadata_response_is_fatal(self):
        for response in ({"payload": b"short".hex()}, {"payload": b"{}".hex(), "type": "application/json"}):
            with self.subTest(response=response):
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                code, report, calls, _ = self.run_provision([response])
                self.assertEqual(code, 1)
                self.assertFalse(report["passed"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(list((self.cache / "artifacts").iterdir()), [])
                self.sleep.assert_not_called()

    def test_transient_release_failure_uses_only_the_pinned_asset_api(self):
        responses = [{"exit": 22, "status": "504"}] + [{"payload": payload.hex()} for payload in self.payloads]
        code, report, calls, _ = self.run_provision(responses)
        self.assertEqual(code, 0, report)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][-1], self.assets[0]["url"])
        self.assertEqual(calls[1][-1], self.assets[0]["api_url"])
        self.assertEqual(calls[1][calls[1].index("--header") + 1], "Accept: application/octet-stream")
        self.assertEqual([a["route"] for a in report["assets"][0]["attempts"]], ["release", "asset-api"])
        self.assertEqual([a["http_status"] for a in report["assets"][0]["attempts"]], [504, 200])
        self.sleep.assert_called_once_with(2)

    def test_retry_exhaustion_keeps_cache_empty_and_failure_visible(self):
        code, report, calls, _ = self.run_provision([{"exit": 22, "status": "503"}] * 3)
        self.assertEqual(code, 1)
        self.assertFalse(report["passed"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(list((self.cache / "artifacts").iterdir()), [])
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [2, 4])

    def test_nontransient_failure_never_exposes_signed_redirect_details(self):
        secret = "https://release-assets.githubusercontent.com/a?signature=private-value"
        code, report, calls, output = self.run_provision([{"exit": 22, "status": "403", "stderr": secret}])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private-value", output)
        self.assertNotIn("private-value", json.dumps(report))
        self.sleep.assert_not_called()

    def test_http_rejection_wins_over_a_transport_error(self):
        for status in ("401", "403", "404", "500"):
            with self.subTest(status=status):
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                code, report, calls, _ = self.run_provision([{"exit": 56, "status": status, "type": "application/json"}] * 3)
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)
                self.assertFalse(report["assets"][0]["attempts"][0]["transient"])
                self.assertEqual(list((self.cache / "artifacts").iterdir()), [])
                self.sleep.assert_not_called()

    def test_transient_status_or_incomplete_transport_can_retry(self):
        for status in ("000", "200", "504"):
            with self.subTest(status=status):
                self.cache = self.root / ("cache-" + status)
                self.sleep.reset_mock()
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                responses = [{"exit": 56, "status": status}] + [{"payload": payload.hex()} for payload in self.payloads]
                code, report, calls, _ = self.run_provision(responses)
                self.assertEqual(code, 0, report)
                self.assertEqual(len(calls), 4)
                self.assertTrue(report["assets"][0]["attempts"][0]["transient"])
                self.sleep.assert_called_once_with(2)

    def test_api_rejection_stops_after_a_transient_release_failure(self):
        code, report, calls, _ = self.run_provision([{"exit": 22, "status": "504"},
                                                    {"exit": 56, "status": "403", "type": "application/json"}, {}])
        self.assertEqual(code, 1)
        self.assertFalse(report["passed"])
        self.assertEqual([call[-1] for call in calls], [self.assets[0]["url"], self.assets[0]["api_url"]])
        self.assertFalse(report["assets"][0]["attempts"][1]["transient"])
        self.sleep.assert_called_once_with(2)

    def test_rate_limit_diagnostics_keep_only_safe_response_fields(self):
        headers = ("HTTP/2 403\r\nContent-Type: application/json; charset=utf-8\r\n"
                   "X-RateLimit-Limit: 60\r\nX-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 1804749000\r\n"
                   "X-RateLimit-Resource: core\r\nRetry-After: 30\r\nX-GitHub-Request-Id: ABCD:1234\r\n"
                   "Server: github.com\r\nDate: Mon, 09 Nov 2026 06:00:00 GMT\r\n"
                   "Location: https://release-assets.githubusercontent.com/a?signature=private-value\r\n"
                   "Set-Cookie: private-cookie\r\nAuthorization: Bearer private-token\r\n")
        body = json.dumps({"message": "API rate limit exceeded. See https://example.invalid/help?token=private-value"}).encode()
        code, report, calls, output = self.run_provision([{"exit": 56, "status": "403", "type": "application/json",
                                                         "payload": body.hex(), "headers": headers}])
        self.assertEqual(code, 1)
        attempt = report["assets"][0]["attempts"][0]
        self.assertEqual(attempt["message"], "API rate limit exceeded. See [URL omitted]")
        self.assertEqual(attempt["responses"], [{"status": 403, "content-type": "application/json",
                                                "x-ratelimit-limit": 60, "x-ratelimit-remaining": 0,
                                                "x-ratelimit-reset": 1804749000, "x-ratelimit-resource": "core",
                                                "retry-after": 30, "x-github-request-id": "ABCD:1234",
                                                "server": "github.com", "date": "2026-11-09T06:00:00+00:00"}])
        self.assertNotIn("private-", output)
        self.assertEqual(len(calls), 1)
        self.assertFalse(Path(calls[0][calls[0].index("--dump-header") + 1]).exists())
        self.assertEqual(list((self.cache / "artifacts").iterdir()), [])

    def test_malformed_error_body_and_sensitive_header_values_are_omitted(self):
        headers = ("HTTP/2 403\r\nX-RateLimit-Remaining: https://example.invalid/?secret=value\r\n"
                   "Server: https://example.invalid/?secret=value\r\nDate: invalid-date\r\n")
        code, report, _, output = self.run_provision([{"exit": 56, "status": "403", "type": "application/json",
                                                     "payload": b"not JSON".hex(), "headers": headers}])
        self.assertEqual(code, 1)
        self.assertEqual(report["assets"][0]["attempts"][0]["responses"], [{"status": 403}])
        self.assertNotIn("message", report["assets"][0]["attempts"][0])
        self.assertNotIn("secret", output)

    def test_json_error_message_omits_authentication_and_cookie_values(self):
        for message in ("Authorization: Bearer private-value", "Authorization Bearer private-value",
                        "Set-Cookie: private-value", "Location https://example.invalid/?sig=private-value"):
            with self.subTest(message=message):
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                body = json.dumps({"message": message}).encode()
                code, report, _, output = self.run_provision([{"exit": 56, "status": "403",
                                                              "type": "application/json", "payload": body.hex()}])
                self.assertEqual(code, 1)
                self.assertEqual(report["assets"][0]["attempts"][0]["message"], "[sensitive field omitted]")
                self.assertNotIn("private-value", output)

    def test_unsafe_cache_directories_and_linked_entries_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        kept = outside / "kept"
        kept.write_bytes(self.payloads[0])
        self.cache.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_provision()[0], 1)
        self.cache.unlink()
        self.cache.mkdir()
        (self.cache / "artifacts").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.run_provision()[0], 1)
        (self.cache / "artifacts").unlink()
        (self.cache / "artifacts").mkdir()
        entry = self.cache / "artifacts" / provisioner.cache_key(self.assets[0]["url"])
        entry.symlink_to(kept)
        code, report, calls, _ = self.run_provision()
        self.assertEqual(code, 1)
        self.assertFalse(report["passed"])
        self.assertEqual(calls, [])
        self.assertEqual(kept.read_bytes(), self.payloads[0])
        self.assertTrue(entry.is_symlink())

    def test_existing_cache_bytes_are_never_trusted_or_overwritten(self):
        (self.cache / "artifacts").mkdir(parents=True)
        entry = self.cache / "artifacts" / provisioner.cache_key(self.assets[0]["url"])
        wrong = b"x" * self.assets[0]["size"]
        entry.write_bytes(wrong)
        code, report, calls, _ = self.run_provision()
        self.assertEqual(code, 1)
        self.assertIn("SHA-256", report["error"])
        self.assertEqual(calls, [])
        self.assertEqual(entry.read_bytes(), wrong)

    def test_colliding_or_nonascii_url_keys_are_rejected_before_cache_creation(self):
        original = self.assets[1]["url"]
        self.assets[1]["url"] = self.assets[0]["url"].replace("WebRTC-M155", "WebRTC_M155")
        code, report, calls, _ = self.run_provision()
        self.assertEqual(code, 1)
        self.assertIn("collide", report["error"])
        self.assertEqual(calls, [])
        self.assertFalse(self.cache.exists())
        self.assets[1]["url"] = original + "é"
        code, report, calls, _ = self.run_provision()
        self.assertEqual(code, 1)
        self.assertIn("ASCII", report["error"])
        self.assertEqual(calls, [])

    def test_input_change_during_acquisition_prevents_publication(self):
        code, report, calls, _ = self.run_provision([{"payload": self.payloads[0].hex(), "change_pins": True}])
        self.assertEqual(code, 1)
        self.assertIn("inputs changed", report["error"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(list((self.cache / "artifacts").iterdir()), [])

    def test_process_timeout_is_fatal_and_removes_partial_download(self):
        code, report, calls, _ = self.run_provision([{"payload": self.payloads[0].hex(), "sleep": 10}], timeout=0.25)
        self.assertEqual(code, 1)
        self.assertTrue(report["assets"][0]["attempts"][0]["timed_out"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(list((self.cache / "artifacts").iterdir()), [])
        self.sleep.assert_not_called()

    def test_cli_rejects_pin_drift_without_network_or_cache_writes(self):
        self.pins[0]["state"]["revision"] = "0" * 40
        self.write_pins()
        result = subprocess.run([sys.executable, str(Path(provisioner.__file__)), "--package-path", str(self.package),
                                 "--cache-path", str(self.cache), "--diagnostics", str(self.diagnostics)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["passed"])
        self.assertFalse((self.root / "calls.jsonl").exists())
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
