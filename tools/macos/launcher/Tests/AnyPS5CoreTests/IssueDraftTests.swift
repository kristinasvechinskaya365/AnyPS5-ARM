import Darwin
import XCTest
@testable import AnyPS5Core

final class IssueDraftTests: XCTestCase {
    private var folder: URL!

    override func setUpWithError() throws {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("crash-report-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: folder)
    }

    private func bundle(_ repository: Any) throws -> Bundle {
        let root = folder.appendingPathComponent("\(UUID().uuidString).bundle")
        let contents = root.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        let info: [String: Any] = ["CFBundleIdentifier": "org.anyps5.tests.support.\(UUID().uuidString)",
                                   "CFBundlePackageType": "BNDL", "AnyPS5Repository": repository]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            .write(to: contents.appendingPathComponent("Info.plist"))
        return try XCTUnwrap(Bundle(url: root))
    }

    private let privacy = ReportPrivacy(home: "/Users/ada", names: ["ada", "Ada Lovelace"], secrets: ["s3cr3t-refresh-token-value"])
    private let system = SystemInfo(model: "Mac15,7", chip: "Apple M3 Pro", memoryGB: 18, macOS: "15.6 (24G84)", displayHz: 120)

    /// An .ips crash report as macOS writes it: a JSON header line, then the report.
    private func ips(pid: Int32, signal: String = "SIGSEGV") -> String {
        let header = #"{"app_name":"eboot","timestamp":"2026-10-24 21:46:00.00 +0300","bundleID":"org.anyps5.ppsa01234","name":"eboot"}"#
        let body: [String: Any] = [
            "pid": pid,
            "procPath": "/Users/ada/Games/.AnyPS5/Games/PPSA01234-abc.app/Contents/MacOS/eboot",
            "crashReporterKey": "5D3A-4E1F-SECRET-KEY",
            "sleepWakeUUID": "0F0F0F0F-1111-2222-3333-444444444444",
            "bundleInfo": ["CFBundleIdentifier": "org.anyps5.ppsa01234"],
            "exception": ["type": "EXC_BAD_ACCESS", "signal": signal, "subtype": "KERN_INVALID_ADDRESS at 0x0000000000000010"],
            "faultingThread": 1,
            "threads": [
                ["frames": [["imageOffset": 4096, "imageIndex": 0]]],
                ["frames": [["imageOffset": 74565, "imageIndex": 1, "symbol": "Texture::Upload", "symbolLocation": 52],
                            ["imageOffset": 8192, "imageIndex": 0]]],
            ],
            "usedImages": [["name": "eboot", "base": 4_294_967_296], ["name": "libSceAgcDriver.dylib", "base": 4_311_744_512]],
        ]
        let data = try! JSONSerialization.data(withJSONObject: body, options: [.prettyPrinted, .sortedKeys])
        return header + "\n" + String(decoding: data, as: UTF8.self)
    }

    private func report(session: String = "line one\nline two\n", crash: String? = nil, unlock: String? = nil, diagnosis: Diagnosis? = .crashed("EXC_BAD_ACCESS (SIGSEGV)")) -> SessionReport {
        var settings = GameSettings()
        settings.resolution = "1920x1080"
        settings.frameGeneration = true
        settings.overlay = .topRight
        var report = SessionReport(gameName: "Stray", titleID: "PPSA01234", gameVersion: "01.000.002", settings: settings, system: system,
                                   launcherVersion: "0.2.14", session: session, created: Date(timeIntervalSince1970: 1_792_867_560), privacy: privacy)
        report.unlockedWith = "0.2.13-arm-5c0ffee12345"
        report.engineNow = "0.2.14-arm-0badc0de4321"
        report.diagnosis = diagnosis
        report.crashReport = crash
        report.unlockLog = unlock
        report.started = Date(timeIntervalSince1970: 1_792_866_600)
        report.ended = Date(timeIntervalSince1970: 1_792_867_548)
        return report
    }

    // MARK: The issue

    func testTheIssueIsPrefilledAndFitsTheLinkLimit() throws {
        let long = (1...400).map { "[gpu] line \($0) " + String(repeating: "x", count: 60) + " /Users/ada/Library" }.joined(separator: "\n") + "\nthe very last line"
        let draft = IssueDraft.crash(report(session: long, crash: ips(pid: 9)))
        XCTAssertLessThanOrEqual(draft.url.absoluteString.count, IssueDraft.maximumLength)
        XCTAssertEqual(draft.title, "Crash: Stray (PPSA01234)")
        let components = try XCTUnwrap(URLComponents(url: draft.url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(components.host, "github.com")
        XCTAssertEqual(components.path, "/kristinasvechinskaya365/AnyPS5-ARM/issues/new")
        let query = Dictionary(uniqueKeysWithValues: (components.queryItems ?? []).map { ($0.name, $0.value ?? "") })
        XCTAssertEqual(query["template"], "crash.yml")
        XCTAssertEqual(query["title"], "Crash: Stray (PPSA01234)")
        XCTAssertTrue(query["log"]?.hasSuffix("the very last line") == true, "the newest lines are kept")
        XCTAssertLessThanOrEqual(query["log"]?.split(separator: "\n").count ?? 0, 60)
        XCTAssertTrue(query["system"]?.contains("Apple M3 Pro") == true)
        XCTAssertTrue(query["reason"]?.contains("SIGSEGV") == true)
        XCTAssertFalse(draft.url.absoluteString.contains("ada"), "nothing of the user in the link")
        XCTAssertTrue(draft.preview.contains("the very last line"), "the preview shows what the link sends")
    }

    func testTheIssueTemplateHasEveryFieldTheLinkFills() throws {
        // tools/macos/launcher/Tests/AnyPS5CoreTests → the repository root.
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let file = root.appendingPathComponent(".github/ISSUE_TEMPLATE/crash.yml")
        // The issue forms are AnyPS5-ARM's; the standalone AnyLauncher repository has none to check.
        guard FileManager.default.fileExists(atPath: file.path) else {
            throw XCTSkip("no .github/ISSUE_TEMPLATE/crash.yml: run this in AnyPS5-ARM's tools/macos/launcher")
        }
        let template = try String(contentsOf: file, encoding: .utf8)
        for field in IssueDraft.crash(report()).fields {
            XCTAssertTrue(template.contains("id: \(field.id)\n"), "crash.yml has no field \(field.id)")
        }
    }

    func testAPlusInTheLogIsNotASpace() {
        let draft = IssueDraft.crash(report(session: "a+b=c & d#e\n"))
        XCTAssertTrue(draft.url.absoluteString.contains("a%2Bb%3Dc%20%26%20d%23e"))
    }

    func testOversizedGameNamesFinishWithBoundedLinks() throws {
        let inputName = "issue-draft-overflow-input.json"
        let resultName = "issue-draft-overflow-result.json"
        let directory = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let input = directory.appendingPathComponent(inputName)
        if FileManager.default.fileExists(atPath: input.path) {
            let names = try JSONDecoder().decode([String].self, from: Data(contentsOf: input))
            let newest = "newest preserved line /Users/ada/Library s3cr3t-refresh-token-value"
            let expected = privacy.clean(newest)
            let session = (1...80).map { "old line \($0) " + String(repeating: "x", count: 80) }.joined(separator: "\n") + "\n" + newest
            var completed = 0
            for name in names {
                var sample = report(session: session)
                sample.gameName = name
                let drafts = [(IssueDraft.crash(sample), "crash.yml", "/\(IssueDraft.repository)/issues/new"),
                              (IssueDraft.engine(sample), "bug_report.yml", "/boykopovar/AnyPS5/issues/new")]
                for (draft, template, path) in drafts {
                    XCTAssertLessThanOrEqual(draft.url.absoluteString.count, IssueDraft.maximumLength)
                    let components = try XCTUnwrap(URLComponents(url: draft.url, resolvingAgainstBaseURL: false))
                    let query = Dictionary(uniqueKeysWithValues: (components.queryItems ?? []).map { ($0.name, $0.value ?? "") })
                    XCTAssertEqual(components.host, "github.com")
                    XCTAssertEqual(components.path, path)
                    XCTAssertEqual(query["template"], template)
                    XCTAssertEqual(query["title"], draft.title)
                    XCTAssertTrue(draft.preview.hasPrefix(draft.title + "\n"))
                    for field in draft.fields { XCTAssertEqual(query[field.id], field.value) }
                    XCTAssertTrue(query["log"]?.hasSuffix(expected) == true)
                    XCTAssertTrue(draft.preview.contains(expected))
                    for secret in ["/Users/ada", "s3cr3t-refresh-token-value"] {
                        XCTAssertFalse(draft.preview.contains(secret))
                        XCTAssertFalse(query.values.contains { $0.contains(secret) })
                    }
                    completed += 1
                }
            }
            try JSONEncoder().encode(["completed": completed]).write(to: directory.appendingPathComponent(resultName))
            return
        }
        let names = [String(repeating: "x", count: 8000),
                     String(repeating: "🚀e\u{301}漢", count: 1500),
                     "A" + String(repeating: "\u{301}", count: 8000)]
            .map { "/Users/ada/Library s3cr3t-refresh-token-value " + $0 }
        try JSONEncoder().encode(names).write(to: folder.appendingPathComponent(inputName))
        let log = folder.appendingPathComponent("issue-draft-overflow.log")
        try Data().write(to: log)
        let output = try FileHandle(forWritingTo: log)
        defer { try? output.close() }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        process.arguments = ["-XCTest", "\(NSStringFromClass(IssueDraftTests.self))/testOversizedGameNamesFinishWithBoundedLinks",
                             Bundle(for: IssueDraftTests.self).bundleURL.path]
        process.currentDirectoryURL = folder
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let deadline = ProcessInfo.processInfo.systemUptime + 10
        while process.isRunning && ProcessInfo.processInfo.systemUptime < deadline { Thread.sleep(forTimeInterval: 0.02) }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
            process.waitUntilExit()
            return XCTFail("issue draft child exceeded 10-second limit")
        }
        process.waitUntilExit()
        let diagnostic = try String(contentsOf: log, encoding: .utf8)
        XCTAssertEqual(process.terminationReason, .exit, diagnostic)
        XCTAssertEqual(process.terminationStatus, 0, diagnostic)
        let result = try JSONDecoder().decode([String: Int].self, from: Data(contentsOf: folder.appendingPathComponent(resultName)))
        XCTAssertEqual(result, ["completed": 6])
    }

    func testTheEngineIssueGoesUpstreamWithItsOwnForm() throws {
        let draft = IssueDraft.engine(report())
        let components = try XCTUnwrap(URLComponents(url: draft.url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(components.path, "/boykopovar/AnyPS5/issues/new")
        let names = Set((components.queryItems ?? []).map(\.name))
        // Their form's "title" field is the issue title's parameter too: one value fills both.
        XCTAssertEqual(names, ["template", "title", "summary", "commit", "system", "steps", "log", "notes"])
        XCTAssertEqual(components.queryItems?.first { $0.name == "template" }?.value, "bug_report.yml")
        XCTAssertEqual(draft.title, "Crash: Stray (PPSA01234) 01.000.002")
        XCTAssertLessThanOrEqual(draft.url.absoluteString.count, IssueDraft.maximumLength)
    }

    func testLauncherIssuesUseTheForkByDefault() throws {
        let problem = LauncherProblem(kind: .crash, at: Date(), version: "1", platform: "macOS")
        let draft = IssueDraft.launcherProblem(problem, system: system, privacy: privacy)
        let components = try XCTUnwrap(URLComponents(url: draft.url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(components.host, "github.com")
        XCTAssertEqual(components.path, "/kristinasvechinskaya365/AnyPS5-ARM/issues/new")
        XCTAssertEqual(components.queryItems?.first { $0.name == "template" }?.value, "launcher_crash.yml")
        XCTAssertEqual(IssueDraft.repository, "kristinasvechinskaya365/AnyPS5-ARM")
        XCTAssertEqual(IssueDraft.launcherRepository, IssueDraft.repository)
    }

    func testConfiguredSupportLinksRetainExplicitUpstreamReporting() throws {
        let bundle = try bundle("developer/Custom-Port")
        let problem = LauncherProblem(kind: .crash, at: Date(), version: "1", platform: "macOS")
        let crash = IssueDraft.crash(report(), bundle: bundle)
        let launcher = IssueDraft.launcherProblem(problem, system: system, privacy: privacy, bundle: bundle)
        for draft in [crash, launcher] {
            XCTAssertEqual(draft.url.host, "github.com")
            XCTAssertEqual(draft.url.path, "/developer/Custom-Port/issues/new")
            XCTAssertLessThanOrEqual(draft.url.absoluteString.count, IssueDraft.maximumLength)
        }
        let upstream = IssueDraft.engine(report(), bundle: bundle)
        XCTAssertEqual(upstream.url.path, "/boykopovar/AnyPS5/issues/new")
        XCTAssertTrue(upstream.fields.first { $0.id == "commit" }?.value.contains("github.com/developer/Custom-Port") == true)
    }

    func testInvalidSupportMetadataFallsBackToTheKnownFork() throws {
        let problem = LauncherProblem(kind: .crash, at: Date(), version: "1", platform: "macOS")
        let values: [Any] = ["owner/repo/extra", "https://example.com/repo", ["owner", "repo"], String(repeating: "x", count: 8000) + "/repo"]
        for value in values {
            let bundle = try bundle(value)
            XCTAssertNil(ReleaseRepository.configured(in: bundle))
            let drafts = [IssueDraft.crash(report(), bundle: bundle),
                          IssueDraft.launcherProblem(problem, system: system, privacy: privacy, bundle: bundle)]
            for draft in drafts {
                XCTAssertEqual(draft.url.host, "github.com")
                XCTAssertEqual(draft.url.path, "/kristinasvechinskaya365/AnyPS5-ARM/issues/new")
            }
            let upstream = IssueDraft.engine(report(), bundle: bundle)
            XCTAssertEqual(upstream.url.path, "/boykopovar/AnyPS5/issues/new")
            XCTAssertTrue(upstream.fields.first { $0.id == "commit" }?.value.contains("github.com/kristinasvechinskaya365/AnyPS5-ARM") == true)
        }
    }
}
