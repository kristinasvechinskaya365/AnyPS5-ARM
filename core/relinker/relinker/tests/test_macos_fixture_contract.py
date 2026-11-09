import contextlib
import ctypes
import io
import mmap
import os
from pathlib import Path
import platform
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "macos"))

import argv_fixture
import guesttools
from nid import nid
import run_macos_fixtures as fixtures
from test_macos_output import MachO

RELINKER = None
NATIVE_X86_64 = sys.platform.startswith("linux") and platform.machine() in ("x86_64", "AMD64")


def execute_argv(arguments):
    image = argv_fixture.fixture()
    with mmap.mmap(-1, len(image), prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC) as memory:
        memory.write(image)
        address = ctypes.addressof(ctypes.c_char.from_buffer(memory))
        exit_address = ctypes.cast(ctypes.CDLL(None)._exit, ctypes.c_void_p).value
        struct.pack_into("<Q", memory, 0x5008, exit_address)
        strings = [ctypes.create_string_buffer(value.encode()) for value in ["fixture", *arguments]]
        block = (ctypes.c_uint64 * (len(strings) + 2))(len(strings), *(ctypes.addressof(value) for value in strings), 0)
        ctypes.CFUNCTYPE(None, ctypes.c_void_p)(address + 0x4000)(block)
    raise RuntimeError("the guest exit import returned")


