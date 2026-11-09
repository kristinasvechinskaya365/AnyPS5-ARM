@testable import AnyPS5
import AnyPS5Core
import XCTest

@MainActor
final class ReleaseSourcePanelTests: XCTestCase {
    func model(signed: Bool? = nil) throws -> AppModel {
        let model = try LauncherFixture.model(games: 1)
        let root = try XCTUnwrap(model.preferences.libraryFolders.first)
        addTeardownBlock { try FileManager.default.removeItem(atPath: root) }
        if let signed {
            let page = URL(string: "https://github.com/kristinasvechinskaya365/AnyPS5-ARM/releases/tag/v9.9.9-arm")!
            let signature = signed ? ReleaseSigning.Assets(sums: page.appendingPathComponent("SHA256SUMS"), signature: page.appendingPathComponent("SHA256SUMS.sig")) : nil
            model.previewUpdate(Updates.Release(tag: "v9.9.9-arm", version: "9.9.9", page: page, download: page.appendingPathComponent("AnyPS5-macOS.zip"),
                                                size: 1, signature: signature), progress: nil)
        }
        model.open(.settings)
        return model
    }

    func aboutUpdate(_ model: AppModel) throws -> PanelRow {
        let about = try XCTUnwrap(model.panelCategories().first { $0.id == "about" })
        return try XCTUnwrap(about.rows.first { $0.id == "update" })
    }

    func testUnsignedAboutUpdateOffersTheReleasePage() throws {
        let model = try model(signed: false)
        let row = try aboutUpdate(model)
        XCTAssertEqual(row.title, TU.text(.openReleasePage))
        XCTAssertEqual(row.detail, TU.text(.manualReleaseDetail))
        XCTAssertEqual(row.symbol, "safari")
        guard case .action(false, _) = row.kind else { return XCTFail("the unsigned release has no manual action") }
        XCTAssertNil(model.updateProgress)
        XCTAssertNil(model.readyUpdate)
    }

    func testSignedAboutUpdateKeepsTheVerifiedInstallAction() throws {
        let model = try model(signed: true)
        let row = try aboutUpdate(model)
        XCTAssertEqual(row.title, T.format(.updateTo, "9.9.9"))
        XCTAssertEqual(row.detail, model.canReplaceItself ? TU.text(.stateAvailableDetail) : T.text(.updateNotWritable))
        XCTAssertEqual(row.symbol, "arrow.down.circle.fill")
        guard case .action(false, _) = row.kind else { return XCTFail("the signed release has no install action") }
    }

    func testUnsignedRequiredUpdateKeepsTheForkReleasePage() throws {
        let model = try model(signed: false)
        model.updateRequired = TogetherClientTooOld(minimum: "9.9.9", current: "0.2.20", downloadUrl: URL(string: "https://example.com/upstream"))
        let rows = model.updateRequiredRows()
        let update = try XCTUnwrap(rows.first { $0.id == "required-update" })
        XCTAssertEqual(update.title, TU.text(.openReleasePage))
        XCTAssertEqual(update.detail, TU.text(.manualReleaseDetail))
        XCTAssertEqual(update.symbol, "safari")
        XCTAssertFalse(rows.contains { $0.id == "required-download" })
        guard case .action(false, _) = update.kind else { return XCTFail("the unsigned release has no manual action") }
    }

    func testRequiredUpdateWithoutAnAvailableReleaseUsesTheFork() throws {
        let model = try model()
        model.updateRequired = TogetherClientTooOld(minimum: "9.9.9", current: "0.2.20", downloadUrl: URL(string: "https://example.com/upstream"))
        let row = try XCTUnwrap(model.updateRequiredRows().first { $0.id == "required-download" })
        XCTAssertEqual(row.detail, "https://github.com/kristinasvechinskaya365/AnyPS5-ARM/releases")
        XCTAssertEqual(row.symbol, "safari")
        guard case .action(false, _) = row.kind else { return XCTFail("the fork releases page has no manual action") }
    }

    func testInvalidRepositoryMetadataHasNoExternalFallback() throws {
        let model = try model()
        model.updateRequired = TogetherClientTooOld(minimum: "9.9.9", current: "0.2.20", downloadUrl: URL(string: "https://example.com/upstream"))
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("AnyPS5-invalid-release-\(UUID().uuidString).bundle")
        let contents = folder.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let info: [String: Any] = ["CFBundleIdentifier": "org.anyps5.tests.invalid-release", "CFBundlePackageType": "BNDL", "AnyPS5Repository": "owner/repo/extra"]
        let data = try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
        try data.write(to: contents.appendingPathComponent("Info.plist"))
        let bundle = try XCTUnwrap(Bundle(url: folder))
        XCTAssertNil(ReleaseRepository.configured(in: bundle))
        XCTAssertFalse(model.updateRequiredRows(bundle: bundle).contains { $0.id == "required-download" })
    }
}
