import contextlib
import importlib.util
import io
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'translator/package_native_app.py'
SPEC = importlib.util.spec_from_file_location('native_packager', SOURCE)
PACKAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGER)


class NativePackagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='native-packager-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.game = self.root / 'game'
        self.game.mkdir()
        self.elf = self.game / 'eboot.elf'
        self.elf.write_bytes(b'\x7fELF')
        self.libs = self.root / 'libs'
        self.libs.mkdir()
        for name in ('libkernel.prx', 'libc.prx'):
            (self.libs / name).write_bytes(b'library')
        self.work = self.root / 'work'
        self.work.mkdir()
        self.translator = self.root / 'translator'
        self.tools = {name: self.root / 'selected-tools' / name for name in ('llvm-readelf', 'llvm-objdump', 'llvm-nm', 'clang++')}
        self.calls = []
        self.needed = ''
        self.undefined = '_abcdefghijk\n___divti3\n'
        self.exports = '0x00001000 _abcdefghijk\n'
        self.inspection_failure = False

    def child(self, command, **kwargs):
        self.calls.append(command)
        name = pathlib.Path(command[0]).name
        if name == 'llvm-readelf':
            output = self.needed
        elif name == 'aps5-translator':
            pathlib.Path(command[2]).write_bytes(b'guest object')
            output = ''
        elif name == 'llvm-nm':
            output = self.undefined
        elif name == 'llvm-objdump':
            self.assertTrue(kwargs['check'])
            if self.inspection_failure:
                raise subprocess.CalledProcessError(1, command, stderr='unreadable export trie')
            output = self.exports
        elif name == 'clang++':
            pathlib.Path(command[command.index('-o') + 1]).write_bytes(b'linked executable')
            output = ''
        else:
            self.fail(f'unexpected child: {command}')
        return subprocess.CompletedProcess(command, 0, output, '')

    def build(self):
        with mock.patch.object(PACKAGER.subprocess, 'run', side_effect=self.child), contextlib.redirect_stdout(io.StringIO()):
            return PACKAGER.build_executable(self.elf, self.libs, self.translator, self.work, self.tools)

    def arguments(self, bundle=None):
        return ['--eboot', str(self.elf), '--game', str(self.game), '--libs', str(self.libs),
                '--vulkan', str(self.root / 'vulkan'), str(bundle or self.root / 'Title.app')]

    def test_explicit_tool_directory_and_linker_are_resolved(self):
        for path in self.tools.values():
            path.parent.mkdir(exist_ok=True)
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
        result = PACKAGER.resolve_tools(self.root / 'selected-tools', str(self.tools['clang++']))
        self.assertEqual(result, {name: path.resolve() for name, path in self.tools.items()})
        with mock.patch.object(PACKAGER.shutil, 'which', side_effect=lambda name: str(self.tools[name])) as lookup:
            self.assertEqual(PACKAGER.resolve_tools(), result)
            self.assertCountEqual([call.args[0] for call in lookup.call_args_list], self.tools)

    def test_missing_tool_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Required executable is unavailable'):
            PACKAGER.resolve_tools(self.root / 'unavailable')

    def test_module_discovery_covers_root_nested_and_linked_directories(self):
        for relative in ('module.prx', 'sce_module/a.sprx', 'sce_modules/deeper/a.PRX', 'prx/a.SPRX'):
            with self.subTest(relative=relative):
                path = self.game / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'guest module')
                with self.assertRaisesRegex(RuntimeError, 'cannot translate guest modules'):
                    PACKAGER.require_moduleless_game(self.game)
                path.unlink()
        external = self.root / 'external'
        external.mkdir()
        (external / 'guest.prx').write_bytes(b'guest module')
        (self.game / 'linked').symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'cannot translate guest modules'):
            PACKAGER.require_moduleless_game(self.game)
        (external / 'guest.prx').unlink()
        (external / 'loop').symlink_to(self.game, target_is_directory=True)
        PACKAGER.require_moduleless_game(self.game)

    def test_missing_or_invalid_library_stops_before_translation(self):
        for name in ('missing.prx', '../libc.prx', 'nested\\libc.prx'):
            with self.subTest(name=name):
                self.calls.clear()
                self.needed = f'(NEEDED) Shared library: [{name}]\n'
                with self.assertRaises(RuntimeError):
                    self.build()
                self.assertEqual([pathlib.Path(command[0]).name for command in self.calls], ['llvm-readelf'])

    def test_export_inspection_failure_is_not_ignored(self):
        self.inspection_failure = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.build()
        self.assertFalse(any(pathlib.Path(command[0]).name == 'clang++' for command in self.calls))
        self.assertFalse((self.work / 'relinked').exists())

    def test_missing_nid_stops_without_stubs_or_linking(self):
        self.exports = ''
        with self.assertRaisesRegex(RuntimeError, 'Native imports have no export: abcdefghijk'):
            self.build()
        self.assertFalse(any(pathlib.Path(command[0]).name == 'clang++' for command in self.calls))
        self.assertFalse((self.work / 'stubs.c').exists())
        self.assertFalse((self.work / 'stubs.o').exists())
        self.assertFalse((self.work / 'relinked').exists())

    def test_link_uses_selected_tools_and_existing_libraries(self):
        self.needed = '(NEEDED) Shared library: [libc.prx]\n'
        result = self.build()
        self.assertTrue((result / 'eboot').is_file())
        command = self.calls[-1]
        self.assertEqual(command[:3], [str(self.tools['clang++']), '-arch', 'arm64'])
        for name in ('libkernel.prx', 'libc.prx'):
            self.assertEqual(command.count(str(self.libs / name)), 1)
        self.assertTrue(all('stub' not in argument for argument in command))

    def test_main_rejects_modules_before_conversion_or_publication(self):
        (self.game / 'guest.prx').write_bytes(b'guest module')
        with mock.patch.object(PACKAGER, 'resolve_tools') as tools, mock.patch.object(PACKAGER.package_macos_app, 'package') as package:
            with self.assertRaisesRegex(RuntimeError, 'cannot translate guest modules'):
                PACKAGER.main(self.arguments())
        tools.assert_not_called()
        package.assert_not_called()
        self.assertFalse((self.root / 'Title.app').exists())

    def test_existing_or_source_nested_output_is_refused(self):
        existing = self.root / 'Existing.app'
        existing.mkdir()
        marker = existing / 'keep'
        marker.write_bytes(b'unchanged')
        for output in (existing, self.game / 'Nested.app', self.root / 'Wrong.bundle'):
            with self.subTest(output=output), mock.patch.object(PACKAGER, 'resolve_tools') as tools:
                with self.assertRaises(RuntimeError):
                    PACKAGER.main(self.arguments(output))
                tools.assert_not_called()
        self.assertEqual(marker.read_bytes(), b'unchanged')

    def test_failed_packaging_does_not_publish_partial_bundle(self):
        def fail(relinked, game, libs, vulkan, config, bundle, patch):
            bundle.mkdir()
            (bundle / 'partial').write_bytes(b'partial')
            raise RuntimeError('packaging failed')
        with mock.patch.object(PACKAGER, 'resolve_tools', return_value=self.tools), \
                mock.patch.object(PACKAGER, 'build_executable', return_value=self.work), \
                mock.patch.object(PACKAGER.package_macos_app, 'package', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'packaging failed'):
                PACKAGER.main(self.arguments())
        self.assertFalse((self.root / 'Title.app').exists())
        self.assertEqual(list(self.root.glob('.anyps5-native-*')), [])
        self.assertEqual(self.elf.read_bytes(), b'\x7fELF')

    def test_completed_bundle_is_published_from_staging(self):
        def package(relinked, game, libs, vulkan, config, bundle, patch):
            self.assertNotEqual(bundle, self.root / 'Title.app')
            self.assertFalse(patch)
            bundle.mkdir()
            (bundle / 'complete').write_bytes(b'complete')
            return 'Title', 'org.anyps5.fixture'
        with mock.patch.object(PACKAGER, 'resolve_tools', return_value=self.tools), \
                mock.patch.object(PACKAGER, 'build_executable', return_value=self.work), \
                mock.patch.object(PACKAGER.package_macos_app, 'package', side_effect=package), \
                contextlib.redirect_stdout(io.StringIO()):
            PACKAGER.main(self.arguments())
        self.assertEqual((self.root / 'Title.app/complete').read_bytes(), b'complete')
        self.assertEqual(list(self.root.glob('.anyps5-native-*')), [])


if __name__ == '__main__':
    unittest.main()
