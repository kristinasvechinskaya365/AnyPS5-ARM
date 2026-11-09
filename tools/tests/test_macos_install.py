import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile


INSTALLER = Path(__file__).resolve().parents[1] / "macos/install.sh"


class MacOSInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="anyps5 install ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "tmp").mkdir()
        self.destination = self.root / "User Applications"
        self.destination.mkdir()
        self.target = self.destination / "AnyPS5.app"
        self.target.mkdir()
        (self.target / "old.txt").write_bytes(b"previous application bytes")
        self.source = self.root / "source/AnyPS5.app"
        (self.source / "Contents/MacOS").mkdir(parents=True)
        self.binary = self.source / "Contents/MacOS/AnyPS5"
        self.binary.write_bytes(b"new executable bytes")
        self.binary.chmod(0o755)
        self.plist = {"CFBundleIdentifier": "org.anyps5.launcher", "CFBundleExecutable": "AnyPS5",
                      "CFBundlePackageType": "APPL", "LSMinimumSystemVersion": "14.0"}
        self.archive = self.root / "download with spaces.zip"
        self.sums = self.root / "SHA256SUMS"
        self.controls = {"destination": str(self.destination), "archive": str(self.archive),
                         "release_url": "https://github.com/test-owner/test-repo/releases/tag/v1.2.3"}
        self.pack()
        self.make_commands()

    def pack(self, extra=None):
        (self.source / "Contents/Info.plist").write_bytes(plistlib.dumps(self.plist))
        with zipfile.ZipFile(self.archive, "w") as output:
            for path in sorted(self.source.rglob("*")):
                name = str(path.relative_to(self.source.parent))
                if path.is_symlink():
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    output.writestr(info, os.readlink(path))
                else:
                    output.write(path, name)
            for name, data in (extra or {}).items():
                output.writestr(name, data)
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.sums.write_text(self.digest + "  AnyPS5-macOS.zip\n")

    def make_commands(self):
        helper = self.bin / "command.py"
        helper.write_text("#!" + sys.executable + "\n" + textwrap.dedent("""\
            import json
            import os
            from pathlib import Path
            import plistlib
            import shutil
            import signal
            import stat
            import subprocess
            import sys
            import tempfile
            import zipfile

            root = Path(os.environ["INSTALL_TEST_ROOT"])
            control = json.loads((root / "control.json").read_text())
            name = Path(sys.argv[0]).name
            args = sys.argv[1:]
            with (root / "calls.jsonl").open("a") as stream:
                stream.write(json.dumps([name, *args]) + "\\n")
            def mapped(value):
                for prefix in ("/Applications", str(Path.home() / "Applications")):
                    if value == prefix or value.startswith(prefix + "/"):
                        return control["destination"] + value[len(prefix):]
                return value
            def extract(source, destination):
                destination.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(source) as archive:
                    for item in archive.infolist():
                        path = destination / item.filename
                        if not path.resolve().is_relative_to(destination.resolve()):
                            sys.exit(1)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if item.is_dir():
                            path.mkdir(exist_ok=True)
                        elif stat.S_ISLNK(item.external_attr >> 16):
                            path.symlink_to(archive.read(item).decode())
                        else:
                            path.write_bytes(archive.read(item))
                            path.chmod((item.external_attr >> 16) & 0o777)
            if name == "uname":
                print("Darwin" if args == ["-s"] else control.get("machine", "arm64"))
            elif name == "sw_vers":
                print(control.get("system_version", "14.7.0"))
            elif name == "mktemp":
                template = next((arg for arg in args if not arg.startswith("-")), None)
                parent = Path(mapped(template)).parent if template else root / "tmp"
                prefix = Path(template).name.split("XXXXXX")[0] if template else "scratch-"
                print(tempfile.mkdtemp(dir=parent, prefix=prefix))
            elif name == "curl":
                url = next(arg for arg in args if arg.startswith("https://"))
                if url.endswith("/releases/latest"):
                    print(control["release_url"])
                else:
                    output = Path(args[args.index("-o") + 1])
                    if url.endswith("/SHA256SUMS"):
                        if control.get("missing_sums"):
                            sys.exit(22)
                        output.write_text(control.get("sums", (root / "SHA256SUMS").read_text()))
                    else:
                        shutil.copyfile(control["archive"], output)
            elif name == "tar":
                if args == ["--version"]:
                    print("bsdtar fixture")
                elif args[0] == "-tf":
                    with zipfile.ZipFile(args[1]) as archive:
                        print("\\n".join(archive.namelist()))
                else:
                    extract(Path(args[1]), Path(args[args.index("-C") + 1]))
            elif name == "ditto":
                if args[:2] == ["-x", "-k"]:
                    extract(Path(args[2]), Path(args[3]))
                else:
                    destination = Path(mapped(args[-1]))
                    if control.get("copy_failure"):
                        destination.mkdir(parents=True, exist_ok=True)
                        (destination / "partial.txt").write_text("incomplete copy")
                        sys.exit(1)
                    shutil.copytree(args[-2], destination, symlinks=True, dirs_exist_ok=True)
            elif name == "plutil":
                try:
                    print(plistlib.loads(Path(args[-1]).read_bytes())[args[1]])
                except (OSError, KeyError, ValueError):
                    sys.exit(1)
            elif name == "file":
                print("Mach-O 64-bit executable " + control.get("binary_arch", "arm64"))
            elif name == "codesign":
                sys.exit(1 if control.get("bad_signature") else 0)
            elif name == "xattr":
                if args[:2] == ["-p", "com.apple.quarantine"]:
                    if not control.get("quarantine"):
                        sys.exit(1)
                    print(control["quarantine"])
            elif name == "arch":
                sys.exit(1 if control.get("missing_rosetta") else 0)
            elif name in ("open", "softwareupdate"):
                pass
            elif name == "mv":
                source = Path(mapped(args[-2]))
                destination = Path(mapped(args[-1]))
                restoring = source.parent.name == "previous"
                placing = ".AnyPS5-install." in str(source) and not restoring
                if placing and control.get("placement_failure") or restoring and control.get("restore_failure"):
                    sys.exit(1)
                if placing and control.get("destination_race"):
                    foreign = destination / "AnyPS5.app"
                    foreign.mkdir()
                    (foreign / "foreign.txt").write_text("concurrent destination")
                code = subprocess.call(["/bin/mv", *args[:-2], str(source), str(destination)])
                if code == 0 and destination.name == "previous" and control.get("interrupt_after_backup"):
                    os.kill(os.getppid(), signal.SIGTERM)
                sys.exit(code)
            elif name in ("rm", "mkdir", "rmdir"):
                sys.exit(subprocess.call(["/bin/" + name, *(mapped(arg) for arg in args)]))
            else:
                sys.exit(99)
            """), encoding="utf-8")
        helper.chmod(0o755)
        for name in ("uname", "sw_vers", "mktemp", "curl", "tar", "ditto", "plutil", "file", "codesign", "xattr",
                     "arch", "open", "softwareupdate", "mv", "rm", "mkdir", "rmdir"):
            (self.bin / name).symlink_to(helper.name)

    def run_install(self, arguments=None):
        (self.root / "control.json").write_text(json.dumps(self.controls))
        environment = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "INSTALL_TEST_ROOT": str(self.root)}
        environment.pop("ANYPS5_REPOSITORY", None)
        if arguments is None:
            arguments = ["--archive", str(self.archive), "--checksums", str(self.sums)]
        result = subprocess.run(["sh", str(INSTALLER), *arguments, "--destination", str(self.destination)],
                                env=environment, text=True, capture_output=True, timeout=20)
        calls = self.root / "calls.jsonl"
        self.calls = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return result

    def assert_previous(self):
        self.assertTrue((self.target / "old.txt").is_file(), "failed installation must preserve the previous app bytes")
        self.assertEqual((self.target / "old.txt").read_bytes(), b"previous application bytes")
        self.assertFalse((self.destination / ".AnyPS5-install.lock").exists())

    def test_local_archive_installs_without_network_or_automatic_open(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / "Contents/MacOS/AnyPS5").read_bytes(), b"new executable bytes")
        self.assertFalse((self.target / "old.txt").exists())
        self.assertFalse(any(call[0] in ("curl", "open", "softwareupdate") for call in self.calls))
        self.assertEqual(list(self.destination.glob(".AnyPS5-install*")), [])

    def test_latest_is_resolved_once_then_both_downloads_use_its_tag(self):
        result = self.run_install(["--repository", "test-owner/test-repo"])
        self.assertEqual(result.returncode, 0, result.stderr)
        urls = [next(arg for arg in call if arg.startswith("https://")) for call in self.calls if call[0] == "curl"]
        self.assertEqual(urls, ["https://github.com/test-owner/test-repo/releases/latest",
                                "https://github.com/test-owner/test-repo/releases/download/v1.2.3/SHA256SUMS",
                                "https://github.com/test-owner/test-repo/releases/download/v1.2.3/AnyPS5-macOS.zip"])

    def test_explicit_release_does_not_query_latest(self):
        result = self.run_install(["--repository", "test-owner/test-repo", "--version", "v2.3.4-arm"])
        self.assertEqual(result.returncode, 0, result.stderr)
        urls = [next(arg for arg in call if arg.startswith("https://")) for call in self.calls if call[0] == "curl"]
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("/releases/download/v2.3.4-arm/" in url for url in urls))

    def test_missing_checksums_refuse_installation(self):
        self.controls["missing_sums"] = True
        result = self.run_install(["--repository", "test-owner/test-repo", "--version", "v1.2.3"])
        self.assertNotEqual(result.returncode, 0, "missing SHA256SUMS must refuse installation")
        self.assert_previous()

    def test_missing_archive_checksum_refuses_installation(self):
        self.controls["sums"] = self.digest + "  another.zip\n"
        result = self.run_install(["--repository", "test-owner/test-repo", "--version", "v1.2.3"])
        self.assertNotEqual(result.returncode, 0, "SHA256SUMS without the app entry must refuse installation")
        self.assert_previous()

    def test_duplicate_or_invalid_checksum_entries_are_rejected(self):
        for contents in (self.sums.read_text() * 2, "z" * 64 + "  AnyPS5-macOS.zip\n", "0" * 64 + "  AnyPS5-macOS.zip\n"):
            with self.subTest(contents=contents):
                self.sums.write_text(contents)
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assert_previous()

    def test_explicit_uppercase_checksum_and_optional_open_work(self):
        result = self.run_install(["--archive", str(self.archive), "--sha256", self.digest.upper(), "--open"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in self.calls if call[0] == "open"], [["open", str(self.target)]])

    def test_copy_failure_preserves_previous_app(self):
        self.controls["copy_failure"] = True
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assert_previous()
        self.assertEqual(list(self.destination.glob(".AnyPS5-install*")), [])

    def test_final_placement_failure_restores_previous_app(self):
        self.controls["placement_failure"] = True
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assert_previous()
        self.assertIn("restored the previous app", result.stderr)

    def test_interruption_after_backup_restores_previous_app(self):
        self.controls["interrupt_after_backup"] = True
        result = self.run_install()
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assert_previous()

    def test_rollback_failure_retains_recovery_bytes(self):
        self.controls.update(placement_failure=True, restore_failure=True)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        recovered = list(self.destination.glob(".AnyPS5-install.*/previous/AnyPS5.app/old.txt"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), b"previous application bytes")
        self.assertIn("previous app is preserved at", result.stderr)
        self.assertFalse((self.destination / ".AnyPS5-install.lock").exists())

    def test_a_concurrent_destination_is_not_deleted_during_rollback(self):
        self.controls["destination_race"] = True
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.target / "foreign.txt").read_text(), "concurrent destination")
        recovered = list(self.destination.glob(".AnyPS5-install.*/previous/AnyPS5.app/old.txt"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), b"previous application bytes")

    def test_invalid_app_identity_signature_and_architecture_are_refused(self):
        for option, value in (("bad_signature", True), ("binary_arch", "x86_64")):
            with self.subTest(option=option):
                self.controls[option] = value
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assert_previous()
                self.controls.pop(option)
        self.plist["CFBundleIdentifier"] = "org.another.application"
        self.pack()
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assert_previous()

    def test_archive_paths_and_external_links_are_refused(self):
        self.pack({"../outside.txt": "do not extract"})
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assert_previous()
        (self.source / "escape").symlink_to(self.root / "outside")
        self.pack()
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assert_previous()

    def test_newer_or_invalid_minimum_macos_version_preserves_the_app(self):
        for version in ("14.7.1", "15.0", "14..0", "unknown"):
            with self.subTest(version=version):
                self.plist["LSMinimumSystemVersion"] = version
                self.pack()
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assert_previous()

    def test_internal_framework_links_are_preserved(self):
        framework = self.source / "Contents/Frameworks/Example.framework"
        (framework / "Versions/A").mkdir(parents=True)
        (framework / "Versions/A/Example").write_bytes(b"framework bytes")
        (framework / "Versions/Current").symlink_to("A")
        (framework / "Example").symlink_to("Versions/Current/Example")
        self.pack()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        copied = self.target / "Contents/Frameworks/Example.framework"
        self.assertTrue((copied / "Example").is_symlink())
        self.assertEqual((copied / "Example").read_bytes(), b"framework bytes")

    def test_directory_link_chains_cannot_escape_the_bundle(self):
        resources = self.source / "Contents/Resources"
        resources.mkdir()
        (resources / "alias").symlink_to(".")
        (resources / "escape").symlink_to("alias/../../..")
        self.pack()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0, "directory symlink chains must remain inside the app bundle")
        self.assertIn("an app link escapes the bundle", result.stderr)
        self.assert_previous()

    def test_existing_destination_links_and_other_install_locks_are_refused(self):
        lock = self.destination / ".AnyPS5-install.lock"
        lock.mkdir()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(lock.is_dir())
        lock.rmdir()
        shutil.rmtree(self.target)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        self.target.symlink_to(outside)
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertEqual((outside / "keep.txt").read_text(), "keep")

    def test_quarantine_is_preserved_and_rosetta_changes_are_not_automatic(self):
        self.controls.update(quarantine="0083;12345678;Browser;fixture", missing_rosetta=True)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        writes = [call for call in self.calls if call[:3] == ["xattr", "-w", "com.apple.quarantine"]]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][3], self.controls["quarantine"])
        self.assertFalse(any(call[0] == "xattr" and any(arg in ("-d", "-dr", "-c") for arg in call) for call in self.calls))
        self.assertFalse(any(call[0] == "softwareupdate" for call in self.calls))
        self.assertIn("softwareupdate --install-rosetta", result.stdout)

    def test_invalid_sources_and_unverified_local_archives_are_refused(self):
        for args in (["--repository", "owner/repo/extra"], ["--repository", "owner/repo", "--version", "../tag"],
                     ["--archive", str(self.archive)], ["--archive", str(self.archive), "--sha256", "bad"]):
            with self.subTest(args=args):
                self.assertNotEqual(self.run_install(args).returncode, 0)
                self.assert_previous()

    def test_help_is_available_without_macos_tools(self):
        result = subprocess.run(["sh", str(INSTALLER), "--help"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--archive", result.stdout)
        self.assertIn("kristinasvechinskaya365/AnyPS5-ARM", result.stdout)


if __name__ == "__main__":
    unittest.main()