class ArgvFixtureTests(unittest.TestCase):
    @unittest.skipUnless(NATIVE_X86_64, "raw x86-64 fixture execution requires Linux x86-64")
    def test_guest_terminates_normally_for_valid_and_invalid_arguments(self):
        for arguments, expected in ((("Z",), 0), ((), 1), (("Z", "extra"), 1), (("Y",), 1), (("ZZ",), 1), (("",), 1)):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--execute-argv", *arguments],
                                        capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, expected, result.stderr)

    @unittest.skipUnless(NATIVE_X86_64, "raw x86-64 fixture execution requires Linux x86-64")
    def test_argument_block_keeps_pointer_and_terminator_checks(self):
        code = argv_fixture.check_arguments()
        with mmap.mmap(-1, len(code), prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC) as memory:
            memory.write(code)
            check = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(ctypes.addressof(ctypes.c_char.from_buffer(memory)))
            name = ctypes.create_string_buffer(b"fixture")
            argument = ctypes.create_string_buffer(b"Z")
            valid = [2, ctypes.addressof(name), ctypes.addressof(argument), 0]
            self.assertEqual(check((ctypes.c_uint64 * 4)(*valid)), 0)
            for index, replacement in ((0, 0), (0, 3), (1, 0), (2, 0), (3, ctypes.addressof(name))):
                block = valid.copy()
                block[index] = replacement
                with self.subTest(index=index, replacement=replacement):
                    self.assertEqual(check((ctypes.c_uint64 * 4)(*block)), 1)

    def test_macos_relink_uses_the_exit_import(self):
        self.assertIsNotNone(RELINKER, "pass the built relinker as the first argument")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source, output = directory / "argv.elf", directory / "argv"
            source.write_bytes(argv_fixture.fixture())
            result = subprocess.run([str(RELINKER), "--macos", "--skip-sce-module", str(source), str(output)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = output.read_bytes()
            macho = MachO(data)
            self.assertEqual(macho.cputype, 0x01000007)
            self.assertEqual(macho.filetype, 2)
            self.assertIn("@rpath/libc.prx", macho.dylibs)
            self.assertIn("@rpath/libkernel.prx", macho.dylibs)
            self.assertIn(("_" + nid("exit")).encode() + b"\0", data)


class FixtureRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.libraries = self.root / "libraries"
        self.libraries.mkdir()
        self.work = self.root / "work"
        self.output = io.StringIO()

    def run_case(self, name, *options):
        with patch.object(fixtures.sys, "platform", "darwin"), contextlib.redirect_stdout(self.output):
            return fixtures.main(["unused-relinker", str(self.libraries), str(self.work), "--case", name, *options])

    def test_missing_runtime_for_required_case_fails(self):
        self.assertEqual(self.run_case("argv"), 1)
        self.assertIn("FAIL argv: patched runtime libraries missing: libc.prx, libkernel.prx", self.output.getvalue())
        self.assertIn("0 passed, 1 failed, 0 skipped", self.output.getvalue())

    def test_missing_compiler_is_an_explicit_skip(self):
        with patch.object(guesttools, "missing_tools", return_value=["fixture/clang++"]):
            self.assertEqual(self.run_case("exception"), fixtures.SKIP)
        self.assertIn("SKIP exception: guest LLVM toolchain missing: fixture/clang++", self.output.getvalue())
        self.assertIn("0 passed, 0 failed, 1 skipped", self.output.getvalue())

    def test_fault_reference_needs_opt_in(self):
        with patch.dict(os.environ, {"ANYPS5_MACOS_FAULT_REFERENCE": "0"}):
            self.assertEqual(self.run_case("intel-unconverted"), fixtures.SKIP)
        self.assertIn("SKIP intel-unconverted: intentional SIGILL reference is opt-in", self.output.getvalue())
        self.output.seek(0)
        self.output.truncate()
        with patch.object(guesttools, "missing_tools", return_value=[]):
            self.assertEqual(self.run_case("intel-unconverted", "--include-faults"), 1)
        self.assertIn("patched runtime libraries missing", self.output.getvalue())

    def test_non_macos_execution_is_an_explicit_skip(self):
        with patch.object(fixtures.sys, "platform", "linux"), contextlib.redirect_stdout(self.output):
            result = fixtures.main(["unused-relinker", str(self.libraries), "--case", "argv"])
        self.assertEqual(result, fixtures.SKIP)
        self.assertIn("SKIP argv: execution requires macOS", self.output.getvalue())

    def test_compiler_detection_includes_the_c_driver(self):
        for tool in ("clang++", "llvm-nm", "llvm-objcopy"):
            path = self.root / tool
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        with patch.object(guesttools, "LLVM", str(self.root)), patch.object(guesttools.shutil, "which", return_value="ld.lld"):
            self.assertEqual(guesttools.missing_tools(), [str(self.root / "clang")])

    def test_stale_runtime_check_ignores_unrelated_libraries(self):
        unpatched = self.libraries / "unpatched"
        unpatched.mkdir()
        (unpatched / "libSceVideoOut.prx").write_bytes(b"unrelated")
        self.assertEqual(fixtures.stale_libraries(self.libraries), [])
        (unpatched / "libc.prx").write_bytes(b"runtime")
        self.assertEqual(fixtures.stale_libraries(self.libraries), ["libc.prx"])

    @unittest.skipIf(os.name == "nt", "fixture process groups require a POSIX host")
    def test_process_exit_and_logs_are_preserved(self):
        result = fixtures.run_command([sys.executable, "-c", "import sys; print('output'); print('error', file=sys.stderr); sys.exit(7)"],
                                      self.root, "child", 3)
        self.assertEqual(result, 7)
        self.assertEqual((self.root / "child.stdout.log").read_text(), "output\n")
        self.assertEqual((self.root / "child.stderr.log").read_text(), "error\n")

    @unittest.skipIf(os.name == "nt", "fixture process groups require a POSIX host")
    def test_timeout_fails_and_reaps_the_child(self):
        command = [sys.executable, "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(30)"]
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            fixtures.run_command(command, self.root, "timeout", 0.5)
        pid = int((self.root / "timeout.stdout.log").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_success_cases_do_not_expect_a_signal(self):
        cases = fixtures.fixture_cases()
        for name, case in cases.items():
            for _, expected in case.runs:
                with self.subTest(name=name):
                    self.assertEqual(expected < 0, case.fault)
        self.assertEqual(cases["intel-unconverted"].runs, (((), -signal.SIGILL),))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--execute-argv"]:
        execute_argv(sys.argv[2:])
    if len(sys.argv) < 2:
        raise SystemExit("usage: test_macos_fixture_contract.py <relinker> [unittest options]")
    RELINKER = Path(sys.argv.pop(1)).resolve()
    unittest.main()
