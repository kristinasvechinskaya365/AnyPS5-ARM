import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import required_test_report


class RequiredTestReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inventory = self.root / "inventory.json"
        self.junit = self.root / "results.xml"
        self.exclusions = self.root / "known-failures.txt"
        self.log = self.root / "swift.log"

    def ctest(self, registered, cases, required=None, exclusions=None):
        self.inventory.write_text(json.dumps({"tests": [{"name": name} for name in registered]}), encoding="utf-8")
        self.junit.write_text(f"<testsuite>{cases}</testsuite>", encoding="utf-8")
        if exclusions is not None:
            self.exclusions.write_text(exclusions, encoding="utf-8")
        return required_test_report.report(self.inventory, self.junit, "ctest", required or registered,
                                           self.exclusions if exclusions is not None else None)

    def test_exact_required_passes_keep_unselected_coverage_visible(self):
        data = self.ctest(["lifecycle", "conversion", "graphics"],
                          '<testcase name="lifecycle" status="run" time="0.2"/><testcase name="conversion" status="run"/>',
                          ["lifecycle", "conversion"])
        self.assertEqual(data["errors"], [])
        self.assertEqual((data["registered"], data["required"], data["passed"]), (3, 2, 2))
        self.assertEqual(data["not_selected"], ["graphics"])

    def test_missing_registration_fails_even_if_other_tests_pass(self):
        data = self.ctest(["conversion"], '<testcase name="conversion"/>', ["lifecycle", "conversion"])
        self.assertTrue(any("no registered tests: lifecycle" in error for error in data["errors"]))

    def test_registered_test_without_result_fails(self):
        data = self.ctest(["lifecycle", "conversion"], '<testcase name="conversion"/>')
        self.assertEqual(data["missing"], ["lifecycle"])
        self.assertEqual(data["tests"]["lifecycle"]["status"], "missing")
        self.assertTrue(data["errors"])

    def test_skipped_required_test_fails(self):
        data = self.ctest(["lifecycle"], '<testcase name="lifecycle" status="notrun"><skipped message="SKIP_RETURN_CODE=77"/></testcase>')
        self.assertEqual(data["skipped"], 1)
        self.assertEqual(data["passed"], 0)
        self.assertIn("required test lifecycle: skipped", data["errors"])

    def test_disabled_required_test_fails(self):
        data = self.ctest(["lifecycle"], '<testcase name="lifecycle" status="disabled"/>')
        self.assertEqual(data["not_run"], 1)
        self.assertIn("required test lifecycle: not_run", data["errors"])

    def test_failures_and_errors_are_not_passes(self):
        for element in ("failure", "error"):
            with self.subTest(element=element):
                data = self.ctest(["lifecycle"], f'<testcase name="lifecycle"><{element}/></testcase>')
                self.assertEqual(data["failed"], 1)
                self.assertEqual(data["passed"], 0)
                self.assertTrue(data["errors"])

    def test_suite_level_failures_cannot_hide_behind_passing_cases(self):
        for tag in ("error", "failure", "skipped"):
            for nested in (False, True):
                with self.subTest(tag=tag, nested=nested):
                    content = f'<testcase name="lifecycle"/><{tag}>suite could not finish</{tag}>'
                    if nested:
                        content = f"<testsuite>{content}</testsuite>"
                    data = self.ctest(["lifecycle"], content)
                    self.assertEqual(data["passed"], 0)
                    self.assertTrue(any("suite-level" in error for error in data["errors"]))

    def test_declared_counts_must_match_testcase_evidence(self):
        self.inventory.write_text(json.dumps({"tests": [{"name": "lifecycle"}]}), encoding="utf-8")
        for attribute, values in (("tests", ["0", "2", "-1", "one", "1.0"]),
                                  ("failures", ["1", "-1", "one", "0.0"]),
                                  ("errors", ["1", "-1", "one", "0.0"]),
                                  ("skipped", ["1", "-1", "one", "0.0"]),
                                  ("disabled", ["1", "-1", "one", "0.0"])):
            for value in values:
                with self.subTest(attribute=attribute, value=value):
                    self.junit.write_text(f'<testsuite {attribute}="{value}"><testcase name="lifecycle"/></testsuite>', encoding="utf-8")
                    data = required_test_report.report(self.inventory, self.junit, "ctest", ["lifecycle"])
                    self.assertEqual(data["passed"], 0)
                    self.assertTrue(any("count" in error for error in data["errors"]))

    def test_nested_suite_counts_accept_complete_evidence(self):
        self.inventory.write_text(json.dumps({"tests": [{"name": "first"}, {"name": "second"}]}), encoding="utf-8")
        self.junit.write_text('<testsuites tests="2" failures="0" errors="0" skipped="0">'
                              '<testsuite tests="1" failures="0"><testcase name="first"/></testsuite>'
                              '<testsuite tests="1" errors="0"><testcase name="second"/></testsuite>'
                              '</testsuites>', encoding="utf-8")
        data = required_test_report.report(self.inventory, self.junit, "ctest", ["first", "second"])
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["passed"], 2)

    def test_ctest_distinguishes_disabled_from_skipped_counts(self):
        names = ["passed", "failed", "skipped", "disabled"]
        self.inventory.write_text(json.dumps({"tests": [{"name": name} for name in names]}), encoding="utf-8")
        self.junit.write_text('<testsuite tests="4" failures="1" disabled="1" skipped="1">'
                              '<testcase name="passed" status="run"/>'
                              '<testcase name="failed" status="fail"><failure/></testcase>'
                              '<testcase name="skipped" status="notrun"><skipped/></testcase>'
                              '<testcase name="disabled" status="disabled"/>'
                              '</testsuite>', encoding="utf-8")
        data = required_test_report.report(self.inventory, self.junit, "ctest", names)
        self.assertEqual((data["passed"], data["failed"], data["skipped"], data["not_run"]), (1, 1, 1, 1))
        self.assertFalse(any(error.startswith("results:") for error in data["errors"]))

    def test_conflicting_testcase_outcomes_are_rejected(self):
        for first, second in (("failure", "error"), ("failure", "skipped"), ("error", "skipped")):
            with self.subTest(first=first, second=second):
                data = self.ctest(["lifecycle"], f'<testcase name="lifecycle"><{first}/><{second}/></testcase>')
                self.assertEqual(data["passed"], 0)
                self.assertTrue(any("conflicting outcomes" in error for error in data["errors"]))

    def test_unexpected_test_does_not_replace_required_test(self):
        data = self.ctest(["lifecycle", "other"], '<testcase name="other"/>', ["lifecycle"])
        self.assertEqual(data["unexpected"], ["other"])
        self.assertEqual(data["missing"], ["lifecycle"])

    def test_duplicate_results_fail(self):
        data = self.ctest(["lifecycle"], '<testcase name="lifecycle"/><testcase name="lifecycle"/>')
        self.assertTrue(any("duplicate testcase" in error for error in data["errors"]))

    def test_duplicate_inventory_fails(self):
        data = self.ctest(["lifecycle", "lifecycle"], '<testcase name="lifecycle"/>')
        self.assertTrue(any("duplicate names" in error for error in data["errors"]))

    def test_empty_inventory_and_results_cannot_pass(self):
        data = self.ctest([], "", ["lifecycle"])
        self.assertEqual(data["required"], 0)
        self.assertTrue(data["errors"])

    def test_known_failures_keep_platforms_and_reasons_visible(self):
        data = self.ctest(["lifecycle", "guest_memory", "shared_a", "shared_b"], '<testcase name="lifecycle"/>', ["lifecycle"],
                          "# existing debt\nmacos guest_memory protection is refused\nall shared_.* known shared issue\nlinux only_linux unsupported\n")
        self.assertEqual(data["errors"], [])
        self.assertEqual(len(data["known_macos_failures"]), 2)
        self.assertEqual(data["known_macos_failures"][0]["reason"], "protection is refused")
        self.assertEqual(data["known_macos_failures"][1]["registered_tests"], ["shared_a", "shared_b"])

    def test_required_test_cannot_be_hidden_by_an_exclusion(self):
        data = self.ctest(["lifecycle"], '<testcase name="lifecycle"/>', exclusions="linux,macos lifecycle still unverified\n")
        self.assertTrue(any("still excluded" in error for error in data["errors"]))

    def test_swift_inventory_and_class_names_match_every_case(self):
        self.inventory.write_text("Building for debugging...\nAnyPS5CoreTests.InstallerRunTests/testRun\nAnyPS5CoreTests.InstallerRunTests/testEnvironment\nAnyPS5CoreTests.OtherTests/testOther\n", encoding="utf-8")
        self.junit.write_text('<testsuites><testsuite><testcase classname="AnyPS5CoreTests.InstallerRunTests" name="testRun"/><testcase classname="AnyPS5CoreTests.InstallerRunTests" name="testEnvironment"/></testsuite></testsuites>', encoding="utf-8")
        self.log.write_text("Test Case '-[AnyPS5CoreTests.InstallerRunTests testRun]' passed (0.001 seconds).\nTest Case '-[AnyPS5CoreTests.InstallerRunTests testEnvironment]' passed (0.002 seconds).\n", encoding="utf-8")
        data = required_test_report.report(self.inventory, self.junit, "swift", [r"AnyPS5CoreTests\.InstallerRunTests/[^\s]+"], log_path=self.log)
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["required"], 2)
        self.assertEqual(data["passed"], 2)

    def test_swift_successful_process_is_not_a_pass_without_xctest_completion(self):
        self.inventory.write_text("AnyPS5CoreTests.InstallerRunTests/testRun\n", encoding="utf-8")
        self.junit.write_text('<testsuites><testsuite><testcase classname="AnyPS5CoreTests.InstallerRunTests" name="testRun"/></testsuite></testsuites>', encoding="utf-8")
        for log in ("", "Test Suite 'Selected tests' passed\nExecuted 0 tests, with 0 failures\n"):
            with self.subTest(log=log):
                self.log.write_text(log, encoding="utf-8")
                data = required_test_report.report(self.inventory, self.junit, "swift", [r"AnyPS5CoreTests\.InstallerRunTests/[^\s]+"], log_path=self.log)
                self.assertEqual(data["passed"], 0)
                self.assertEqual(data["not_run"], 1)
                self.assertTrue(data["errors"])

    def test_swift_successful_process_with_xctest_skip_or_failure_fails(self):
        self.inventory.write_text("AnyPS5CoreTests.InstallerRunTests/testRun\n", encoding="utf-8")
        self.junit.write_text('<testsuites><testsuite><testcase classname="AnyPS5CoreTests.InstallerRunTests" name="testRun"/></testsuite></testsuites>', encoding="utf-8")
        for status in ("skipped", "failed"):
            with self.subTest(status=status):
                self.log.write_text(f"Test Case '-[AnyPS5CoreTests.InstallerRunTests testRun]' {status} (0.001 seconds).\n", encoding="utf-8")
                data = required_test_report.report(self.inventory, self.junit, "swift", [r"AnyPS5CoreTests\.InstallerRunTests/[^\s]+"], log_path=self.log)
                self.assertEqual(data["passed"], 0)
                self.assertEqual(data[status], 1)
                self.assertTrue(data["errors"])

    def test_swift_log_cannot_replace_a_different_nonpassing_xml_outcome(self):
        name = "AnyPS5CoreTests.InstallerRunTests/testRun"
        self.inventory.write_text(name + "\n", encoding="utf-8")
        for xml, log, retained in (("skipped", "failed", "skipped"), ("failure", "skipped", "failed"), ("error", "passed", "failed")):
            with self.subTest(xml=xml, log=log):
                self.junit.write_text('<testsuites><testsuite><testcase classname="AnyPS5CoreTests.InstallerRunTests" name="testRun">'
                                      f'<{xml}/></testcase></testsuite></testsuites>', encoding="utf-8")
                self.log.write_text(f"Test Case '-[AnyPS5CoreTests.InstallerRunTests testRun]' {log} (0.001 seconds).\n", encoding="utf-8")
                data = required_test_report.report(self.inventory, self.junit, "swift", [name], log_path=self.log)
                self.assertEqual(data["passed"], 0)
                self.assertEqual(data["tests"][name]["status"], retained)
                self.assertTrue(any("statuses disagree" in error for error in data["errors"]))

    def test_swift_completed_pass_does_not_hide_a_suite_error(self):
        name = "AnyPS5CoreTests.InstallerRunTests/testRun"
        self.inventory.write_text(name + "\n", encoding="utf-8")
        self.junit.write_text('<testsuites><testsuite errors="1"><testcase classname="AnyPS5CoreTests.InstallerRunTests" name="testRun"/>'
                              '<error>worker failed</error></testsuite></testsuites>', encoding="utf-8")
        self.log.write_text("Test Case '-[AnyPS5CoreTests.InstallerRunTests testRun]' passed (0.001 seconds).\n", encoding="utf-8")
        data = required_test_report.report(self.inventory, self.junit, "swift", [name], log_path=self.log)
        self.assertEqual(data["passed"], 0)
        self.assertTrue(any("suite-level error" in error for error in data["errors"]))

    def test_swift_duplicate_completions_cannot_mask_a_skip(self):
        self.log.write_text("Test Case '-[AnyPS5CoreTests.InstallerRunTests testRun]' skipped (0.001 seconds).\nTest Case '-[AnyPS5CoreTests.InstallerRunTests testRun]' passed (0.001 seconds).\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate XCTest completion"):
            required_test_report.swift_outcomes(self.log)

    def test_portable_xctest_completion_format(self):
        self.log.write_text("Test Case 'AnyPS5CoreTests.InstallerRunTests.testRun' passed (0.001 seconds).\n", encoding="utf-8")
        self.assertEqual(required_test_report.swift_outcomes(self.log), {"AnyPS5CoreTests.InstallerRunTests/testRun": "passed"})

    def test_short_xctest_class_matches_one_discovered_identity(self):
        self.log.write_text("Test Case 'InstallerRunTests.testRun' passed (0.001 seconds).\n", encoding="utf-8")
        name = "AnyPS5CoreTests.InstallerRunTests/testRun"
        self.assertEqual(required_test_report.swift_outcomes(self.log, [name]), {name: "passed"})

    def test_ambiguous_short_xctest_class_fails(self):
        self.log.write_text("Test Case 'InstallerRunTests.testRun' passed (0.001 seconds).\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not identify one"):
            required_test_report.swift_outcomes(self.log, ["First.InstallerRunTests/testRun", "Second.InstallerRunTests/testRun"])

    def test_swift_missing_suite_fails(self):
        self.inventory.write_text("AnyPS5CoreTests.OtherTests/testOther\n", encoding="utf-8")
        self.junit.write_text("<testsuites/>", encoding="utf-8")
        data = required_test_report.report(self.inventory, self.junit, "swift", [r"AnyPS5CoreTests\.InstallerRunTests/[^\s]+"])
        self.assertTrue(data["errors"])

    def test_cli_writes_failure_report_when_build_did_not_produce_results(self):
        output = self.root / "diagnostics/report.json"
        summary = self.root / "summary.md"
        env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary))
        tool = Path(required_test_report.__file__)
        result = subprocess.run([sys.executable, str(tool), "--inventory", str(self.inventory), "--junit", str(self.junit),
                                 "--require", "lifecycle", "--output", str(output)], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        data = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(data["passed"], 0)
        self.assertTrue(data["errors"])
        self.assertEqual(len(data["revision"]), 40)
        self.assertIn("GPU rendering", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
