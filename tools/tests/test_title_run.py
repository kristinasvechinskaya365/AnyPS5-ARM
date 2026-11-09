import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import title_run


class TitlesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, data):
        path = self.root / "titles.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_repository_file_is_valid(self):
        titles = title_run.load_titles(title_run.ROOT / "tools/titles.json")
        self.assertEqual(titles["PPSA85300"], {"relink": ["--registry"]})

    def test_unknown_field_is_refused(self):
        with self.assertRaisesRegex(ValueError, "PPSA00001: unknown field rlink"):
            title_run.load_titles(self.write({"PPSA00001": {"rlink": []}}))

    def test_wrong_type_is_refused(self):
        with self.assertRaisesRegex(ValueError, "PPSA00001: seconds must be int"):
            title_run.load_titles(self.write({"PPSA00001": {"seconds": "60"}}))

    def test_non_string_items_are_refused(self):
        with self.assertRaisesRegex(ValueError, "PPSA00001: env must hold strings"):
            title_run.load_titles(self.write({"PPSA00001": {"env": {"APS5_X": 1}}}))
        with self.assertRaisesRegex(ValueError, "PPSA00001: relink must hold strings"):
            title_run.load_titles(self.write({"PPSA00001": {"relink": [1]}}))

    def test_pad_timeline_is_checked(self):
        events = [{"at_ms": 0, "hold_ms": 200, "buttons": ["cross"]}, {"at_ms": 400, "hold_ms": 300, "buttons": ["right", "cross"]}]
        titles = title_run.load_titles(self.write({"PPSA00001": {"pad": {"after_s": 3, "repeat": 2, "events": events}}}))
        self.assertEqual(titles["PPSA00001"]["pad"]["repeat"], 2)
        for pad in ({"after_s": 3}, {"events": events, "delay": 3}, {"events": [{"at_ms": 0, "hold_ms": 200, "buttons": ["jump"]}]},
                    {"events": events, "after_s": 3601}, {"events": events, "repeat": 0}):
            with self.assertRaises(ValueError):
                title_run.load_titles(self.write({"PPSA00001": {"pad": pad}}))

    def test_bad_id_is_refused(self):
        with self.assertRaisesRegex(ValueError, "NOT-AN-ID is not a title ID"):
            title_run.load_titles(self.write({"NOT-AN-ID": {}}))

    def test_name_is_refused(self):
        with self.assertRaisesRegex(ValueError, "PPSA00001: unknown field name"):
            title_run.load_titles(self.write({"PPSA00001": {"name": "Light"}}))

    def test_default_entry(self):
        self.assertEqual(title_run.relink_args({}, "windows"), ["--windows"])
        self.assertEqual(title_run.relink_args({}, "linux"), [])
        self.assertEqual(title_run.relink_args({}, "macos"), ["--macos", "--to-rosetta"])

    def test_entry_on_windows_and_linux(self):
        entry = {"relink": ["--registry"], "fixed_base": "0x400000", "executables": ["picker/picker.bin"]}
        self.assertEqual(title_run.relink_args(entry, "windows"),
                         ["--windows", "--registry", "--windows-fixed-base", "0x400000", "--executable", "picker/picker.bin"])
        self.assertEqual(title_run.relink_args(entry, "linux"),
                         ["--registry", "--linux-fixed-base", "0x400000", "--executable", "picker/picker.bin"])

    def test_fixed_base_on_macos_is_refused(self):
        with self.assertRaisesRegex(ValueError, "fixed_base is not supported on macos"):
            title_run.relink_args({"fixed_base": "0x400000"}, "macos")


