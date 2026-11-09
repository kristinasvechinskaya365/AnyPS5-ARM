import XCTest
@testable import AnyPS5Core

final class PlaceTests: XCTestCase {
    var store: URL!
    let fm = FileManager.default

    override func setUpWithError() throws {
        store = fm.temporaryDirectory.appendingPathComponent("place-\(UUID().uuidString)/.AnyPS5")
        try fm.createDirectory(at: store.appendingPathComponent("Games"), withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? fm.removeItem(at: store.deletingLastPathComponent()) }

    /// A bundle as the installer builds it: code in MacOS, the game in Resources/game.
    func partial(_ name: String) throws -> URL {
        let bundle = store.appendingPathComponent("Games/\(name).partial")
        let macOS = bundle.appendingPathComponent("Contents/MacOS")
        let game = bundle.appendingPathComponent("Contents/Resources/game")
        try fm.createDirectory(at: macOS.appendingPathComponent("libs"), withIntermediateDirectories: true)
        try fm.createDirectory(at: game.appendingPathComponent("app0"), withIntermediateDirectories: true)
        try Data("lib".utf8).write(to: macOS.appendingPathComponent("libs/libc.prx"))
        try Data("eboot".utf8).write(to: game.appendingPathComponent("app0/eboot.bin"))
        try fm.createSymbolicLink(atPath: game.appendingPathComponent("libs").path, withDestinationPath: "../../MacOS/libs")
        try fm.createSymbolicLink(atPath: game.appendingPathComponent("app0/libs").path, withDestinationPath: "../../../MacOS/libs")
        try fm.createSymbolicLink(atPath: macOS.appendingPathComponent("app0").path, withDestinationPath: "../Resources/game/app0")
        return bundle
    }

    func text(_ url: URL) -> String? { (try? Data(contentsOf: url)).map { String(decoding: $0, as: UTF8.self) } }

    func testTheGameLivesOutsideTheBundle() throws {
        let bundle = store.appendingPathComponent("Games/X-1.app")
        let data = store.appendingPathComponent("Data/X-1")
        try Installer.place(partial: try partial("X-1"), bundle: bundle, data: data)
        let link = try fm.destinationOfSymbolicLink(atPath: bundle.appendingPathComponent("Contents/Resources/game").path)
        XCTAssertEqual(link, "../../../../Data/X-1/game")
        XCTAssertEqual(text(bundle.appendingPathComponent("Contents/MacOS/app0/eboot.bin")), "eboot", "MacOS/app0 reaches the game through the link")
        XCTAssertEqual(text(data.appendingPathComponent("game/libs/libc.prx")), "lib", "game/libs reaches the bundle's libraries")
        XCTAssertEqual(text(data.appendingPathComponent("game/app0/libs/libc.prx")), "lib", "app0/libs reaches the bundle's libraries")
        XCTAssertFalse(fm.fileExists(atPath: store.appendingPathComponent("Games/X-1.partial").path))
    }

    func testLinksStayShortUnderPrivate() throws {
        let resolved = store.resolvingSymlinksInPath().path
        let privateStore = URL(fileURLWithPath: resolved.hasPrefix("/private/") ? resolved : "/private" + store.path)
        XCTAssertTrue(fm.fileExists(atPath: privateStore.path))
        let bundle = privateStore.appendingPathComponent("Games/X-1.app")
        let data = privateStore.appendingPathComponent("Data/X-1")
        let staged = try partial("X-1")
        let privateStaged = privateStore.appendingPathComponent("Games/" + staged.lastPathComponent)
        try Installer.place(partial: privateStaged, bundle: bundle, data: data)
        XCTAssertEqual(try fm.destinationOfSymbolicLink(atPath: bundle.appendingPathComponent("Contents/Resources/game").path), "../../../../Data/X-1/game")
        XCTAssertEqual(try fm.destinationOfSymbolicLink(atPath: data.appendingPathComponent("game/libs").path), "../../../Games/X-1.app/Contents/MacOS/libs")
    }

    func testAnOldBundleKeepsItsSavesAndMovesOut() throws {
        let bundle = store.appendingPathComponent("Games/X-1.app")
        let old = try partial("old")
        try Data("save".utf8).write(to: old.appendingPathComponent("Contents/Resources/game/_sd").appendingPathExtension("bin"))
        try fm.moveItem(at: old, to: bundle)
        let data = store.appendingPathComponent("Data/X-1")
        try Installer.place(partial: try partial("X-1"), bundle: bundle, data: data)
        XCTAssertEqual(text(data.appendingPathComponent("game/_sd.bin")), "save")
        XCTAssertEqual(text(bundle.appendingPathComponent("Contents/Resources/game/_sd.bin")), "save")
    }

    func testUnlockingAgainKeepsSavesInTheDataFolder() throws {
        let bundle = store.appendingPathComponent("Games/X-1.app")
        let data = store.appendingPathComponent("Data/X-1")
        try Installer.place(partial: try partial("X-1"), bundle: bundle, data: data)
        try Data("save".utf8).write(to: data.appendingPathComponent("game/_sd.bin"))
        try Data("old game".utf8).write(to: data.appendingPathComponent("game/app0/eboot.bin"))
        try Installer.place(partial: try partial("X-1"), bundle: bundle, data: data)
        XCTAssertEqual(text(data.appendingPathComponent("game/_sd.bin")), "save", "saves stay")
        XCTAssertEqual(text(data.appendingPathComponent("game/app0/eboot.bin")), "eboot", "the game itself is the new one")
        XCTAssertEqual(text(data.appendingPathComponent("game/libs/libc.prx")), "lib")
    }

    func write(_ text: String, to url: URL) throws {
        try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(text.utf8).write(to: url)
    }

    func oldInstallation(external: Bool) throws -> (bundle: URL, data: URL, game: URL) {
        let bundle = store.appendingPathComponent("Games/X-1.app")
        let data = store.appendingPathComponent("Data/X-1")
        let old = try partial("old")
        try write("old executable", to: old.appendingPathComponent("Contents/MacOS/eboot"))
        try write("old library", to: old.appendingPathComponent("Contents/MacOS/libs/libc.prx"))
        if external {
            try Installer.place(partial: old, bundle: bundle, data: data)
        } else {
            try fm.moveItem(at: old, to: bundle)
        }
        let game = bundle.appendingPathComponent("Contents/Resources/game").resolvingSymlinksInPath()
        try write("old game", to: game.appendingPathComponent("app0/eboot.bin"))
        try write("save", to: game.appendingPathComponent("_sd/user/slot/data.bin"))
        try write("trophy", to: game.appendingPathComponent("_trophy/trophy.bin"))
        try write("parked save", to: game.appendingPathComponent("_profiles/other/_sd/slot.bin"))
        try write("profile", to: game.appendingPathComponent(".anyps5-profile"))
        return (bundle, data, game)
    }

    func requireOriginal(_ bundle: URL, game: URL, file: StaticString = #filePath, line: UInt = #line) throws {
        XCTAssertEqual(text(bundle.appendingPathComponent("Contents/MacOS/eboot")), "old executable", "failed placement must preserve the old executable", file: file, line: line)
        XCTAssertEqual(text(bundle.appendingPathComponent("Contents/MacOS/libs/libc.prx")), "old library", file: file, line: line)
        XCTAssertEqual(text(game.appendingPathComponent("app0/eboot.bin")), "old game", file: file, line: line)
        XCTAssertEqual(text(game.appendingPathComponent("_sd/user/slot/data.bin")), "save", "failed placement must preserve save bytes after partial cleanup", file: file, line: line)
        XCTAssertEqual(text(game.appendingPathComponent("_trophy/trophy.bin")), "trophy", file: file, line: line)
        XCTAssertEqual(text(game.appendingPathComponent("_profiles/other/_sd/slot.bin")), "parked save", file: file, line: line)
        XCTAssertEqual(text(game.appendingPathComponent(".anyps5-profile")), "profile", file: file, line: line)
        XCTAssertEqual(text(bundle.appendingPathComponent("Contents/MacOS/app0/eboot.bin")), "old game", file: file, line: line)
        XCTAssertEqual(text(game.appendingPathComponent("libs/libc.prx")), "old library", file: file, line: line)
        XCTAssertEqual(text(game.appendingPathComponent("app0/libs/libc.prx")), "old library", file: file, line: line)
    }

    func immutable(_ url: URL, _ enabled: Bool) throws {
        guard chflags(url.path, enabled ? UInt32(UF_IMMUTABLE) : 0) == 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
    }

    func testFailedDataDestinationPreservesLegacySavesAfterCleanup() throws {
        let old = try oldInstallation(external: false)
        try write("occupied destination", to: old.data)
        let staged = try partial("X-1")
        XCTAssertThrowsError(try Installer.place(partial: staged, bundle: old.bundle, data: old.data))
        try fm.removeItem(at: staged)
        try requireOriginal(old.bundle, game: old.game)
        XCTAssertEqual(text(old.data), "occupied destination")
    }

    func finalMoveFailure(external: Bool) throws {
        let old = try oldInstallation(external: external)
        let save = old.game.appendingPathComponent("_sd/user/slot/data.bin")
        let identity = try fm.attributesOfItem(atPath: save.path)[.systemFileNumber] as? NSNumber
        let staged = try partial("X-1")
        try immutable(staged, true)
        defer { try? immutable(staged, false) }
        XCTAssertThrowsError(try Installer.place(partial: staged, bundle: old.bundle, data: old.data))
        XCTAssertEqual(try staged.appendingPathComponent("Contents/Resources/game").resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink, true)
        try immutable(staged, false)
        try fm.removeItem(at: staged)
        try requireOriginal(old.bundle, game: old.game)
        XCTAssertEqual((try? fm.attributesOfItem(atPath: save.path)[.systemFileNumber]) as? NSNumber, identity)
        try Installer.place(partial: try partial("X-1"), bundle: old.bundle, data: old.data)
        XCTAssertEqual(text(old.data.appendingPathComponent("game/_sd/user/slot/data.bin")), "save")
        XCTAssertEqual(text(old.data.appendingPathComponent("game/app0/eboot.bin")), "eboot")
        XCTAssertEqual(text(old.data.appendingPathComponent("game/libs/libc.prx")), "lib")
        XCTAssertEqual(try fm.contentsOfDirectory(atPath: old.data.path), ["game"])
        XCTAssertEqual(try fm.contentsOfDirectory(atPath: store.appendingPathComponent("Games").path), ["X-1.app"])
    }

    func testFailedFinalMoveRestoresLegacyBundleAndSaves() throws {
        try finalMoveFailure(external: false)
    }

    func testFailedFinalMoveRestoresExternalBundleAndSaves() throws {
        try finalMoveFailure(external: true)
    }

    func testSuccessfulReinstallPreservesNestedAndParkedSaves() throws {
        let old = try oldInstallation(external: true)
        try Installer.place(partial: try partial("X-1"), bundle: old.bundle, data: old.data)
        let game = old.data.appendingPathComponent("game")
        XCTAssertEqual(text(game.appendingPathComponent("_sd/user/slot/data.bin")), "save")
        XCTAssertEqual(text(game.appendingPathComponent("_trophy/trophy.bin")), "trophy")
        XCTAssertEqual(text(game.appendingPathComponent("_profiles/other/_sd/slot.bin")), "parked save")
        XCTAssertEqual(text(game.appendingPathComponent(".anyps5-profile")), "profile")
        XCTAssertEqual(text(game.appendingPathComponent("app0/eboot.bin")), "eboot")
        XCTAssertEqual(text(game.appendingPathComponent("libs/libc.prx")), "lib")
        XCTAssertEqual(try fm.contentsOfDirectory(atPath: old.data.path), ["game"])
        XCTAssertEqual(try fm.contentsOfDirectory(atPath: store.appendingPathComponent("Games").path), ["X-1.app"])
    }

    func testFailedFirstInstallLeavesNoGameAndCanRetry() throws {
        let bundle = store.appendingPathComponent("Games/X-1.app")
        let data = store.appendingPathComponent("Data/X-1")
        let staged = try partial("X-1")
        try immutable(staged, true)
        defer { try? immutable(staged, false) }
        XCTAssertThrowsError(try Installer.place(partial: staged, bundle: bundle, data: data))
        try immutable(staged, false)
        try fm.removeItem(at: staged)
        XCTAssertFalse(fm.fileExists(atPath: bundle.path))
        XCTAssertFalse(fm.fileExists(atPath: data.appendingPathComponent("game").path))
        try Installer.place(partial: try partial("X-1"), bundle: bundle, data: data)
        XCTAssertEqual(text(bundle.appendingPathComponent("Contents/MacOS/app0/eboot.bin")), "eboot")
        XCTAssertEqual(try fm.contentsOfDirectory(atPath: data.path), ["game"])
    }
}
