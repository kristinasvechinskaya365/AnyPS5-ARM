import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from tools.macos import resolve_packages as resolver


class SwiftPackageResolveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swift-resolve-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.package = self.root / "package with spaces"
        self.package.mkdir()
        self.inputs = {"Package.swift": b"pinned manifest\n", "Package.resolved": b'{"pins": []}\n'}
        for name, contents in self.inputs.items():
            (self.package / name).write_bytes(contents)
        self.diagnostics = self.root / "diagnostics"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        swift = self.bin / "swift"
        swift.write_text("#!" + sys.executable + "\n" + textwrap.dedent("""\
            import json
            import os
            from pathlib import Path
            import signal
            import sys
            import time

            root = Path(__file__).parent.parent
            calls = root / "calls.jsonl"
            index = len(calls.read_text().splitlines()) if calls.exists() else 0
            with calls.open("a") as output:
                output.write(json.dumps(sys.argv[1:]) + "\\n")
            response = json.loads((root / "responses.json").read_text())[index]
            (root / "pid").write_text(str(os.getpid()))
            package = Path(sys.argv[sys.argv.index("--package-path") + 1])
            if "replace" in response:
                (package / "Package.resolved").write_text(response["replace"])
            if response.get("delete"):
                (package / "Package.swift").unlink()
            print(response.get("output", ""), end="", flush=True)
            if response.get("signal"):
                os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(response.get("sleep", 0))
            sys.exit(response.get("exit", 0))
            """), encoding="utf-8")
        swift.chmod(0o755)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}).start()
        self.sleep = patch.object(resolver, "sleep").start()

    def download_error(self, status=504, target="WebRTC"):
        return (f"error: failed downloading 'https://example.invalid/{target}.zip' which is required by "
                f"binary target '{target}': badResponseStatusCode({status})\n")

    def run_resolve(self, responses, timeout=3):
        (self.root / "responses.json").write_text(json.dumps(responses), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = resolver.resolve_packages(self.package, self.diagnostics, timeout=timeout)
        data = json.loads((self.diagnostics / "report.json").read_text())
        calls = self.root / "calls.jsonl"
        invoked = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return code, data, invoked

    def test_success_only_resolves_exact_pins_and_records_input_hashes(self):
        code, data, calls = self.run_resolve([{"output": "Resolved binary artifacts\n"}])
        self.assertEqual(code, 0)
        self.assertTrue(data["passed"])
        self.assertEqual(calls, [["package", "--package-path", str(self.package), "--force-resolved-versions", "resolve"]])
        self.assertEqual(data["inputs"], {name: hashlib.sha256(value).hexdigest() for name, value in self.inputs.items()})
        self.assertTrue(data["attempts"][0]["inputs_unchanged"])
        self.sleep.assert_not_called()

    def test_transient_downloads_retry_and_preserve_every_attempt(self):
        first = self.download_error(502) + self.download_error(503, "RustLiveKitUniFFI") + "error: fatalError\n"
        second = self.download_error(504) + "error: fatalError\n"
        code, data, calls = self.run_resolve([{"exit": 1, "output": first}, {"exit": 1, "output": second},
                                            {"output": "Resolved\n"}])
        self.assertEqual(code, 0)
        self.assertTrue(data["passed"])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call == calls[0] for call in calls))
        self.assertEqual([entry["exit_code"] for entry in data["attempts"]], [1, 1, 0])
        self.assertEqual([entry["transient_download_failure"] for entry in data["attempts"]], [True, True, False])
        self.assertEqual([Path(entry["log"]).read_text() for entry in data["attempts"]], [first, second, "Resolved\n"])
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [10, 30])

    def test_exhaustion_stays_failed_and_does_not_attempt_a_fourth_time(self):
        code, data, calls = self.run_resolve([{"exit": 1, "output": self.download_error()}] * 3 + [{}])
        self.assertEqual(code, 1)
        self.assertFalse(data["passed"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(data["attempts"]), 3)
        self.assertIn("after three attempts", data["error"])
        self.assertEqual(self.sleep.call_count, 2)

    def test_checksum_error_beside_a_transient_download_fails_without_retry(self):
        text = self.download_error() + "error: checksum of downloaded artifact does not match checksum specified by the manifest\nerror: fatalError\n"
        code, data, calls = self.run_resolve([{"exit": 1, "output": text}, {}])
        self.assertEqual(code, 1)
        self.assertFalse(data["passed"])
        self.assertEqual(len(calls), 1)
        self.sleep.assert_not_called()

    def test_nontransient_or_unrecognized_errors_are_never_retried(self):
        messages = [self.download_error(401), self.download_error(404), self.download_error(500),
                    "error: Package.resolved is out of date\n", "error: fatalError\n", "",
                    "error: source compilation failed with 504\n", "fatal: unable to access remote\n",
                    self.download_error() + "error: invalid manifest\n",
                    self.download_error() + "fatal: repository not found\n"]
        for text in messages:
            with self.subTest(text=text):
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                code, data, calls = self.run_resolve([{"exit": 1, "output": text}, {}])
                self.assertEqual(code, 1)
                self.assertFalse(data["passed"])
                self.assertEqual(len(calls), 1)
                self.sleep.assert_not_called()

    def test_zero_exit_cannot_hide_reported_errors(self):
        code, data, calls = self.run_resolve([{"output": self.download_error()}, {}])
        self.assertEqual(code, 1)
        self.assertFalse(data["passed"])
        self.assertEqual(len(calls), 1)
        self.sleep.assert_not_called()

    def test_changed_lock_file_fails_even_when_resolution_succeeds(self):
        code, data, calls = self.run_resolve([{"replace": "different pins"}, {}])
        self.assertEqual(code, 1)
        self.assertFalse(data["passed"])
        self.assertFalse(data["attempts"][0]["inputs_unchanged"])
        self.assertIn("inputs changed", data["error"])
        self.assertEqual(len(calls), 1)
        self.sleep.assert_not_called()

    def test_removed_input_stops_retries_and_keeps_download_evidence(self):
        code, data, calls = self.run_resolve([{"delete": True, "exit": 1, "output": self.download_error()}, {}])
        self.assertEqual(code, 1)
        self.assertFalse(data["passed"])
        self.assertEqual(len(calls), 1)
        self.assertIn("Package.swift", data["error"])
        self.assertEqual(Path(data["attempts"][0]["log"]).read_text(), self.download_error())
        self.sleep.assert_not_called()

    def test_missing_lock_file_prevents_any_command(self):
        (self.package / "Package.resolved").unlink()
        code, data, calls = self.run_resolve([{}])
        self.assertEqual(code, 1)
        self.assertFalse(data["passed"])
        self.assertEqual(calls, [])
        self.assertEqual(data["attempts"], [])
        self.assertIn("Package.resolved", data["error"])

    def test_unavailable_swift_fails_and_records_the_attempt(self):
        with patch.object(resolver.subprocess, "Popen", side_effect=FileNotFoundError("swift missing")):
            code, data, calls = self.run_resolve([{}])
        self.assertEqual(code, 1)
        self.assertFalse(data["passed"])
        self.assertEqual(calls, [])
        self.assertEqual(len(data["attempts"]), 1)
        self.assertIn("swift missing", data["error"])
        self.sleep.assert_not_called()

    def test_timeout_terminates_the_process_and_never_retries(self):
        code, data, calls = self.run_resolve([{"output": self.download_error(), "sleep": 10}, {}], timeout=0.25)
        self.assertEqual(code, 124)
        self.assertFalse(data["passed"])
        self.assertTrue(data["attempts"][0]["timed_out"])
        self.assertEqual(data["attempts"][0]["exit_code"], -signal.SIGKILL)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "pid").read_text()), 0)
        self.sleep.assert_not_called()

    def test_signal_failure_after_a_download_error_is_not_retried(self):
        code, data, calls = self.run_resolve([{"output": self.download_error(), "signal": True}, {}])
        self.assertEqual(code, 1)
        self.assertFalse(data["passed"])
        self.assertEqual(data["attempts"][0]["exit_code"], -signal.SIGTERM)
        self.assertEqual(len(calls), 1)
        self.sleep.assert_not_called()

    def test_command_line_propagates_failure(self):
        (self.root / "responses.json").write_text(json.dumps([{"exit": 1, "output": self.download_error(404)}]))
        result = subprocess.run([sys.executable, str(Path(resolver.__file__)), "--package-path", str(self.package),
                                 "--diagnostics", str(self.diagnostics)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("badResponseStatusCode(404)", result.stdout)
        self.assertFalse(json.loads((self.diagnostics / "report.json").read_text())["passed"])


if __name__ == "__main__":
    unittest.main()