def bmp(path, width, height, color):
    rows = b"".join(bytes(channel for x in range(width) for channel in (*color(x, y), 255)) for y in range(height))
    header = struct.pack("<2sIHHI", b"BM", 54 + len(rows), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, len(rows), 0, 0, 0, 0)
    path.write_bytes(header + info + rows)
    return path


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_first_error(self):
        text = "Transferring control\n  what():  sysctl_nid_postfix: unsupported MIB 1.1\nFATAL: abort()\n"
        self.assertEqual(title_run.first_error(text), "what():  sysctl_nid_postfix: unsupported MIB 1.1")
        self.assertEqual(title_run.first_error("radv/ps5: the GPU has not reached submission 12 (at 11) in 10 s\nVkResult is ERROR_DEVICE_LOST\n"), "radv/ps5: the GPU has not reached submission 12 (at 11) in 10 s")
        self.assertEqual(title_run.first_error("Converting\nFAIL: Forbidden syscall instruction at code offset 0x1\nInput: x\n"), "FAIL: Forbidden syscall instruction at code offset 0x1")
        self.assertEqual(title_run.first_error("all fine\n"), "")

    def test_warning_counts(self):
        text = "[gpu] storage image 0x1 destroyed with GPU results pending\n" * 2
        self.assertEqual(title_run.warning_counts(text)["destroyed with GPU results pending"], 2)

    def test_read_summary(self):
        path = self.root / "aps5-summary.jsonl"
        lines = [{"presents": 300, "window": {"ms": 10000}, "gpu": "A", "driver": "1", "os": "W"},
                 {"presents": 200, "window": {"ms": 10000}, "gpu": "B", "driver": "2", "os": "W"}]
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        self.assertEqual(title_run.read_summary(path), {"presents": 500, "fps": 25.0, "gpu": "B", "driver": "2", "os": "W"})
        path.write_text(path.read_text(encoding="utf-8") + '{"presents": 3', encoding="utf-8")
        self.assertEqual(title_run.read_summary(path)["presents"], 500)
        path.write_text(path.read_text(encoding="utf-8") + "\n[1, 2]\n", encoding="utf-8")
        self.assertEqual(title_run.read_summary(path)["presents"], 500)
        self.assertEqual(title_run.read_summary(self.root / "missing.jsonl")["presents"], 0)

    def test_frame_is_uniform(self):
        black = bmp(self.root / "black.bmp", 8, 4, lambda x, y: (0, 0, 0))
        noisy = bmp(self.root / "noisy.bmp", 8, 4, lambda x, y: (3, 2, 1) if x < 7 else (200, 200, 200))
        nearly = bmp(self.root / "nearly.bmp", 8, 4, lambda x, y: (x, 0, 0))
        self.assertTrue(title_run.frame_is_uniform(black))
        self.assertFalse(title_run.frame_is_uniform(noisy))
        self.assertTrue(title_run.frame_is_uniform(nearly))
        truncated = self.root / "truncated.bmp"
        truncated.write_bytes(black.read_bytes()[:40])
        self.assertIsNone(title_run.frame_is_uniform(truncated))
        cut = self.root / "cut.bmp"
        cut.write_bytes(noisy.read_bytes()[:-20])
        self.assertIsNone(title_run.frame_is_uniform(cut))

    def test_classify_reads_the_last_frame(self):
        uniform = bmp(self.root / "uniform.bmp", 8, 4, lambda x, y: (9, 9, 9))
        varied = bmp(self.root / "varied.bmp", 8, 4, lambda x, y: (x * 30, y * 60, 0))
        truncated = self.root / "truncated.bmp"
        truncated.write_bytes(varied.read_bytes()[:-20])
        for frame, outcome in ((uniform, "blank"), (varied, "rendering"), (truncated, "frame-unreadable")):
            self.assertEqual(title_run.classify(True, None, 5.0, True, title_run.frame_is_uniform(frame)), outcome, frame.name)

    def test_classify(self):
        cases = [
            ((False, None, None, False, None), "relink-failed"),
            ((True, 3221226505, None, False, None), "crashed"),
            ((True, 0, None, False, None), "exited"),
            ((True, None, None, False, None), "stalled"),
            ((True, None, 25.0, True, False), "stalled"),
            ((True, None, 5.0, False, None), "stalled"),
            ((True, None, 5.0, True, True), "blank"),
            ((True, None, 5.0, True, False), "rendering"),
            ((True, None, 5.0, True, None), "frame-unreadable"),
        ]
        for arguments, outcome in cases:
            self.assertEqual(title_run.classify(*arguments), outcome, arguments)


def run(outcome, cache="cold", fps=0.0, error=""):
    return {"outcome": outcome, "cache": cache, "fps": fps, "first_error": error}


def summary(titles):
    return {"build": {"commit": "abc1234", "dirty": False, "note": "main + #6814"},
            "machine": {"os": "Windows 11", "gpu": "Radeon 860M", "driver": "32.0.13058.8001", "cpu": "Ryzen AI 7 350"},
            "runs": 2, "seconds": 60, "titles": titles}


