import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


TRANSLATOR = pathlib.Path(sys.argv.pop(1)).resolve()


class TranslatorCliTests(unittest.TestCase):
    def invoke(self, *arguments):
        return subprocess.run([str(TRANSLATOR), *map(str, arguments)], capture_output=True, text=True, timeout=5)

    def test_default_target_metadata(self):
        result = self.invoke("--build-info")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = json.loads(result.stdout)
        self.assertEqual(info["schema_version"], 1)
        self.assertEqual(info["target_cpu"], "apple-m1")
        self.assertEqual(info["target_triple"], "arm64-apple-macosx13.0.0")
        self.assertEqual(info["optimization"], "O2")
        self.assertRegex(info["llvm_version"], r"^\d+\.\d+")
        self.assertRegex(info["capstone_api_version"], r"^\d+\.\d+$")

    def test_unknown_cpu_rejected_before_opening_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            output = root / "guest.o"
            result = self.invoke(root / "missing.elf", output, "--cpu", "anyps5-invalid-cpu")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("is not supported by LLVM", result.stderr)
            self.assertFalse(output.exists())

    def test_apple_m2_is_selected_only_if_supported(self):
        result = self.invoke("--build-info", "--cpu", "apple-m2")
        if result.returncode == 0:
            self.assertEqual(json.loads(result.stdout)["target_cpu"], "apple-m2")
        else:
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("is not supported by LLVM", result.stderr)

    def test_malformed_options_are_errors(self):
        cases = (("--build-info", "--unknown"), ("--build-info", "--cpu"), ("--build-info", "--build-info"),
                 ("--build-info", "--emit-llvm"), ("--build-info", "input.elf", "out.o"),
                 ("--build-info", "--cpu", "apple-m1", "--cpu", "apple-m1"))
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
