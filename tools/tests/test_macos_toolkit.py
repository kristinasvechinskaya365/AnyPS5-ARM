import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


LAYOUT = Path(__file__).resolve().parents[1] / "macos/toolkit_layout.sh"


@unittest.skipUnless(os.name == "posix", "macOS toolkit shell scripts require a POSIX host")
class MacOSToolkitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="toolkit-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.toolkit = self.root / "toolkit with spaces"
        self.inputs = {
            "relinker": b"#!/bin/sh\necho relinker\n",
            "libs/libSceAgc.prx": b"libSceAgc",
            "libs/libSceAudioOut.prx": b"libSceAudioOut",
            "vulkan/libvulkan.1.dylib": b"loader",
            "vulkan/libMoltenVK.dylib": b"moltenvk",
            "vulkan/MoltenVK_icd.json": b"{}",
            "patches/PPSA02100.json": b'{"titleId":"PPSA02100"}',
        }
        for name, contents in self.inputs.items():
            path = self.toolkit / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        (self.toolkit / "version.txt").write_text("0.2.35-arm-fixture\n", encoding="utf-8")

    def run_function(self, function, *arguments):
        return subprocess.run(["/bin/sh", "-c", '. "$1"; shift; "$@"', "toolkit-test", str(LAYOUT),
                               function, *map(str, arguments)], capture_output=True, text=True, timeout=10)

    def fingerprint(self):
        result = self.run_function("toolkit_fingerprint", self.toolkit)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^[0-9a-f]{12}\n$")
        return result.stdout.strip()

    def manifest(self):
        return self.run_function("write_toolkit_manifest", LAYOUT.parents[2], self.toolkit, "0.2.15")

    def test_every_runtime_input_changes_the_fingerprint(self):
        original = self.fingerprint()
        self.assertEqual(original, "9375296f48d3")
        for name, contents in self.inputs.items():
            with self.subTest(name=name):
                path = self.toolkit / name
                path.write_bytes(contents + b"changed")
                self.assertNotEqual(self.fingerprint(), original)
                path.write_bytes(contents)
                self.assertEqual(self.fingerprint(), original)

    def test_path_names_and_content_boundaries_are_part_of_the_identity(self):
        first, second = self.toolkit / "libs/libSceAgc.prx", self.toolkit / "libs/libSceAudioOut.prx"
        first.write_bytes(b"ab")
        second.write_bytes(b"c")
        original = self.fingerprint()
        first.write_bytes(b"a")
        second.write_bytes(b"bc")
        changed = self.fingerprint()
        self.assertNotEqual(changed, original)
        first.rename(first.with_name("renamed.prx"))
        self.assertNotEqual(self.fingerprint(), changed)

    def test_paths_use_utf8_byte_order_independent_of_creation_order(self):
        names = ["libs/é.prx", "libs/Z.prx", "libs/Å.prx", "libs/a.prx"]
        for name in names:
            (self.toolkit / name).write_bytes(name.encode("utf-8"))
        original = self.fingerprint()
        for name in names:
            (self.toolkit / name).unlink()
        for name in reversed(names):
            (self.toolkit / name).write_bytes(name.encode("utf-8"))
        self.assertEqual(self.fingerprint(), original)

    def test_metadata_and_unrelated_files_do_not_change_the_runtime_identity(self):
        original = self.fingerprint()
        for name in ("version.txt", "toolkit.json", "libs/LICENSE.txt", "vulkan/README.txt", "patches/README.md"):
            (self.toolkit / name).write_text("metadata", encoding="utf-8")
        self.assertEqual(self.fingerprint(), original)

    def test_missing_required_inputs_do_not_produce_an_identity(self):
        for name in ("relinker", "vulkan/libvulkan.1.dylib", "vulkan/libMoltenVK.dylib", "vulkan/MoltenVK_icd.json"):
            with self.subTest(name=name):
                path = self.toolkit / name
                contents = path.read_bytes()
                path.unlink()
                result = self.run_function("toolkit_fingerprint", self.toolkit)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn(name, result.stderr)
                path.write_bytes(contents)

    def test_incomplete_folders_and_nonregular_inputs_are_rejected(self):
        for directory in ("libs", "patches"):
            with self.subTest(directory=directory):
                path = self.toolkit / directory
                kept = path.with_name(directory + "-kept")
                path.rename(kept)
                self.assertNotEqual(self.run_function("toolkit_fingerprint", self.toolkit).returncode, 0)
                kept.rename(path)
        for name in ("libs/libSceAgc.prx", "libs/libSceAudioOut.prx"):
            (self.toolkit / name).unlink()
        self.assertNotEqual(self.run_function("toolkit_fingerprint", self.toolkit).returncode, 0)
        (self.toolkit / "libs/invalid.prx").mkdir()
        result = self.run_function("toolkit_fingerprint", self.toolkit)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a regular file", result.stderr)

    def test_make_toolkit_uses_the_complete_fingerprint(self):
        source, build, sdk, output = (self.root / name for name in ("source", "build", "sdk", "output"))
        for directory in (source / "tools/macos/patches", build / "core/relinker", build / "core/libs/libs",
                          sdk / "lib", sdk / "share/vulkan/icd.d"):
            directory.mkdir(parents=True)
        (source / "tools/macos/moltenvk_patches.py").write_text("pass\n", encoding="utf-8")
        shutil.copy2(self.toolkit / "relinker", build / "core/relinker/relinker")
        for file in (self.toolkit / "libs").iterdir():
            shutil.copy2(file, build / "core/libs/libs" / file.name)
        for file in (self.toolkit / "patches").iterdir():
            shutil.copy2(file, source / "tools/macos/patches" / file.name)
        for name in ("libvulkan.1.dylib", "libMoltenVK.dylib"):
            shutil.copy2(self.toolkit / "vulkan" / name, sdk / "lib" / name)
        (sdk / "share/vulkan/icd.d/MoltenVK_icd.json").write_text('{"ICD": {"library_path": "old"}}\n', encoding="utf-8")
        result = self.run_function("make_toolkit", source, build, sdk, output, "0.2.35-arm")
        self.assertEqual(result.returncode, 0, result.stderr)
        original = (output / "version.txt").read_text()
        self.assertRegex(original, r"^0\.2\.35-arm-[0-9a-f]{12}\n$")
        (sdk / "lib/libMoltenVK.dylib").write_bytes(b"new graphics runtime")
        result = self.run_function("make_toolkit", source, build, sdk, output, "0.2.35-arm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual((output / "version.txt").read_text(), original)

    def test_manifest_records_every_plain_file(self):
        (self.toolkit / "version.txt").write_text("0.2.35-arm-" + self.fingerprint() + "\n", encoding="utf-8")
        (self.toolkit / ".metadata").write_bytes(b"hidden")
        result = self.manifest()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.toolkit / "toolkit.json").read_text())
        expected = {path.relative_to(self.toolkit).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self.toolkit.rglob("*") if path.is_file() and path.name != "toolkit.json"}
        self.assertEqual(manifest["files"], expected)

    def test_manifest_rejects_file_and_directory_links_before_replacing_output(self):
        manifest = self.toolkit / "toolkit.json"
        manifest.write_text("previous manifest", encoding="utf-8")
        for target in (self.root / "external", self.root / "external-file", self.root / "missing"):
            if target.name == "external":
                target.mkdir()
                (target / "dependency.prx").write_bytes(b"outside")
            elif target.name == "external-file":
                target.write_bytes(b"outside")
            link = self.toolkit / "linked"
            link.symlink_to(target, target_is_directory=target.is_dir())
            result = self.manifest()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("is a link", result.stderr)
            self.assertEqual(manifest.read_text(), "previous manifest")
            link.unlink()

    def test_manifest_does_not_follow_an_existing_manifest_link(self):
        outside = self.root / "external-manifest"
        outside.write_text("keep", encoding="utf-8")
        (self.toolkit / "toolkit.json").symlink_to(outside)
        result = self.manifest()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is a link", result.stderr)
        self.assertEqual(outside.read_text(), "keep")

    def test_manifest_rejects_named_pipes_without_opening_them(self):
        os.mkfifo(self.toolkit / "pipe")
        result = self.manifest()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a plain file", result.stderr)


if __name__ == "__main__":
    unittest.main()