class ReportTests(unittest.TestCase):
    def test_cell(self):
        runs = [run("crashed"), run("rendering", "warm"), run("crashed", "warm")]
        self.assertEqual(title_run.cell(runs, "cold"), "crashed")
        self.assertEqual(title_run.cell(runs, "warm"), "crashed 1/2, rendering 1/2")
        self.assertEqual(title_run.cell([run("rendering", "warm"), run("rendering", "warm")], "warm"), "rendering 2/2")
        self.assertEqual(title_run.cell([run("rendering")], "warm"), "-")

    def test_worst_and_compare(self):
        now = summary({"PPSA85300": {"name": "Light", "version": "0.4", "runs": [run("crashed"), run("crashed", "warm")]},
                       "PPSA97358": {"name": "XPSemu", "version": "A2.1", "runs": [run("rendering"), run("rendering", "warm")]}})
        before = summary({"PPSA85300": {"name": "Light", "version": "0.4", "runs": [run("rendering"), run("rendering", "warm")]},
                          "PPSA97358": {"name": "XPSemu", "version": "A2.1", "runs": [run("stalled"), run("stalled", "warm")]}})
        self.assertEqual(title_run.worst(now["titles"]["PPSA85300"]["runs"]), "crashed")
        self.assertEqual(title_run.compare(now, before),
                         ([("PPSA85300", "rendering", "crashed")], [("PPSA97358", "stalled", "rendering")]))

    def test_render_markdown(self):
        now = summary({"PPSA85300": {"name": "Light", "version": "0.4",
                                     "runs": [run("crashed", error="what(): unsupported MIB 1.1 | x"), run("rendering", "warm", 31.0)]}})
        before = summary({"PPSA85300": {"name": "Light", "version": "0.4", "runs": [run("rendering"), run("rendering", "warm")]}})
        self.assertEqual(title_run.render_markdown(now, before), "\n".join([
            "AnyPS5 `abc1234`, main + #6814. Windows 11, Radeon 860M, driver 32.0.13058.8001, CPU Ryzen AI 7 350. 2 runs per title, 60 s each.",
            "",
            "Regressions: Light (PPSA85300) rendering -> crashed",
            "",
            "| Title | ID | Version | Cold | Warm | FPS | First error | Before |",
            "|---|---|---|---|---|---|---|---|",
            "| Light | PPSA85300 | 0.4 | crashed | rendering | 31.0 | what(): unsupported MIB 1.1 \\| x | rendering |",
            "",
        ]))
        self.assertNotIn("Before", title_run.render_markdown(now))
        now["machine"] = {"os": "", "gpu": "", "driver": "", "cpu": "Ryzen AI 7 350"}
        self.assertTrue(title_run.render_markdown(now).startswith("AnyPS5 `abc1234`, main + #6814. CPU Ryzen AI 7 350. 2 runs"))

    def test_render_markdown_skips_runs_without_an_error(self):
        now = summary({"PPSA01325": {"name": "Playroom", "version": "01.905.000",
                                     "runs": [run("stalled"), run("crashed", "warm", error="vkQueueSubmit recorder: Vulkan result -4")]}})
        self.assertIn("| stalled | crashed | 0.0 | vkQueueSubmit recorder: Vulkan result -4 |", title_run.render_markdown(now))

    def test_render_markdown_lists_missing_titles(self):
        now = summary({"PPSA85300": {"name": "Light", "version": "0.4", "runs": [run("rendering"), run("rendering", "warm")]}})
        before = summary({"PPSA85300": {"name": "Light", "version": "0.4", "runs": [run("rendering"), run("rendering", "warm")]},
                          "PPSA97358": {"name": "XPSemu", "version": "A2.1", "runs": [run("stalled"), run("stalled", "warm")]}})
        self.assertIn("Missing: XPSemu (PPSA97358)", title_run.render_markdown(now, before))

    def test_macos_report_separates_the_toolkit_from_runner_source(self):
        now = summary({"PPSA00001": {"name": "Game", "version": "1", "runs": [run("crashed")],
                                     "installation": {"toolkitVersion": "engine-2"}}})
        now["macos"] = {"execution_mode": "rosetta-x86_64"}
        report = title_run.render_markdown(now)
        self.assertIn("AnyPS5 macOS toolkit `engine-2`.", report)
        self.assertIn("Engine commit (declared by toolkit manifest): unknown.", report)
        self.assertIn("Runner source `abc1234`", report)
        self.assertNotIn("AnyPS5 `abc1234`", report)
        now["titles"]["PPSA00001"]["installation"]["engineCommit"] = "b" * 40
        self.assertIn("Engine commit (declared by toolkit manifest): " + "b" * 40, title_run.render_markdown(now))


def package(root, title_id, version="01.000.000", name="Game"):
    (root / "sce_sys").mkdir(parents=True)
    (root / "eboot.bin").write_bytes(b"\x7fELF" + bytes(60))
    params = {"titleId": title_id, "contentVersion": version,
              "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": name}}}
    (root / "sce_sys/param.json").write_text(json.dumps(params), encoding="utf-8")
    return root


class PreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_a_running_title_is_left_alone(self):
        package(self.root / "games/PPSA00001", "PPSA00001")
        out = self.root / "out"
        marker = out / "work/PPSA00001/app0/eboot.bin"
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"running")
        held = title_run.lock_work(out / "work/PPSA00001.lock")
        self.addCleanup(held.close)
        self.assertIsNotNone(held)
        with unittest.mock.patch.object(title_run.sys, "platform", "linux"), self.assertRaisesRegex(SystemExit, "already running in .*: PPSA00001"):
            title_run.main(["--games", str(self.root / "games"), "--out", str(out), "PPSA00001"])
        self.assertEqual(marker.read_bytes(), b"running")
        held.close()
        again = title_run.lock_work(out / "work/PPSA00001.lock")
        self.assertIsNotNone(again)
        again.close()

    def test_discover(self):
        package(self.root / "a/src/PPSA00001", "PPSA00001", "01.000.110", "Light")
        package(self.root / "b/PPSA00002", "PPSA00002")
        found = title_run.discover([self.root])
        self.assertEqual(sorted(found), ["PPSA00001", "PPSA00002"])
        self.assertEqual(found["PPSA00001"]["version"], "01.000.110")
        self.assertEqual(found["PPSA00001"]["name"], "Light")

    def test_source_root_follows_the_build(self):
        build = self.root / "build"
        build.mkdir()
        self.assertEqual(title_run.source_root(build), title_run.ROOT)
        (build / "CMakeCache.txt").write_text("CMAKE_BUILD_TYPE:STRING=Release\nCMAKE_HOME_DIRECTORY:INTERNAL=C:/src/AnyPS5\n", encoding="utf-8")
        self.assertEqual(title_run.source_root(build), Path("C:/src/AnyPS5"))

    def test_discover_refuses_duplicates(self):
        package(self.root / "a/PPSA00001", "PPSA00001")
        package(self.root / "b/PPSA00001", "PPSA00001")
        with self.assertRaisesRegex(ValueError, "PPSA00001 found more than once"):
            title_run.discover([self.root])

    def test_discover_refuses_a_package_without_title_id(self):
        package(self.root / "a/PPSA00001", "PPSA00001")
        param = self.root / "a/PPSA00001/sce_sys/param.json"
        param.write_text(json.dumps({"contentVersion": "01.000.000"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "param.json: no titleId"):
            title_run.discover([self.root])

    def test_discover_refuses_a_title_id_that_is_not_one(self):
        for index, title_id in enumerate(("../../outside", str(self.root / "elsewhere"), "PPSA0001")):
            package(self.root / str(index) / "PPSA00001", title_id)
            with self.assertRaisesRegex(ValueError, "is not a title ID"):
                title_run.discover([self.root / str(index)])

    def test_unwrap_or_copy_keeps_an_elf(self):
        source = self.root / "eboot.bin"
        source.write_bytes(b"\x7fELF" + bytes(60))
        title_run.unwrap_or_copy(source, self.root / "out/eboot.elf")
        self.assertEqual((self.root / "out/eboot.elf").read_bytes(), source.read_bytes())

    def test_link_tree_skips(self):
        source = package(self.root / "pkg", "PPSA00001")
        (source / "sce_module").mkdir()
        (source / "sce_module/libc.prx").write_bytes(b"x")
        title_run.link_tree(source, self.root / "app0", {"sce_module"})
        self.assertTrue((self.root / "app0/sce_sys/param.json").is_file())
        self.assertTrue((self.root / "app0/eboot.bin").is_file())
        self.assertFalse((self.root / "app0/sce_module").exists())


class MacPreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.dump = package(self.root / "dump with spaces", "PPSA00001")
        self.toolkit = self.root / "toolkit"
        self.toolkit.mkdir()
        self.tool = self.root / "anyps5-tool"
        for file in (self.tool, self.toolkit / "relinker"):
            file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            file.chmod(0o755)
        self.work = self.root / "out/work/PPSA00001"

    def installed(self):
        bundle = self.work / ".AnyPS5/Games/PPSA00001-fixture.app"
        launcher = bundle / "Contents/MacOS/run"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        executable = launcher.with_name("eboot")
        executable.write_bytes(struct.pack("<IIIIIIII", 0xFEEDFACF, 0x01000007, 3, 2, 0, 0, 0, 0))
        runtime = bundle / "Contents/Resources/game"
        runtime.mkdir(parents=True, exist_ok=True)
        return {"schemaVersion": 1, "executionMode": "rosetta-x86_64", "titleID": "PPSA00001", "source": str(self.dump.resolve()),
                "toolkit": str(self.toolkit.resolve()), "toolkitVersion": "fixture-1", "bundle": str(bundle.resolve()),
                "launcher": str(launcher.resolve()), "workingDirectory": str(runtime.resolve()),
                "binarySHA256": {"toolkit/relinker": title_run.file_sha256(self.toolkit / "relinker"),
                                 "bundle/Contents/MacOS/eboot": title_run.file_sha256(executable)},
                "leftOut": [{"path": "Media/Plugins/encrypted.sprx", "reason": "encrypted"}]}

    def install(self, command, **kwargs):
        result_file = Path(command[-1])
        self.assertFalse(result_file.exists())
        result_file.write_text(json.dumps(self.installed()), encoding="utf-8")
        kwargs["stdout"].write("raw installer output\n")
        return subprocess.CompletedProcess(command, 0)

    def prepare(self, entry=None):
        return title_run.prepare_macos("PPSA00001", {"package": self.dump}, entry or {}, self.tool, self.toolkit, self.work)

    def test_installer_contract_preserves_relinks_saves_and_source(self):
        result = self.installed()
        kept = self.work / ".AnyPS5/Relinked/kept"
        kept.parent.mkdir()
        kept.write_bytes(b"compiled")
        save = Path(result["workingDirectory"]) / "_sd/save"
        save.parent.mkdir()
        save.write_bytes(b"saved game")
        original = (self.dump / "eboot.bin").read_bytes()
        with unittest.mock.patch.object(title_run.subprocess, "run", side_effect=self.install) as invoked:
            for _ in range(2):
                cache = self.work / "shader-cache/old"
                cache.parent.mkdir()
                cache.write_bytes(b"shader")
                (self.work / "install.json").write_text("stale result", encoding="utf-8")
                self.assertEqual(self.prepare(), result)
                self.assertFalse(cache.parent.exists())
            command = invoked.call_args.args[0]
            self.assertEqual(command, [str(self.tool.resolve()), "install", str(self.dump.resolve()), str(self.work), "--result", str(self.work / "install.json")])
            self.assertEqual(invoked.call_args.kwargs["env"]["ANYPS5_TOOLKIT"], str(self.toolkit.resolve()))
            self.assertEqual(invoked.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kept.read_bytes(), b"compiled")
        self.assertEqual(save.read_bytes(), b"saved game")
        self.assertEqual((self.dump / "eboot.bin").read_bytes(), original)
        self.assertEqual((self.work / "relink.log").read_text(encoding="utf-8"), "raw installer output\n")

    def test_unrepresentable_title_settings_are_refused_before_installing(self):
        entries = [{"relink": ["--registry"]}, {"fixed_base": "0x400000"}, {"fixed_base": ""},
                   {"executables": ["picker.bin"]}, {"env": {"ANYPS5_SHADER_CACHE_DIR": "/shared"}},
                   {"env": {"ANYPS5_NO_SHADER_CACHE": "1"}}, {"env": {"ANYPS5_UPDATE_DIR": "/update"}},
                   {"env": {"ANYPS5_ADDCONT_DIRS": "/dlc"}}]
        with unittest.mock.patch.object(title_run.subprocess, "run") as invoked:
            for entry in entries:
                with self.subTest(entry=entry), self.assertRaises(ValueError):
                    self.prepare(entry)
            invoked.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_output_cannot_overlap_inputs(self):
        original = (self.dump / "eboot.bin").read_bytes()
        for work in (self.dump, self.dump / "runs", self.dump.parent, self.toolkit / "runs"):
            with self.subTest(work=work), self.assertRaisesRegex(ValueError, "overlap"):
                title_run.check_macos_paths(self.dump, self.tool, self.toolkit, work)
        self.assertEqual((self.dump / "eboot.bin").read_bytes(), original)
        self.assertFalse((self.dump / "runs").exists())

    @unittest.skipIf(os.name == "nt", "symlink creation requires a Windows privilege")
    def test_output_and_store_links_cannot_escape_into_inputs(self):
        link = self.root / "linked-output"
        link.symlink_to(self.dump, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "overlap"):
            title_run.check_macos_paths(self.dump, self.tool, self.toolkit, link)
        self.work.mkdir(parents=True)
        (self.work / ".AnyPS5").symlink_to(self.dump, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "store must stay inside"):
            self.prepare()

    @unittest.skipIf(os.name == "nt", "symlink creation requires a Windows privilege")
    def test_mutable_files_cannot_redirect_writes_into_the_dump(self):
        self.work.mkdir(parents=True)
        original = (self.dump / "eboot.bin").read_bytes()
        for name in ("relink.log", "install.json", "pad-input.txt", "shader-cache"):
            path = self.work / name
            path.symlink_to(self.dump / "eboot.bin")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "redirected or shared"):
                self.prepare()
            path.unlink()
        os.link(self.dump / "eboot.bin", self.work / "relink.log")
        with self.assertRaisesRegex(ValueError, "redirected or shared"):
            self.prepare()
        self.assertEqual((self.dump / "eboot.bin").read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "macOS CLI selection requires a POSIX host")
    def test_main_refuses_a_log_link_before_its_first_write(self):
        self.work.mkdir(parents=True)
        (self.work / "relink.log").symlink_to(self.dump / "eboot.bin")
        original = (self.dump / "eboot.bin").read_bytes()
        titles = self.root / "titles.json"
        titles.write_text("{}", encoding="utf-8")
        with unittest.mock.patch.object(title_run.sys, "platform", "darwin"), self.assertRaisesRegex(ValueError, "redirected or shared"):
            title_run.main(["--games", str(self.dump), "--out", str(self.root / "out"), "--titles", str(titles),
                            "--macos-tool", str(self.tool), "--toolkit", str(self.toolkit)])
        self.assertEqual((self.dump / "eboot.bin").read_bytes(), original)

    def test_failed_install_cannot_reuse_a_stale_result(self):
        self.installed()
        (self.work / "install.json").write_text("stale result", encoding="utf-8")

        def fail(command, **kwargs):
            kwargs["stdout"].write("FAIL: conversion failed\n")
            return subprocess.CompletedProcess(command, 2)

        with unittest.mock.patch.object(title_run.subprocess, "run", side_effect=fail):
            self.assertIsNone(self.prepare())
        self.assertFalse((self.work / "install.json").exists())
        self.assertEqual((self.work / "relink.log").read_text(encoding="utf-8"), "FAIL: conversion failed\n")

    def test_result_rejects_mismatched_or_unsafe_installations(self):
        original = self.installed()
        cases = [{}, {"schemaVersion": 2}, {"schemaVersion": True}, {"titleID": "PPSA00002"}, {"executionMode": "native-arm64"},
                 {"source": str(self.root)}, {"toolkit": str(self.root)}, {"launcher": "relative/run"},
                 {"workingDirectory": str(self.dump)}, {"bundle": str(self.root)}, {"toolkitVersion": None}, {"leftOut": None},
                 {"engineCommit": 1}, {"binarySHA256": {}}, {"binarySHA256": {"toolkit/relinker": "bad"}}]
        for updates in cases:
            result = {**original, **updates} if updates else []
            path = self.work / "install.json"
            path.write_text(json.dumps(result), encoding="utf-8")
            with self.subTest(result=result), self.assertRaises(ValueError):
                title_run.read_install_result(path, "PPSA00001", self.dump, self.toolkit, self.work)

    def test_result_checks_the_executable_architecture(self):
        result = self.installed()
        path = self.work / "install.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        executable = Path(result["bundle"]) / "Contents/MacOS/eboot"
        cases = [b"\x7fELF" + bytes(28), bytes(4),
                 struct.pack("<IIIIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0),
                 struct.pack("<IIIIIIII", 0xFEEDFACF, 0x01000007, 3, 6, 0, 0, 0, 0)]
        for contents in cases:
            executable.write_bytes(contents)
            with self.subTest(contents=contents), self.assertRaisesRegex(ValueError, "not a thin x86_64 Mach-O executable"):
                title_run.read_install_result(path, "PPSA00001", self.dump, self.toolkit, self.work)

    @unittest.skipIf(os.name == "nt", "macOS CLI selection requires a POSIX host")
    def test_main_records_toolkit_identity_and_keeps_the_second_cache_warm(self):
        titles = self.root / "titles.json"
        titles.write_text("{}", encoding="utf-8")
        installation = self.installed()
        frozen = title_run.datetime.datetime(2026, 1, 2, 3, 4, 5)
        old_report = self.root / "out/runner-revision-20260102-030405"
        old_report.symlink_to(self.dump, target_is_directory=True)
        original = (self.dump / "eboot.bin").read_bytes()
        cache = self.work / "shader-cache/shader"
        cache.parent.mkdir()
        cache.write_bytes(b"stale")
        observed = []

        def run_title(executable, work, run_dir, seconds, env, pad, runtime=None):
            self.assertEqual(executable, Path(installation["launcher"]))
            self.assertEqual(runtime, Path(installation["workingDirectory"]))
            observed.append(cache.exists())
            cache.parent.mkdir(exist_ok=True)
            cache.write_bytes(b"new")
            return {"outcome": "crashed", "exit_code": 7, "seconds": 0, "presents": 0, "fps": 0.0, "first_error": "",
                    "warnings": {}, "frames": [], "os": "macOS", "gpu": "fixture", "driver": "fixture", "cpu": "fixture"}

        def git(root, *args):
            self.assertEqual(root, title_run.ROOT)
            return "runner-revision" if args[0] == "rev-parse" else ""

        with unittest.mock.patch.object(title_run.sys, "platform", "darwin"), unittest.mock.patch.object(title_run, "git", side_effect=git), \
                unittest.mock.patch.object(title_run.subprocess, "run", side_effect=self.install), \
                unittest.mock.patch.object(title_run, "run_title", side_effect=run_title), unittest.mock.patch("builtins.print"), \
                unittest.mock.patch.object(title_run.datetime, "datetime", wraps=title_run.datetime.datetime) as clock:
            clock.now.return_value = frozen
            self.assertEqual(title_run.main(["--games", str(self.dump), "--out", str(self.root / "out"), "--titles", str(titles),
                                             "--macos-tool", str(self.tool), "--toolkit", str(self.toolkit), "--runs", "2"]), 0)
        self.assertEqual(observed, [False, True])
        self.assertEqual((self.dump / "eboot.bin").read_bytes(), original)
        self.assertFalse((self.dump / "summary.json").exists())
        report = next((self.root / "out").glob("*/summary.json"))
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["build"]["commit"], "runner-revision")
        self.assertEqual(data["titles"]["PPSA00001"]["installation"], installation)
        self.assertEqual(json.loads((report.parent / "PPSA00001/install.json").read_text(encoding="utf-8")), installation)
        self.assertEqual(data["macos"]["tool_sha256"], title_run.file_sha256(self.tool))
        self.assertIn("AnyPS5 macOS toolkit `fixture-1`", (report.parent / "summary.md").read_text(encoding="utf-8"))
        handle = title_run.lock_work(self.work.with_suffix(".lock"))
        self.assertIsNotNone(handle)
        handle.close()

    @unittest.skipIf(os.name == "nt", "macOS CLI selection requires a POSIX host")
    def test_main_keeps_installer_output_when_result_decoding_fails(self):
        titles = self.root / "titles.json"
        titles.write_text("{}", encoding="utf-8")

        def malformed(command, **kwargs):
            kwargs["stdout"].write("raw conversion details\n")
            Path(command[-1]).write_text("not json", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with unittest.mock.patch.object(title_run.sys, "platform", "darwin"), unittest.mock.patch.object(title_run, "git", return_value="fixture"), \
                unittest.mock.patch.object(title_run.subprocess, "run", side_effect=malformed), unittest.mock.patch("builtins.print"):
            self.assertEqual(title_run.main(["--games", str(self.dump), "--out", str(self.root / "out"), "--titles", str(titles),
                                             "--macos-tool", str(self.tool), "--toolkit", str(self.toolkit), "--runs", "1"]), 0)
        report = next((self.root / "out").glob("*/summary.json"))
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(data["titles"]["PPSA00001"]["runs"][0]["outcome"], "relink-failed")
        self.assertEqual(data["driver_cache"], "uncontrolled (macOS/Metal)")
        self.assertFalse(data["macos"]["audio_validated"])
        self.assertEqual(data["build"]["component"], "runner-source")
        log = (report.parent / "PPSA00001/relink.log").read_text(encoding="utf-8")
        self.assertTrue(log.startswith("raw conversion details\n"))
        self.assertIn("JSONDecodeError:", log)

    @unittest.skipIf(os.name == "nt", "POSIX launch script")
    def test_run_captures_direct_output_and_frames_from_runtime(self):
        installation = self.installed()
        launcher, runtime = Path(installation["launcher"]), Path(installation["workingDirectory"])
        launcher.write_text('#!/bin/sh\nif [ -n "$ANYPS5_UPDATE_DIR$ANYPS5_ADDCONT_DIRS" ]; then exit 9; fi\nprintf "raw stdout\\n%s\\n%s\\n" "$PWD" "$ANYPS5_SHADER_CACHE_DIR"\nprintf "raw stderr\\n" >&2\nexit 7\n', encoding="utf-8")
        bmp(runtime / "frame_1.bmp", 4, 4, lambda x, y: (x * 40, y * 40, 0))
        run_dir = self.root / "run1"
        with unittest.mock.patch.dict(os.environ, {"ANYPS5_UPDATE_DIR": "/ambient/update", "ANYPS5_ADDCONT_DIRS": "/ambient/dlc"}):
            result = title_run.run_title(launcher, self.work, run_dir, 1, {}, runtime=runtime)
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["outcome"], "crashed")
        self.assertEqual(result["frames"], ["frame_1.bmp"])
        self.assertFalse((runtime / "frame_1.bmp").exists())
        self.assertEqual((run_dir / "stdout.log").read_text(encoding="utf-8"), f"raw stdout\n{runtime}\n{self.work / 'shader-cache'}\n")
        self.assertEqual((run_dir / "stderr.log").read_text(encoding="utf-8"), "raw stderr\n")
        self.assertEqual(result["audio_driver"], "dummy")
        self.assertFalse(result["audio_validated"])


def wait_until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


class PadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name) / "work"
        self.work.mkdir()

    def run_with(self, pad, seconds=1):
        seen = {}

        class FakeSession:
            def __init__(self, command, cwd, env, stdout, stderr, owned=None):
                seen["env"] = env

            def alive(self):
                return True

            def send_new_windows_to_back(self):
                pass

            def exit_code(self):
                return None

            def close(self):
                seen["closed"] = True

            def kill(self):
                pass

        with unittest.mock.patch.object(title_run, "Session", FakeSession), unittest.mock.patch.object(title_run, "CLOSE_SECONDS", 0):
            result = title_run.run_title(self.work / "eboot.exe", self.work, self.work.parent / "run1", seconds, {}, pad)
        return result, seen["env"]

    def test_timeline_is_queued_after_the_delay(self):
        stale = self.work / "pad-input.txt"
        stale.write_text("cross\n", encoding="utf-8")
        result, env = self.run_with({"after_s": 0, "repeat": 2, "events": [{"at_ms": 0, "hold_ms": 200, "buttons": ["right", "cross"]}]})
        self.assertEqual(env["APS5_PAD_INPUT_FILE"], str(stale))
        lines = stale.read_text(encoding="utf-8").splitlines()
        self.assertRegex(lines[0], r"^APS5_PAD_TIMELINE 1 [0-9]+ 2$")
        self.assertEqual(lines[1:], [f"0 200 {0x20 | 0x4000}", f"400 200 {0x20 | 0x4000}"])
        self.assertIsNotNone(result["pad_queued_s"])

    def test_no_pad_leaves_the_input_file_alone(self):
        with unittest.mock.patch.dict(os.environ, {"APS5_PAD_INPUT_FILE": str(self.work / "outside.txt")}):
            result, env = self.run_with(None)
        self.assertNotIn("APS5_PAD_INPUT_FILE", env)
        self.assertNotIn("pad_queued_s", result)

    def test_a_late_delay_queues_nothing(self):
        result, _ = self.run_with({"after_s": 30, "events": [{"at_ms": 0, "hold_ms": 200, "buttons": ["cross"]}]})
        self.assertIsNone(result["pad_queued_s"])
        self.assertFalse((self.work / "pad-input.txt").exists())


