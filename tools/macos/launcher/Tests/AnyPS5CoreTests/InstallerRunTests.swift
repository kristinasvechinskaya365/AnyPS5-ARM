import Foundation
import XCTest
@testable import AnyPS5Core

final class InstallerRunTests: XCTestCase {
    var scratch: URL!
    let fm = FileManager.default

    override func setUpWithError() throws {
        scratch = fm.temporaryDirectory.appendingPathComponent("installer-run-\(UUID().uuidString)")
        try fm.createDirectory(at: scratch, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? fm.removeItem(at: scratch) }

    func fixture(folder: String = "dump") throws -> (game: Game, toolkit: Toolkit, root: URL, arguments: URL) {
        let dump = scratch.appendingPathComponent(folder)
        try fm.createDirectory(at: dump.appendingPathComponent("sce_sys"), withIntermediateDirectories: true)
        try Data("\u{7F}ELF executable".utf8).write(to: dump.appendingPathComponent("eboot.bin"))
        try Data(#"{"titleId":"PPSA00001","contentVersion":"01.000.000","localizedParameters":{"defaultLanguage":"en-US","en-US":{"titleName":"Test"}}}"#.utf8)
            .write(to: dump.appendingPathComponent("sce_sys/param.json"))
        let root = scratch.appendingPathComponent("game store")
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        let toolkit = try FakeToolkit.make(in: scratch)
        let arguments = scratch.appendingPathComponent("arguments.txt")
        let script = """
        #!/bin/sh
        printf '%s\\n' "$@" > "\(arguments.path)"
        for argument; do last="$argument"; done
        mkdir -p "$(dirname "$last")"
        printf 'relinked\\n' > "$last"

        """
        try script.write(to: toolkit.appendingPathComponent("relinker"), atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: toolkit.appendingPathComponent("relinker").path)
        let game = try XCTUnwrap(GameLibrary.inspect(dump))
        return (game, Toolkit(root: toolkit), root, arguments)
    }

    func install(_ game: Game, _ toolkit: Toolkit, _ root: URL) throws -> URL {
        try Installer(game: game, toolkit: toolkit, progress: { _, _ in }, log: { _ in }).run(root: root, requireOwnedStore: true)
    }

    func executable(in bundle: URL) throws {
        let script = """
        #!/bin/sh
        printf '%s\\n' "pid=$$" "cwd=$(pwd -P)" "argument=$1" "cache=$ANYPS5_SHADER_CACHE_DIR" "driver=$VK_DRIVER_FILES" "submits=$MVK_CONFIG_SYNCHRONOUS_QUEUE_SUBMITS"
        printf 'raw stderr\\n' >&2
        exit 7

        """
        let file = bundle.appendingPathComponent("Contents/MacOS/eboot")
        try script.write(to: file, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
    }

    func run(_ file: URL, _ arguments: [String] = [], environment: [String: String] = [:]) throws -> (code: Int32, pid: Int32, stdout: String, stderr: String) {
        let process = Process()
        process.executableURL = file
        process.arguments = arguments
        process.environment = ProcessInfo.processInfo.environment.merging(["HOME": scratch.appendingPathComponent("home").path]) { _, new in new }
            .merging(environment) { _, new in new }
        let stdout = Pipe(), stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr
        try process.run()
        process.waitUntilExit()
        return (process.terminationStatus, process.processIdentifier,
                String(decoding: stdout.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self),
                String(decoding: stderr.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self))
    }

    func testStdioRunnerPreservesProcessOutputAndEnvironment() throws {
        let (game, toolkit, root, _) = try fixture()
        let bundle = try install(game, toolkit, root)
        try executable(in: bundle)
        let cache = scratch.appendingPathComponent("isolated cache").path
        let result = try run(bundle.appendingPathComponent("Contents/MacOS/run"), ["space and $literal"],
                             environment: ["ANYPS5_SHADER_CACHE_DIR": cache, "VK_DRIVER_FILES": "/custom/icd.json", "MVK_CONFIG_SYNCHRONOUS_QUEUE_SUBMITS": "1"])
        XCTAssertEqual(result.code, 7)
        XCTAssertEqual(result.stdout, "pid=\(result.pid)\ncwd=\(bundle.appendingPathComponent("Contents/Resources/game").resolvingSymlinksInPath().path)\nargument=space and $literal\ncache=\(cache)\ndriver=/custom/icd.json\nsubmits=1\n")
        XCTAssertEqual(result.stderr, "raw stderr\n")
        XCTAssertFalse(fm.fileExists(atPath: scratch.appendingPathComponent("home/Library/Logs/AnyPS5/PPSA00001.log").path))
    }

    func testFinderLauncherAppendsBothStreamsToTheTitleLog() throws {
        let (game, toolkit, root, _) = try fixture()
        let bundle = try install(game, toolkit, root)
        try executable(in: bundle)
        for _ in 0..<2 {
            let result = try run(bundle.appendingPathComponent("Contents/MacOS/launch"), ["finder"])
            XCTAssertEqual(result.code, 7)
            XCTAssertEqual(result.stdout, "")
            XCTAssertEqual(result.stderr, "")
        }
        let log = try String(contentsOf: scratch.appendingPathComponent("home/Library/Logs/AnyPS5/PPSA00001.log"), encoding: .utf8)
        XCTAssertEqual(log.components(separatedBy: "argument=finder\n").count, 3)
        XCTAssertEqual(log.components(separatedBy: "raw stderr\n").count, 3)
    }

    func testInstallResultEncodesPathsAndOmittedModules() throws {
        let (game, toolkit, root, _) = try fixture(folder: "Oyunlar/\"quoted\"\nOyunları")
        let missing = "lib encrypted.prx"
        try Data([1, 2, 3, 4]).write(to: game.folder.appendingPathComponent(missing))
        let installer = Installer(game: game, toolkit: toolkit, progress: { _, _ in }, log: { _ in })
        let bundle = try installer.run(root: root)
        let file = scratch.appendingPathComponent("result.json")
        try InstallResult(game: game, toolkit: toolkit, bundle: bundle, leftOut: installer.leftOut).write(to: file)
        let result = try JSONDecoder().decode(InstallResult.self, from: Data(contentsOf: file))
        XCTAssertEqual(result.schemaVersion, 1)
        XCTAssertEqual(result.executionMode, "rosetta-x86_64")
        XCTAssertEqual(result.titleID, game.titleID)
        XCTAssertEqual(result.source, game.folder.resolvingSymlinksInPath().path)
        XCTAssertEqual(result.toolkit, toolkit.root.resolvingSymlinksInPath().path)
        XCTAssertEqual(result.toolkitVersion, toolkit.version)
        XCTAssertEqual(result.engineCommit, toolkit.manifest?.engineCommit)
        XCTAssertEqual(result.binarySHA256["toolkit/relinker"], try FileDigest.sha256(toolkit.relinker))
        XCTAssertEqual(result.binarySHA256["bundle/Contents/MacOS/eboot"], try FileDigest.sha256(bundle.appendingPathComponent("Contents/MacOS/eboot")))
        XCTAssertEqual(result.binarySHA256["bundle/Contents/MacOS/libs/libMoltenVK.dylib"], try FileDigest.sha256(toolkit.vulkan.appendingPathComponent("libMoltenVK.dylib")))
        XCTAssertEqual(result.launcher, bundle.appendingPathComponent("Contents/MacOS/run").resolvingSymlinksInPath().path)
        XCTAssertEqual(result.workingDirectory, bundle.appendingPathComponent("Contents/Resources/game").resolvingSymlinksInPath().path)
        XCTAssertEqual(result.leftOut, [LeftOutModule(missing, .encrypted)])
        XCTAssertEqual(try Data(contentsOf: game.folder.appendingPathComponent(missing)), Data([1, 2, 3, 4]))
        try fm.removeItem(at: toolkit.root.appendingPathComponent("toolkit.json"))
        let withoutManifest = try InstallResult(game: game, toolkit: toolkit, bundle: bundle, leftOut: installer.leftOut)
        XCTAssertNil(withoutManifest.engineCommit)
    }

    func testInstallUsesRosettaAndEveryDiscoveredModuleDirectory() throws {
        let (game, toolkit, root, arguments) = try fixture()
        let modules = ["root.prx", "sce_module/standard.prx", "Media/Modules/module.prx", "Media/Plugins/plugin.sprx"]
        for name in modules {
            let file = game.folder.appendingPathComponent(name)
            try fm.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data("\u{7F}ELF module".utf8).write(to: file)
        }
        _ = try install(game, toolkit, root)
        let received = try String(contentsOf: arguments, encoding: .utf8).split(separator: "\n").map(String.init)
        XCTAssertEqual(Array(received.dropLast()), ["--macos", "--to-rosetta", "--module-dir", ".", "--module-dir", "Media/Modules", "--module-dir", "Media/Plugins", "eboot.bin"])
        XCTAssertEqual(Installer.modules(in: game.folder).map(\.relativePath), modules.sorted())
    }

    func testCommandLineInstallWritesTheResultFile() throws {
        let (game, toolkit, root, _) = try fixture(folder: "dump with spaces")
        let tool = Bundle(for: InstallerRunTests.self).bundleURL.deletingLastPathComponent().appendingPathComponent("anyps5-tool")
        let output = scratch.appendingPathComponent("install result.json")
        let outcome = try run(tool, ["install", game.folder.path, root.path, "--result", output.path], environment: ["ANYPS5_TOOLKIT": toolkit.root.path])
        XCTAssertEqual(outcome.code, 0, outcome.stdout + outcome.stderr)
        let result = try JSONDecoder().decode(InstallResult.self, from: Data(contentsOf: output))
        XCTAssertEqual(result.titleID, game.titleID)
        XCTAssertEqual(result.toolkitVersion, toolkit.version)
        XCTAssertEqual(result.source, game.folder.resolvingSymlinksInPath().path)
        XCTAssertTrue(fm.isExecutableFile(atPath: result.launcher))
        XCTAssertNotNil(Installer.record(of: URL(fileURLWithPath: result.bundle)))
    }

    func testBenchmarkRefusesRedirectedStoreDestinationsBeforeRelinking() throws {
        let (game, toolkit, root, arguments) = try fixture()
        let bundle = Installer.bundle(for: game, root: root)
        let data = Installer.dataFolder(for: game, root: root)
        let kept = Installer.keptRelink(for: game, root: root)
        let original = try Data(contentsOf: game.folder.appendingPathComponent("eboot.bin"))
        for target in [data, bundle, kept, data.appendingPathComponent("game"), bundle.appendingPathComponent("Contents/Resources/game")] {
            try fm.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
            try fm.createSymbolicLink(at: target, withDestinationURL: game.folder)
            XCTAssertThrowsError(try install(game, toolkit, root)) { error in
                XCTAssertTrue(error.localizedDescription.contains("redirected outside its owned location"), error.localizedDescription)
            }
            XCTAssertEqual(try Data(contentsOf: game.folder.appendingPathComponent("eboot.bin")), original)
            XCTAssertFalse(fm.fileExists(atPath: arguments.path))
            try fm.removeItem(at: target)
        }
    }

    func testBenchmarkKeepsAssetLinksAndItsOwnDataDirectory() throws {
        let (game, toolkit, root, _) = try fixture()
        let asset = scratch.appendingPathComponent("asset.pak")
        try Data("original asset".utf8).write(to: asset)
        try fm.createSymbolicLink(at: game.folder.appendingPathComponent("linked.pak"), withDestinationURL: asset)
        let bundle = try install(game, toolkit, root)
        let data = bundle.appendingPathComponent("Contents/Resources/game").resolvingSymlinksInPath()
        try Data("save".utf8).write(to: data.appendingPathComponent("save.bin"))
        _ = try install(game, toolkit, root)
        XCTAssertEqual(try Data(contentsOf: asset), Data("original asset".utf8))
        XCTAssertEqual(try Data(contentsOf: data.appendingPathComponent("save.bin")), Data("save".utf8))
        XCTAssertEqual(bundle.appendingPathComponent("Contents/Resources/game").resolvingSymlinksInPath(), data)
    }

    func testCommandLineBenchmarkRefusesAdjacentUpdatesAndAddons() throws {
        let (game, toolkit, root, _) = try fixture()
        let tool = Bundle(for: InstallerRunTests.self).bundleURL.deletingLastPathComponent().appendingPathComponent("anyps5-tool")
        let output = scratch.appendingPathComponent("result.json")
        let update = scratch.appendingPathComponent("PPSA00001-UPDATE")
        let addon = scratch.appendingPathComponent("PPSA00001-DLC/EXTRA")
        for folder in [update, addon] {
            try fm.createDirectory(at: folder.appendingPathComponent("sce_sys"), withIntermediateDirectories: true)
            try Data(#"{"contentVersion":"01.001.000"}"#.utf8).write(to: folder.appendingPathComponent("sce_sys/param.json"))
            let outcome = try run(tool, ["install", game.folder.path, root.path, "--result", output.path], environment: ["ANYPS5_TOOLKIT": toolkit.root.path])
            XCTAssertEqual(outcome.code, 2)
            XCTAssertTrue(outcome.stderr.contains("does not support adjacent updates or add-ons"), outcome.stderr)
            XCTAssertFalse(fm.fileExists(atPath: output.path))
            XCTAssertFalse(fm.fileExists(atPath: root.appendingPathComponent(".AnyPS5").path))
            try fm.removeItem(at: folder)
        }
        try fm.createDirectory(at: update.appendingPathComponent("sce_sys"), withIntermediateDirectories: true)
        try Data(#"{"contentVersion":"01.001.000"}"#.utf8).write(to: update.appendingPathComponent("sce_sys/param.json"))
        let ordinary = try run(tool, ["install", game.folder.path, root.path], environment: ["ANYPS5_TOOLKIT": toolkit.root.path])
        XCTAssertEqual(ordinary.code, 0, ordinary.stdout + ordinary.stderr)
        XCTAssertEqual(Installer.record(of: Installer.bundle(for: game, root: root))?.update, "01.001.000")
    }
}
