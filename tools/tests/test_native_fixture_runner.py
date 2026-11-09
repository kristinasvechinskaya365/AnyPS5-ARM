import contextlib
import copy
import importlib.util
import io
import json
import pathlib
import struct
import subprocess
import tempfile
import unittest
from unittest import mock


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "translator/tests/run_native_fixtures.py"
SPEC = importlib.util.spec_from_file_location("native_fixture_runner", SOURCE)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)
MODEL_SPEC = importlib.util.spec_from_file_location("cmpxchg_amd_model", SOURCE.with_name("cmpxchg_amd_model.py"))
MODEL = importlib.util.module_from_spec(MODEL_SPEC)
MODEL_SPEC.loader.exec_module(MODEL)


class NativeFixtureRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.build = self.root / "build"
        self.build.mkdir()
        self.libraries = self.root / "libraries"
        self.libraries.mkdir()
        (self.build / "aps5-translator").write_bytes(b"translator fixture")
        (self.build / "aps5-translator").chmod(0o755)
        (self.build / "libaps5-translator-runtime.a").write_bytes(b"runtime fixture")
        (self.libraries / "libc.prx").write_bytes(b"libc fixture")
        self.tools = {name: {"path": f"/fixture-tools/{name}", "available": True, "version": "fixture tool 1"}
                      for name in (*RUNNER.COMPILED_TOOLS, "native_linker", "reference_arch", "reference_python")}
        self.host = {"system": "Darwin", "machine": "arm64", "release": "25.0.0", "macos": "26.0", "cpu": "Apple M2", "python": "3.13"}
        self.info = {"schema_version": 1, "llvm_version": "22.1.8", "target_triple": "arm64-apple-macosx13.0.0",
                     "target_cpu": "apple-m1", "optimization": "O2", "capstone_api_version": "5.0"}
        self.results = {"import": (0, "hello from a PS5 ELF on macOS\n"), "lifter-smoke": (28, "3b0c6e4bf8726a1c\n"),
                        "vector-smoke": (42, "000000000000002a\n"), "cmpxchg-alias": (9, "ca6fbb61f3358b09\n"),
                        "cmpxchg-amd-register": (118, "475cd741284f3476\n"), "lea-address-size": (114, "e294d24a144bdbf2\n"), "leave-operand-size": (14, "aa69bd1b27fa6a0e\n")}
        self.failures = {}
        self.calls = []
        self.architecture = 0x0100000C
        self.reference = {"vector-smoke": (42, "000000000000002a\n"), "cmpxchg-alias": (9, "ca6fbb61f3358b09\n"),
                          "lea-address-size": (114, "e294d24a144bdbf2\n"), "leave-operand-size": (14, "aa69bd1b27fa6a0e\n")}
        self.model = MODEL.reference()
        self.guest_compilations = []

    def fake_run(self, command, **kwargs):
        command = [str(argument) for argument in command]
        self.calls.append((command, kwargs))
        if command[0] == str(self.build / "aps5-translator"):
            if "--build-info" in command:
                name, stage = "preflight", "translator-info"
            else:
                name, stage = pathlib.Path(command[1]).parent.name, "translate"
        elif command[0] == self.tools["guest_clang"]["path"]:
            name, stage = pathlib.Path(command[command.index("-o") + 1]).parent.name, "reference-build"
        elif command[0] == self.tools["reference_arch"]["path"]:
            name, stage = pathlib.Path(command[-1]).parent.name, "reference-run"
        elif command[0] == self.tools["reference_python"]["path"]:
            name, stage = "cmpxchg-amd-register", "reference-model"
        elif command[0] == self.tools["native_linker"]["path"]:
            name, stage = pathlib.Path(command[command.index("-o") + 1]).parent.name, "native-link"
        elif pathlib.Path(command[0]).name == "eboot":
            name, stage = pathlib.Path(command[0]).parent.name, "native-run"
        else:
            self.fail(f"unexpected command: {command}")
        failure = self.failures.get((name, stage))
        if failure is not None:
            if isinstance(failure, Exception):
                raise failure
            return subprocess.CompletedProcess(command, *failure)
        if stage == "translator-info":
            return subprocess.CompletedProcess(command, 0, json.dumps(self.info), "")
        if stage == "translate":
            pathlib.Path(command[2]).write_bytes(b"object fixture")
        elif stage == "native-link":
            header = struct.pack("<IIIIIIII", 0xFEEDFACF, self.architecture, 0, 2, 0, 0, 0, 0)
            pathlib.Path(command[command.index("-o") + 1]).write_bytes(header)
        elif stage == "native-run":
            return subprocess.CompletedProcess(command, *self.results[name], "")
        elif stage == "reference-run":
            return subprocess.CompletedProcess(command, *self.reference[name], "")
        elif stage == "reference-model":
            return subprocess.CompletedProcess(command, 0, json.dumps(self.model), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def compiled_guest(self, directory, source, flags):
        self.guest_compilations.append((directory.name, source, flags))
        failure = self.failures.get((directory.name, "fixture-build"))
        if failure is not None:
            raise failure
        (directory / "eboot.elf").write_bytes(b"guest fixture")

    def run_suite(self, *extra):
        report_file = self.root / "report.json"
        with mock.patch.object(RUNNER, "host_identity", return_value=self.host), \
                mock.patch.object(RUNNER, "toolchain_identity", return_value=copy.deepcopy(self.tools)), \
                mock.patch.object(RUNNER, "compiled_guest", side_effect=self.compiled_guest), \
                mock.patch.object(RUNNER.subprocess, "run", side_effect=self.fake_run), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = RUNNER.main([str(self.build), str(self.libraries), str(self.root / "new/work"), "--report", str(report_file), *extra])
        return code, json.loads(report_file.read_text()), stdout.getvalue()

    def test_all_required_results_and_actual_binary_identity(self):
        code, report, output = self.run_suite()
        self.assertEqual(code, 0)
        self.assertTrue(report["complete"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["counts"], {"required": 7, "pass": 7, "fail": 0, "skip": 0})
        self.assertEqual([case["name"] for case in report["fixtures"]], list(RUNNER.REQUIRED))
        self.assertTrue(all(case["required"] for case in report["fixtures"]))
        for name in ("translator", "runtime", "libc", "runner", "guesttools"):
            self.assertRegex(report["provenance"][name]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(report["provenance"]["translator"]["build_info"], self.info)
        self.assertIn("required=7 pass=7 fail=0 skip=0", output)
        native_commands = [call for call in self.calls if pathlib.Path(call[0][0]).name == "eboot"]
        self.assertEqual(len(native_commands), 7)
        self.assertTrue(all(len(command) == 1 and kwargs["cwd"] == pathlib.Path(command[0]).parent for command, kwargs in native_commands))
        reference = next(command for command, _ in self.calls if command[0] == self.tools["guest_clang"]["path"])
        self.assertIn("-O0", reference)
        self.assertTrue(all(flag in reference for flag in RUNNER.VECTOR_FLAGS))
        self.assertEqual(report["fixtures"][1]["compile_flags"], ["-fno-inline"])
        self.assertEqual(self.guest_compilations[0], ("lifter-smoke", "lifter_smoke.c", ["-fno-inline"]))

    def test_missing_guest_compiler_is_counted_for_all_compiled_cases(self):
        for tool in ("guest_clang", "guest_clangxx", "guest_linker"):
            with self.subTest(tool=tool):
                self.tools[tool]["available"] = False
                code, report, _ = self.run_suite()
                self.assertEqual(code, 1)
                self.assertTrue(report["complete"])
                self.assertFalse(report["passed"])
                self.assertEqual(report["counts"], {"required": 7, "pass": 1, "fail": 0, "skip": 6})
                self.assertTrue(all(tool in case["reason"] for case in report["fixtures"][1:]))
                self.tools[tool]["available"] = True

    def test_missing_reference_execution_is_explicit(self):
        self.tools["reference_arch"]["available"] = False
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 3, "fail": 0, "skip": 4})
        self.assertIn("reference_arch", report["fixtures"][2]["reason"])

    def test_reference_compilation_failure_is_not_a_skip(self):
        self.failures[("vector-smoke", "reference-build")] = (1, "", "missing SDK")
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 6, "fail": 1, "skip": 0})
        case = report["fixtures"][2]
        self.assertEqual(case["stage"], "reference-build")
        self.assertIn("missing SDK", case["reason"])
        self.assertEqual(pathlib.Path(case["commands"][0]["stderr"]).read_text(), "missing SDK")

    def test_cmpxchg_reference_uses_only_its_required_compiler_flags(self):
        code, report, _ = self.run_suite()
        self.assertEqual(code, 0)
        case = report["fixtures"][3]
        self.assertEqual(case["name"], "cmpxchg-alias")
        self.assertEqual(case["reference"], {"execution": "x86_64 under Rosetta", "optimization": "O0", "flags": []})
        command = case["commands"][0]["command"]
        self.assertIn(str(RUNNER.HERE / "cmpxchg_alias.c"), command)
        self.assertFalse(any(flag in command for flag in RUNNER.VECTOR_FLAGS))
        self.assertEqual([entry["stage"] for entry in case["commands"]], ["reference-build", "reference-run", "translate", "native-link", "native-run"])

    def test_cmpxchg_mismatch_cannot_pass_the_other_six_results(self):
        self.results["cmpxchg-alias"] = (1, "cmpxchg accumulator alias mismatch\n")
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 6, "fail": 1, "skip": 0})
        case = report["fixtures"][3]
        self.assertEqual((case["name"], case["status"], case["stage"]), ("cmpxchg-alias", "fail", "compare"))
        self.assertTrue(report["complete"])
        self.assertFalse(report["passed"])

    def test_amd_integer_model_matches_hardware_checksum_and_edge_cases(self):
        self.assertEqual(MODEL.reference(), {"schema_version": 1, "contract": RUNNER.AMD_CONTRACT, "cases": 576,
                                            "expected": {"exit_code": 118, "stdout": "475cd741284f3476"}})
        self.assertEqual(MODEL.exchange(0, 1, 0xffffffffffffff7f, 8), (1, 1, 0x0b))
        self.assertEqual(MODEL.exchange(0, 0x7fffffffffffffff, 0xffffffffffffff7f, 32), (0xffffffff, 0x7fffffffffffffff, 0x01))
        self.assertEqual(MODEL.exchange(0x1234567800000001, 0x8765432100000001, 2, 32), (0x1234567800000001, 2, 0x06))

    def test_amd_register_uses_separate_model_and_compile_mode(self):
        code, report, _ = self.run_suite()
        self.assertEqual(code, 0)
        case = report["fixtures"][4]
        self.assertEqual(case["reference"], {"execution": "native Python integer model", "contract": RUNNER.AMD_CONTRACT,
                                             "cases": 576, "source": RUNNER.file_identity(SOURCE.with_name("cmpxchg_amd_model.py"))})
        self.assertEqual(case["compile_flags"], ["-DCMPXCHG_AMD_REGISTERS"])
        self.assertEqual([entry["stage"] for entry in case["commands"]], ["reference-model", "translate", "native-link", "native-run"])
        self.assertIn(("cmpxchg-amd-register", "cmpxchg_alias.c", ["-DCMPXCHG_AMD_REGISTERS"]), self.guest_compilations)
        self.assertIn(("cmpxchg-alias", "cmpxchg_alias.c", []), self.guest_compilations)

    def test_amd_model_error_or_malformed_result_cannot_pass(self):
        invalid = [{}, {**self.model, "cases": 575}, {**self.model, "contract": "Intel"},
                   {**self.model, "expected": {"exit_code": 0, "stdout": "475cd741284f3476"}}]
        for model in invalid:
            with self.subTest(model=model):
                self.model = model
                code, report, _ = self.run_suite()
                self.assertEqual(code, 1)
                case = report["fixtures"][4]
                self.assertEqual((case["status"], case["stage"]), ("fail", "reference-model"))
                self.assertNotIn("translate", [entry["stage"] for entry in case["commands"]])
        self.failures[("cmpxchg-amd-register", "reference-model")] = (2, "", "model unavailable")
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertIn("model unavailable", report["fixtures"][4]["reason"])
        self.tools["reference_python"]["available"] = False
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 6, "fail": 0, "skip": 1})
        self.assertIn("reference_python", report["fixtures"][4]["reason"])

    def test_amd_register_mismatch_cannot_pass_other_results(self):
        self.results["cmpxchg-amd-register"] = (1, "cmpxchg register exchange mismatch\n")
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 6, "fail": 1, "skip": 0})
        self.assertEqual((report["fixtures"][4]["status"], report["fixtures"][4]["stage"]), ("fail", "compare"))

    def test_lea_reference_uses_only_its_required_compiler_flags(self):
        code, report, _ = self.run_suite()
        self.assertEqual(code, 0)
        case = report["fixtures"][5]
        self.assertEqual(case["name"], "lea-address-size")
        self.assertEqual(case["reference"], {"execution": "x86_64 under Rosetta", "optimization": "O0", "flags": []})
        self.assertEqual(case["compile_flags"], [])
        self.assertEqual(case["expected"], {"exit_code": 114, "stdout": "e294d24a144bdbf2"})
        self.assertEqual(case["source"], RUNNER.file_identity(SOURCE.with_name("lea_address_size.c")))
        self.assertEqual([entry["stage"] for entry in case["commands"]], ["reference-build", "reference-run", "translate", "native-link", "native-run"])
        self.assertIn(("lea-address-size", "lea_address_size.c", []), self.guest_compilations)

    def test_lea_mismatch_cannot_pass_the_other_six_results(self):
        self.results["lea-address-size"] = (1, "lea address-size mismatch\n")
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 6, "fail": 1, "skip": 0})
        self.assertEqual((report["fixtures"][5]["status"], report["fixtures"][5]["stage"]), ("fail", "compare"))

    def test_leave_reference_uses_only_its_required_compiler_flags(self):
        code, report, _ = self.run_suite()
        self.assertEqual(code, 0)
        case = report["fixtures"][6]
        self.assertEqual(case["name"], "leave-operand-size")
        self.assertEqual(case["reference"], {"execution": "x86_64 under Rosetta", "optimization": "O0", "flags": []})
        self.assertEqual(case["compile_flags"], [])
        self.assertEqual(case["expected"], {"exit_code": 14, "stdout": "aa69bd1b27fa6a0e"})
        self.assertEqual(case["source"], RUNNER.file_identity(SOURCE.with_name("leave_operand_size.c")))
        self.assertEqual([entry["stage"] for entry in case["commands"]], ["reference-build", "reference-run", "translate", "native-link", "native-run"])
        self.assertIn(("leave-operand-size", "leave_operand_size.c", []), self.guest_compilations)

    def test_leave_mismatch_cannot_pass_the_other_six_results(self):
        self.results["leave-operand-size"] = (1, "leave operand-size mismatch\n")
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 6, "fail": 1, "skip": 0})
        self.assertEqual((report["fixtures"][6]["status"], report["fixtures"][6]["stage"]), ("fail", "compare"))
        self.assertEqual(report["fixtures"][6]["actual"], {"exit_code": 1, "stdout": "leave operand-size mismatch"})
        self.assertTrue(report["complete"])
        self.assertFalse(report["passed"])

    def test_reference_crash_or_bad_checksum_cannot_define_success(self):
        for reference in ((-4, ""), (0, ""), (0, "000000000000002a\n"), (42, "not a checksum\n")):
            with self.subTest(reference=reference):
                self.reference["vector-smoke"] = reference
                code, report, _ = self.run_suite()
                self.assertEqual(code, 1)
                case = report["fixtures"][2]
                self.assertEqual((case["status"], case["stage"]), ("fail", "reference-run"))
                self.assertNotIn("translate", [command["stage"] for command in case["commands"]])

    def test_translate_timeout_retains_output_and_continues_accounting(self):
        self.failures[("import", "translate")] = subprocess.TimeoutExpired("translator", 30, output=b"partial output", stderr=b"partial error")
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 6, "fail": 1, "skip": 0})
        case = report["fixtures"][0]
        self.assertEqual(case["stage"], "translate")
        command = case["commands"][0]
        self.assertIsNone(command["returncode"])
        self.assertEqual(pathlib.Path(command["stdout"]).read_text(), "partial output")
        self.assertEqual(pathlib.Path(command["stderr"]).read_text(), "partial error")

    def test_guest_build_exception_retains_required_result(self):
        self.failures[("lifter-smoke", "fixture-build")] = subprocess.CalledProcessError(1, ["ld.lld"])
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 6, "fail": 1, "skip": 0})
        self.assertEqual(report["fixtures"][1]["stage"], "fixture-build")
        self.assertIn("CalledProcessError", report["fixtures"][1]["reason"])

    def test_native_output_mismatch_is_failure(self):
        self.results["lifter-smoke"] = (28, "wrong\n")
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        case = report["fixtures"][1]
        self.assertEqual((case["status"], case["stage"]), ("fail", "compare"))
        self.assertEqual(case["actual"], {"exit_code": 28, "stdout": "wrong"})

    def test_native_link_failure_retains_required_result(self):
        self.failures[("import", "native-link")] = (1, "", "wrong architecture")
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["fixtures"][0]["stage"], "native-link")
        self.assertEqual(report["counts"], {"required": 7, "pass": 6, "fail": 1, "skip": 0})

    def test_x86_executable_is_rejected_before_execution(self):
        self.architecture = 0x01000007
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 0, "fail": 7, "skip": 0})
        self.assertTrue(all(case["stage"] == "native-architecture" for case in report["fixtures"]))
        self.assertFalse(any(pathlib.Path(command[0]).name == "eboot" for command, _ in self.calls))

    def test_wrong_host_cannot_return_an_empty_pass(self):
        self.host["machine"] = "x86_64"
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"], {"required": 7, "pass": 0, "fail": 0, "skip": 7})
        self.assertTrue(report["complete"])
        self.assertFalse(report["passed"])
        self.assertEqual(self.calls, [])

    def test_missing_runtime_is_recorded_before_translation(self):
        (self.build / "libaps5-translator-runtime.a").unlink()
        code, report, _ = self.run_suite()
        self.assertEqual(code, 1)
        self.assertEqual(report["counts"]["skip"], 7)
        self.assertIn("runtime:", report["fixtures"][0]["reason"])
        self.assertEqual(self.calls, [])

    def test_translator_metadata_must_match_selected_target(self):
        for key, value in (("schema_version", True), ("target_cpu", "generic"), ("target_triple", "x86_64-apple-macosx13.0.0"), ("llvm_version", "")):
            with self.subTest(key=key):
                previous = self.info[key]
                self.info[key] = value
                code, report, _ = self.run_suite()
                self.assertEqual(code, 1)
                self.assertEqual(report["counts"], {"required": 7, "pass": 0, "fail": 7, "skip": 0})
                self.assertTrue(all(case["stage"] == "translator-info" for case in report["fixtures"]))
                self.info[key] = previous

    def test_selected_cpu_is_probed_and_passed_to_every_translation(self):
        self.info["target_cpu"] = "apple-m2"
        code, report, _ = self.run_suite("--cpu", "apple-m2")
        self.assertEqual(code, 0)
        self.assertEqual(report["provenance"]["requested_cpu"], "apple-m2")
        translations = [command for command, _ in self.calls if command[0] == str(self.build / "aps5-translator")]
        self.assertEqual(len(translations), 8)
        self.assertTrue(all(command[-2:] == ["--cpu", "apple-m2"] for command in translations))

    def test_new_work_directory_is_created_and_previous_run_is_preserved(self):
        code, first, _ = self.run_suite()
        self.assertEqual(code, 0)
        sentinel = pathlib.Path(first["work"]) / "sentinel"
        sentinel.write_text("keep")
        code, second, _ = self.run_suite()
        self.assertEqual(code, 0)
        self.assertNotEqual(first["work"], second["work"])
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertTrue(pathlib.Path(first["fixtures"][0]["commands"][0]["stdout"]).is_file())

    def test_missing_or_duplicate_results_cannot_pass(self):
        for names in ((), ("import",), ("import",) * 7, RUNNER.REQUIRED[:-1], ("import", "vector-smoke", "lifter-smoke", "cmpxchg-alias", "cmpxchg-amd-register", "lea-address-size", "leave-operand-size")):
            with self.subTest(names=names):
                report = {"fixtures": [{"name": name, "status": "pass"} for name in names]}
                RUNNER.write_report(self.root / "incomplete.json", report)
                self.assertFalse(report["complete"])
                self.assertFalse(report["passed"])

    def test_tool_identity_preserves_clang_driver_name(self):
        clang = self.root / "clang"
        clang.write_bytes(b"fixture")
        clang.chmod(0o755)
        clangxx = self.root / "clang++"
        clangxx.symlink_to(clang)
        with mock.patch.object(RUNNER.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "clang version 22", "")) as run:
            identity = RUNNER.tool_identity(clangxx)
        self.assertTrue(identity["available"])
        self.assertEqual(identity["path"], str(clangxx))
        self.assertEqual(identity["resolved_path"], str(clang))
        self.assertEqual(run.call_args.args[0], [str(clangxx), "--version"])


if __name__ == "__main__":
    unittest.main()