class SessionTests(unittest.TestCase):
    def start(self, code, **options):
        session = title_run.Session([sys.executable, "-c", code], Path.cwd(), dict(os.environ), subprocess.DEVNULL, subprocess.DEVNULL, **options)
        self.addCleanup(session.kill)
        return session

    def test_exit_code(self):
        session = self.start("import sys; sys.exit(3)")
        self.assertTrue(wait_until(lambda: not session.alive()))
        self.assertEqual(session.exit_code(), 3)

    @unittest.skipUnless(os.name == "nt", "LoadExec hand-over is Windows-specific")
    def test_exit_code_of_the_last_process(self):
        session = self.start("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time, sys; time.sleep(1); sys.exit(7)'])")
        self.assertTrue(wait_until(lambda: not session.alive()))
        self.assertEqual(session.exit_code(), 7)

    @unittest.skipUnless(os.name == "nt", "job process list is Windows-specific")
    def test_exit_code_ignores_processes_outside_owned(self):
        child = "import subprocess, time; time.sleep(1); subprocess.run(['cmd.exe', '/c', 'exit 0']); raise SystemExit(7)"
        session = self.start(f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}])", owned=Path(sys.executable).parent)
        self.assertTrue(wait_until(lambda: not session.alive()))
        self.assertEqual(session.exit_code(), 7)

    @unittest.skipUnless(os.name == "nt", "window placement is Windows-specific")
    def test_new_windows_go_behind_the_others_without_polling(self):
        window = ("import ctypes; user32 = ctypes.WinDLL('user32'); user32.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint] + [ctypes.c_int] * 4 + [ctypes.c_void_p] * 4; "
                  "user32.CreateWindowExW(0x08000000, 'STATIC', 'title_run test', 0x10CF0000, 100, 100, 200, 200, None, None, None, None); message = ctypes.create_string_buffer(64)\n"
                  "while user32.GetMessageW(message, None, 0, 0) > 0: user32.DispatchMessageW(message)")
        user32 = title_run.user32
        if not user32.FindWindowW("Progman", None):
            self.skipTest("no desktop shell in this session")
        session = self.start(window)
        user32.GetWindow.restype = title_run.wintypes.HWND
        user32.GetWindow.argtypes = (title_run.wintypes.HWND, title_run.wintypes.UINT)
        name = title_run.ctypes.create_unicode_buffer(256)

        def below(window):
            classes, window = set(), user32.GetWindow(window, 2)
            while window:
                if user32.IsWindowVisible(window) and user32.GetClassNameW(window, name, 256):
                    classes.add(name.value)
                window = user32.GetWindow(window, 2)
            return classes

        self.assertTrue(wait_until(lambda: session.windows() and all(below(found) <= {"Progman", "WorkerW"} for found in session.windows())))
        rect = title_run.wintypes.RECT()
        user32.GetWindowRect(session.windows()[0], title_run.ctypes.byref(rect))
        self.assertEqual((rect.left, rect.top), (100, 100))

    def test_children_keep_it_alive_and_are_killed(self):
        session = self.start("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])")
        self.assertTrue(wait_until(lambda: session.exit_code() == 0))
        self.assertTrue(session.alive())
        session.kill()
        self.assertTrue(wait_until(lambda: not session.alive()))


if __name__ == "__main__":
    unittest.main()
