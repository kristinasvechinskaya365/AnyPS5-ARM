import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


class MacOSInstalledAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "darwin":
            raise RuntimeError("these installation fixtures require an actual macOS host")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="AnyPS5 install fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.addCleanup(self.clear_flags)
        self.destination = self.root / "Applications with spaces"
        self.destination.mkdir()
        self.target = self.destination / "AnyPS5.app"
        self.bin = self.root / "commands"
        self.bin.mkdir()
        self.environment = {**os.environ, "PATH": str(self.bin) + ":/usr/bin:/bin:/usr/sbin:/sbin"}
        for command in ("open", "softwareupdate"):
            path = self.bin / command
            path.write_text("#!/bin/sh\nprintf '%s\\n' unexpected-command >&2\nexit 99\n")
            path.chmod(0o755)
        self.source = self.make_app("new")
        self.archive = self.root / "AnyPS5-macOS.zip"
        self.sums = self.root / "SHA256SUMS"
        self.pack()

    def clear_flags(self):
        subprocess.run(["/usr/bin/chflags", "-R", "nouchg", str(self.root)], capture_output=True, timeout=10)

    def make_app(self, label):
        app = self.root / label / "AnyPS5.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/Resources").mkdir()
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "org.anyps5.launcher", "CFBundlePackageType": "APPL",
            "CFBundleExecutable": "AnyPS5", "CFBundleName": "AnyPS5",
            "CFBundleVersion": "1.0.0", "CFBundleShortVersionString": "1.0.0", "LSMinimumSystemVersion": "14.0",
        }))
        binary = app / "Contents/MacOS/AnyPS5"
        subprocess.run(["xcrun", "clang", "-x", "c", "-", "-o", str(binary)],
                       input='#include <stdio.h>\nint main(void) { puts("' + label + '"); return 0; }\n',
                       text=True, capture_output=True, check=True, timeout=20)
        (app / "Contents/Resources/marker.txt").write_text(label)
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], capture_output=True, check=True, timeout=10)
        return app

    def pack(self):
        self.archive.unlink(missing_ok=True)
        subprocess.run(["ditto", "-c", "-k", "--norsrc", "--noextattr", "--keepParent", str(self.source), str(self.archive)],
                       check=True, capture_output=True, timeout=10)
        self.sums.write_text(hashlib.sha256(self.archive.read_bytes()).hexdigest() + "  AnyPS5-macOS.zip\n")

    def install(self):
        return subprocess.run(["sh", str(INSTALLER), "--archive", str(self.archive), "--checksums", str(self.sums),
                               "--destination", str(self.destination)], env=self.environment,
                              capture_output=True, text=True, timeout=20)

    def install_old(self):
        old = self.make_app("old")
        shutil.copytree(old, self.target)
        return self.snapshot()

    def snapshot(self):
        return {str(path.relative_to(self.target)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.target.rglob("*") if path.is_file()}

    def test_verified_archive_installs_and_its_real_macho_executes(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        execution = subprocess.run([str(self.target / "Contents/MacOS/AnyPS5")], capture_output=True, text=True, timeout=5)
        self.assertEqual((execution.returncode, execution.stdout, execution.stderr), (0, "new\n", ""))
        self.assertEqual((self.target / "Contents/Resources/marker.txt").read_text(), "new")
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(self.target)], check=True, capture_output=True, timeout=10)
        self.assertEqual(list(self.destination.glob(".AnyPS5-install*")), [])
        self.assertNotIn("unexpected-command", result.stderr)

    def test_final_rename_failure_restores_the_real_signed_previous_app(self):
        original = self.install_old()
        wrapper = self.bin / "ditto"
        wrapper.write_text('#!/bin/sh\nset -eu\n/usr/bin/ditto "$@"\nfor destination do :; done\n'
                           'case "$destination" in */.AnyPS5-install.*/AnyPS5.app) /usr/bin/chflags uchg "$destination" ;; esac\n')
        wrapper.chmod(0o755)
        result = self.install()
        self.assertNotEqual(result.returncode, 0, "an immutable incoming directory must make the final rename fail")
        self.assertIn("restored the previous app", result.stderr)
        self.assertEqual(self.snapshot(), original, "failed placement must restore every previous signed app byte")
        execution = subprocess.run([str(self.target / "Contents/MacOS/AnyPS5")], capture_output=True, text=True, timeout=5)
        self.assertEqual((execution.returncode, execution.stdout, execution.stderr), (0, "old\n", ""))
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(self.target)], check=True, capture_output=True, timeout=10)
        self.assertFalse((self.destination / ".AnyPS5-install.lock").exists())

    def test_rehashed_archive_with_invalid_code_signature_cannot_replace_the_app(self):
        original = self.install_old()
        (self.source / "Contents/Resources/marker.txt").write_text("changed after signing")
        self.pack()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("code-signature integrity check failed", result.stderr)
        self.assertEqual(self.snapshot(), original)

    def test_browser_archive_quarantine_is_preserved_on_the_installed_app(self):
        quarantine = "0083;12345678;AnyPS5Fixture;B10A0C5D-69A2-4681-864F-46C1ED8AE391"
        subprocess.run(["xattr", "-w", "com.apple.quarantine", quarantine, str(self.archive)], check=True, timeout=5)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = subprocess.check_output(["xattr", "-p", "com.apple.quarantine", str(self.target)], text=True, timeout=5).strip()
        self.assertEqual(actual, quarantine)
        self.assertNotIn("unexpected-command", result.stderr)


if __name__ == "__main__":
    unittest.main()
