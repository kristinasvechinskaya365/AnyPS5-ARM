import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tools.macos import bundle_minimum


SCANNER = Path(__file__).resolve().parents[1] / "macos/bundle_minimum.py"


def load_commands(minimum="14.0", platform="1", legacy=False):
    metadata = (f"      cmd LC_VERSION_MIN_MACOSX\n  cmdsize 16\n  version {minimum}\n      sdk 26.0\n" if legacy else
                f"      cmd LC_BUILD_VERSION\n  cmdsize 32\n platform {platform}\n    minos {minimum}\n"
                "      sdk 26.0\n   ntools 1\n     tool 3\n  version 1234.5\n")
    return "/fixture/AnyPS5 (architecture arm64):\nLoad command 0\n      cmd LC_UUID\n  cmdsize 24\n" + "Load command 1\n" + metadata


class BundleMinimumTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="AnyPS5 minimum ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app = self.root / "AnyPS5.app"
        library = self.app / "Contents/Resources/toolkit/libs/libc.prx"
        library.parent.mkdir(parents=True)
        library.write_bytes(b"fixture")
        (library.parent / "libkernel.prx").write_bytes(b"fixture")
        self.control = {"app": str(self.app), "binaries": {}, "errors": {}}
        for path, expected in bundle_minimum.bundled_binaries(self.app):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
            architectures = ["arm64", "x86_64"] if path.suffix == ".dylib" else expected
            self.control["binaries"][str(path.relative_to(self.app))] = {
                "architectures": architectures, "loads": {arch: load_commands() for arch in architectures},
            }
        commands = self.root / "commands"
        commands.mkdir()
        helper = commands / "command.py"
        helper.write_text("#!" + sys.executable + "\n" + textwrap.dedent("""\
            import json
            import os
            from pathlib import Path
            import sys

            control_file = Path(os.environ["BUNDLE_MINIMUM_TEST_CONTROL"])
            control = json.loads(control_file.read_text())
            name = Path(sys.argv[0]).name
            arguments = sys.argv[1:]
            with control_file.with_suffix(".calls").open("a") as output:
                output.write(json.dumps([name, *arguments]) + "\\n")
            error = control["errors"].get(name)
            if error:
                sys.stderr.write(error.get("stderr", ""))
                sys.exit(error.get("exit", 0))
            path = Path(arguments[1] if name == "lipo" and arguments[0] == "-archs" else
                        arguments[0] if name == "lipo" else arguments[-1])
            entry = control["binaries"][str(path.relative_to(control["app"]))]
            if name == "lipo":
                if arguments[0] == "-archs":
                    print(" ".join(entry["architectures"]))
                else:
                    sys.exit(0 if set(arguments[2:]).issubset(entry["architectures"]) else 1)
            else:
                if arguments[:1] != ["-arch"] or arguments[2] != "-l":
                    sys.exit(99)
                print(entry["loads"][arguments[1]])
            """), encoding="utf-8")
        helper.chmod(0o755)
        for name in ("lipo", "otool"):
            (commands / name).symlink_to(helper.name)
        self.control_file = self.root / "control.json"
        self.report_file = self.root / "minimum.json"
        self.environment = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                            "BUNDLE_MINIMUM_TEST_CONTROL": str(self.control_file)}

    def run_scanner(self, *arguments):
        self.control_file.write_text(json.dumps(self.control))
        self.report_file.unlink(missing_ok=True)
        return subprocess.run([sys.executable, str(SCANNER), str(self.app), "--report", str(self.report_file), *arguments],
                              env=self.environment, capture_output=True, text=True, timeout=15)

    def first(self):
        return self.control["binaries"]["Contents/MacOS/AnyPS5"]

    def test_every_actual_slice_contributes_to_the_packaged_minimum(self):
        self.control["binaries"]["Contents/Resources/toolkit/libs/libkernel.prx"]["loads"]["x86_64"] = load_commands("26.3.1")
        self.control["binaries"]["Contents/Resources/toolkit/vulkan/libMoltenVK.dylib"]["loads"]["arm64"] = load_commands("26.4")
        result = self.run_scanner()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "26.4\n", ""))
        report = json.loads(self.report_file.read_text())
        self.assertEqual(report["minimum_macos"], "26.4")
        self.assertEqual({entry["path"] for entry in report["binaries"]}, set(self.control["binaries"]))
        self.assertEqual(sum(len(entry["minimum_macos"]) for entry in report["binaries"]), 17)
        calls = [json.loads(line) for line in self.control_file.with_suffix(".calls").read_text().splitlines()]
        self.assertEqual(sum(call[0] == "otool" for call in calls), 17)
        self.assertEqual(sum("-verify_arch" in call for call in calls), 10)
        self.assertTrue(all(call[1] == "-arch" and call[3] == "-l" for call in calls if call[0] == "otool"))

    def test_older_slices_retain_the_launcher_floor_without_using_sdk_version(self):
        for entry in self.control["binaries"].values():
            entry["loads"] = {arch: load_commands("10.15", legacy=arch == "x86_64") for arch in entry["architectures"]}
        result = self.run_scanner()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "14.0\n", ""))

    def test_versions_compare_numerically_across_all_components(self):
        self.first()["loads"].update(arm64=load_commands("14.9.12"), x86_64=load_commands("14.10.1"))
        result = self.run_scanner()
        self.assertEqual((result.returncode, result.stdout), (0, "14.10.1\n"), result.stderr)

    def test_numeric_and_symbolic_macos_platforms_are_supported(self):
        for platform in ("1", "MACOS"):
            with self.subTest(platform=platform):
                self.assertEqual(bundle_minimum.minimum_from_load_commands(load_commands("14.2.3", platform)), (14, 2, 3))
        self.assertEqual(bundle_minimum.minimum_from_load_commands(load_commands("10.15.7", legacy=True)), (10, 15, 7))

    def test_non_macos_platforms_are_rejected_before_publication(self):
        for metadata in (load_commands(platform="2"), load_commands(platform="6"), load_commands(platform="MACCATALYST"),
                         load_commands(legacy=True).replace("LC_VERSION_MIN_MACOSX", "LC_VERSION_MIN_IPHONEOS")):
            with self.subTest(metadata=metadata):
                self.first()["loads"]["arm64"] = metadata
                result = self.run_scanner()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("non-macOS", result.stderr)
                self.assertFalse(self.report_file.exists())

    def test_missing_duplicate_and_conflicting_load_commands_are_rejected(self):
        valid = load_commands()
        for metadata in ("", "Load command 0\n      cmd LC_SOURCE_VERSION\n  version 26.0\n",
                         valid + valid, valid + load_commands("26.0", legacy=True),
                         valid.replace(" platform 1\n", ""), valid.replace("    minos 14.0\n", ""),
                         valid.replace("    minos 14.0\n", "    minos 14.0\n    minos 26.0\n"),
                         valid.replace("cmdsize 32", "cmdsize 32 Incorrect size")):
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    bundle_minimum.minimum_from_load_commands(metadata)

    def test_malformed_minimum_versions_are_rejected(self):
        for value in ("0.0", "14..0", "14.0beta", "14.0.0.0", "14.256", "14.0.256", "65536.0", "-1", " 14.0", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    bundle_minimum.version(value)
        self.first()["loads"]["arm64"] = load_commands("14.0beta")
        result = self.run_scanner()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid macOS version", result.stderr)

    def test_missing_runtime_files_and_empty_library_sets_are_rejected(self):
        path = self.app / "Contents/Resources/toolkit/vulkan/libvulkan.1.dylib"
        path.unlink()
        self.assertNotEqual(self.run_scanner().returncode, 0)
        path.write_bytes(b"fixture")
        for library in (self.app / "Contents/Resources/toolkit/libs").glob("*.prx"):
            library.unlink()
        result = self.run_scanner()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no engine libraries", result.stderr)

    def test_external_binary_links_are_rejected(self):
        binary = self.app / "Contents/MacOS/AnyPS5"
        outside = self.root / "outside"
        binary.rename(outside)
        binary.symlink_to(outside)
        result = self.run_scanner()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid bundled binary", result.stderr)

    def test_missing_and_duplicate_architectures_are_rejected(self):
        for architectures in (["arm64"], ["arm64", "x86_64", "arm64"], ["arm64", "x86_64", "--invalid"]):
            with self.subTest(architectures=architectures):
                self.first()["architectures"] = architectures
                self.assertNotEqual(self.run_scanner().returncode, 0)
                self.assertFalse(self.report_file.exists())

    def test_tool_errors_and_unexpected_diagnostics_are_fatal(self):
        for name, error in (("lipo", {"exit": 1}), ("otool", {"exit": 1}),
                            ("otool", {"stderr": "malformed object\n"})):
            with self.subTest(name=name, error=error):
                self.control["errors"] = {name: error}
                result = self.run_scanner()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.report_file.exists())

    def test_declared_minimum_must_cover_every_slice_and_the_floor(self):
        self.first()["loads"]["x86_64"] = load_commands("26.1")
        result = self.run_scanner("--declared", "14.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bundled binaries require 26.1", result.stderr)
        self.assertFalse(self.report_file.exists())
        for declared in ("26.1", "26.2"):
            with self.subTest(declared=declared):
                result = self.run_scanner("--declared", declared)
                self.assertEqual(result.returncode, 0, result.stderr)
        for declared in (None, "unknown", "13.9"):
            with self.subTest(declared=declared):
                with self.assertRaises(ValueError):
                    bundle_minimum.check_declared({"minimum_macos": "14.0"}, declared)


if __name__ == "__main__":
    unittest.main()
