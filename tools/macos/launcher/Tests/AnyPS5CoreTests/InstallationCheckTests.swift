import Foundation
import XCTest
@testable import AnyPS5Core

final class InstallationCheckTests: XCTestCase {
    private var root: URL!
    private var app: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        app = root.appendingPathComponent("AnyPS5.app")
        try write("Contents/Info.plist", PropertyListSerialization.data(fromPropertyList: [
            "CFBundleIdentifier": "org.anyps5.launcher", "CFBundleExecutable": "AnyPS5",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0.0",
            "AnyPS5Repository": "owner/fork", "AnyPS5Commit": String(repeating: "a", count: 40),
        ], format: .xml, options: 0))
        for name in ["AnyPS5", "anyps5-tool"] { try write("Contents/MacOS/" + name, Data("binary".utf8), executable: true) }
        for name in ["fonts/NotoSans-Regular.ttf", "fonts/OFL.txt", "browser-pad/index.html", "l10n/en.json", "licenses/AnyPS5-LICENSE.txt"] {
            try write("Contents/Resources/" + name, Data("resource".utf8))
        }
        for name in ["WebRTC", "LiveKitWebRTC", "RustLiveKitUniFFI"] {
            try write("Contents/Frameworks/\(name).framework/\(name)", Data("framework".utf8), executable: true)
        }
        try write("Contents/Resources/LiveKit_LiveKit.bundle/Contents/Info.plist", PropertyListSerialization.data(fromPropertyList: [
            "CFBundleIdentifier": "test.livekit.resources", "CFBundlePackageType": "BNDL",
        ], format: .xml, options: 0))
        for name in ["libs/libc.prx", "vulkan/libvulkan.1.dylib", "vulkan/libMoltenVK.dylib", "vulkan/MoltenVK_icd.json", "patches/a.json"] {
            try write("Contents/Resources/toolkit/" + name, Data("toolkit".utf8))
        }
        try write("Contents/Resources/toolkit/relinker", Data("relinker".utf8), executable: true)
        let fingerprint = try XCTUnwrap(Toolkit.fingerprint(of: app.appendingPathComponent("Contents/Resources/toolkit")))
        try write("Contents/Resources/toolkit/version.txt", Data("1.0.0-\(fingerprint)\n".utf8))
    }

    override func tearDownWithError() throws { try FileManager.default.removeItem(at: root) }

    private func write(_ relative: String, _ data: Data, executable: Bool = false) throws {
        let file = app.appendingPathComponent(relative)
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: file)
        if executable { try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path) }
    }

    private func inspect() throws -> InstallationCheck.Report {
        try InstallationCheck.inspect(bundle: XCTUnwrap(Bundle(url: app)))
    }

    func testCompleteBundleReportsItsActualSourceAndToolkit() throws {
        let report = try inspect()
        XCTAssertEqual(report.schemaVersion, 1)
        XCTAssertEqual(report.repository, "owner/fork")
        XCTAssertEqual(report.revision, String(repeating: "a", count: 40))
        XCTAssertEqual(report.launcherVersion, "1.0.0")
        XCTAssertEqual(report.libraryCount, 1)
        XCTAssertEqual(report.resourceBundles, ["LiveKit_LiveKit.bundle"])
        XCTAssertEqual(try JSONDecoder().decode(InstallationCheck.Report.self, from: JSONEncoder().encode(report)), report)
    }

    func testResourceBundleMustBePackagedInsideTheApp() throws {
        try FileManager.default.moveItem(at: app.appendingPathComponent("Contents/Resources/LiveKit_LiveKit.bundle"),
                                        to: root.appendingPathComponent("LiveKit_LiveKit.bundle"))
        XCTAssertThrowsError(try inspect())
    }

    func testMissingRuntimeFrameworkIsRejected() throws {
        try FileManager.default.removeItem(at: app.appendingPathComponent("Contents/Frameworks/RustLiveKitUniFFI.framework"))
        XCTAssertThrowsError(try inspect())
    }

    func testEmptyRequiredResourceIsRejected() throws {
        try write("Contents/Resources/browser-pad/index.html", Data())
        XCTAssertThrowsError(try inspect())
    }

    func testChangedLibraryIsRejectedByTheToolkitFingerprint() throws {
        try write("Contents/Resources/toolkit/libs/libc.prx", Data("different library".utf8))
        XCTAssertThrowsError(try inspect())
    }

    func testExternalResourceLinkCannotUseTheBuildTree() throws {
        let resource = app.appendingPathComponent("Contents/Resources/fonts/NotoSans-Regular.ttf")
        let outside = root.appendingPathComponent("font.ttf")
        try FileManager.default.moveItem(at: resource, to: outside)
        try FileManager.default.createSymbolicLink(at: resource, withDestinationURL: outside)
        XCTAssertThrowsError(try inspect())
    }

    func testNonExecutableCommandLineInstallerIsRejected() throws {
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: app.appendingPathComponent("Contents/MacOS/anyps5-tool").path)
        XCTAssertThrowsError(try inspect())
    }

    func testMalformedSourceIdentityIsRejected() throws {
        let info = app.appendingPathComponent("Contents/Info.plist")
        let original = try XCTUnwrap(PropertyListSerialization.propertyList(from: Data(contentsOf: info), format: nil) as? [String: Any])
        for (key, value) in [("AnyPS5Repository", "https://github.com/owner/fork"),
                             ("AnyPS5Repository", "owner/fork\n"), ("AnyPS5Commit", "unknown")] {
            var changed = original
            changed[key] = value
            let variant = root.appendingPathComponent(UUID().uuidString + ".app")
            try FileManager.default.copyItem(at: app, to: variant)
            try PropertyListSerialization.data(fromPropertyList: changed, format: .xml, options: 0)
                .write(to: variant.appendingPathComponent("Contents/Info.plist"))
            XCTAssertThrowsError(try InstallationCheck.inspect(bundle: XCTUnwrap(Bundle(url: variant))))
        }
    }
}
