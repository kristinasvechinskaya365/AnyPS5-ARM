import XCTest
@testable import AnyPS5Core

private final class RepositoryStub: URLProtocol, @unchecked Sendable {
    struct State {
        var status = 200
        var requests: [URLRequest] = []
    }

    static let state = Locked(State())

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let status = Self.state.withLock { state in
            state.requests.append(request)
            return state.status
        }
        let url = request.url!
        let repository = url.path.split(separator: "/").dropFirst().prefix(2).joined(separator: "/")
        let page = "https://github.com/\(repository)/releases/tag/v9.9.9-arm"
        let download = "https://github.com/\(repository)/releases/download/v9.9.9-arm"
        let release: [String: Any] = [
            "tag_name": "v9.9.9-arm", "html_url": page, "draft": false, "prerelease": false,
            "assets": [
                ["name": "AnyPS5-macOS.zip", "browser_download_url": download + "/AnyPS5-macOS.zip", "size": 1],
                ["name": "AnyPS5-toolkit-macos-9.9.9-arm.zip", "browser_download_url": download + "/engine.zip", "size": 1],
            ],
        ]
        let body: Any = url.path.hasSuffix("/latest") ? release : [release]
        let data = try! JSONSerialization.data(withJSONObject: body)
        let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

final class ReleaseRepositoryTests: XCTestCase {
    var session: URLSession!
    var folder: URL!

    override func setUpWithError() throws {
        RepositoryStub.state.value = RepositoryStub.State()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [RepositoryStub.self]
        session = URLSession(configuration: configuration)
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("AnyPS5-release-source-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        session.invalidateAndCancel()
        try FileManager.default.removeItem(at: folder)
    }

    func bundle(_ repository: Any? = nil) throws -> Bundle {
        let root = folder.appendingPathComponent("\(UUID().uuidString).bundle")
        let contents = root.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        var info: [String: Any] = ["CFBundleIdentifier": "org.anyps5.tests.release.\(UUID().uuidString)", "CFBundlePackageType": "BNDL"]
        if let repository { info["AnyPS5Repository"] = repository }
        let data = try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
        try data.write(to: contents.appendingPathComponent("Info.plist"))
        return try XCTUnwrap(Bundle(url: root))
    }

    func requests(_ urls: [String], file: StaticString = #filePath, line: UInt = #line) {
        let requests = RepositoryStub.state.value.requests
        XCTAssertEqual(requests.compactMap { $0.url?.absoluteString }, urls, file: file, line: line)
        for request in requests {
            XCTAssertEqual(request.url?.host, "api.github.com", file: file, line: line)
            XCTAssertEqual(request.httpMethod, "GET", file: file, line: line)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "application/vnd.github+json", file: file, line: line)
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"), file: file, line: line)
            XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalCacheData, file: file, line: line)
            XCTAssertEqual(request.timeoutInterval, 20, file: file, line: line)
        }
    }

    func testStableLookupDefaultsToTheFork() async throws {
        let result = await Updates.lookup(current: "0.2.20", session: session, bundle: try bundle())
        guard case .newer(let release) = result else { return XCTFail("the fork release was not offered: \(result)") }
        XCTAssertEqual(release.tag, "v9.9.9-arm")
        XCTAssertEqual(release.page.absoluteString, "https://github.com/kristinasvechinskaya365/AnyPS5-ARM/releases/tag/v9.9.9-arm")
        requests(["https://api.github.com/repos/kristinasvechinskaya365/AnyPS5-ARM/releases/latest"])
    }

    func testNightlyLookupDefaultsToTheFork() async throws {
        let result = await Updates.lookup(current: "0.2.20", channel: .nightly, session: session, bundle: try bundle())
        guard case .newer(let release) = result else { return XCTFail("the fork release was not offered: \(result)") }
        XCTAssertEqual(release.tag, "v9.9.9-arm")
        requests(["https://api.github.com/repos/kristinasvechinskaya365/AnyPS5-ARM/releases?per_page=30"])
    }

    func testEngineCatalogueDefaultsToTheFork() async throws {
        let entries = await EngineCatalogue.fetch(session: session, bundle: try bundle())
        XCTAssertEqual(entries?.map(\.release), ["9.9.9-arm"])
        requests(["https://api.github.com/repos/kristinasvechinskaya365/AnyPS5-ARM/releases?per_page=40"])
    }

    func testBundleRepositoryControlsEveryReleaseLookup() async throws {
        let bundle = try bundle("Example-Team/Custom_Engine.v2")
        XCTAssertEqual(ReleaseRepository.configured(in: bundle)?.name, "Example-Team/Custom_Engine.v2")
        let stable = await Updates.check(current: "0.2.20", session: session, bundle: bundle)
        XCTAssertEqual(stable?.tag, "v9.9.9-arm")
        let nightly = await Updates.lookup(current: "0.2.20", channel: .nightly, session: session, bundle: bundle)
        guard case .newer = nightly else { return XCTFail("the configured nightly lookup failed") }
        let entries = await EngineCatalogue.fetch(session: session, bundle: bundle)
        XCTAssertEqual(entries?.count, 1)
        requests([
            "https://api.github.com/repos/Example-Team/Custom_Engine.v2/releases/latest",
            "https://api.github.com/repos/Example-Team/Custom_Engine.v2/releases?per_page=30",
            "https://api.github.com/repos/Example-Team/Custom_Engine.v2/releases?per_page=40",
        ])
    }

    func testMalformedRepositoryMakesNoRequests() async throws {
        for value in ["", "owner", "/repo", "owner/", "owner/repo/extra", "../repo", "owner/..", "owner/.",
                      "owner/a b", "owner/repo?per_page=1", "owner/repo#fragment", "owner/repo%2Freleases",
                      "https://github.com/owner/repo", "owner\\repo", "\nowner/repo", "owner/répô"] {
            let bundle = try bundle(value)
            XCTAssertNil(ReleaseRepository.configured(in: bundle), value)
            let stable = await Updates.lookup(current: "0.2.20", session: session, bundle: bundle)
            let nightly = await Updates.lookup(current: "0.2.20", channel: .nightly, session: session, bundle: bundle)
            let entries = await EngineCatalogue.fetch(session: session, bundle: bundle)
            XCTAssertEqual(stable, .failed, value)
            XCTAssertEqual(nightly, .failed, value)
            XCTAssertNil(entries, value)
        }
        requests([])
    }

    func testNonStringRepositoryMakesNoRequests() async throws {
        let values: [Any] = [true, 7, ["owner/repo"], ["repository": "owner/repo"]]
        for value in values {
            let bundle = try bundle(value)
            XCTAssertNil(ReleaseRepository.configured(in: bundle))
            let stable = await Updates.lookup(current: "0.2.20", session: session, bundle: bundle)
            let nightly = await Updates.lookup(current: "0.2.20", channel: .nightly, session: session, bundle: bundle)
            let entries = await EngineCatalogue.fetch(session: session, bundle: bundle)
            XCTAssertEqual(stable, .failed)
            XCTAssertEqual(nightly, .failed)
            XCTAssertNil(entries)
        }
        requests([])
    }

    func testHTTPFailuresNeverFallBackToUpstream() async throws {
        let bundle = try bundle()
        for status in [403, 404, 500] {
            RepositoryStub.state.value = RepositoryStub.State(status: status)
            let stable = await Updates.lookup(current: "0.2.20", session: session, bundle: bundle)
            let nightly = await Updates.lookup(current: "0.2.20", channel: .nightly, session: session, bundle: bundle)
            let entries = await EngineCatalogue.fetch(session: session, bundle: bundle)
            XCTAssertEqual(stable, .failed)
            XCTAssertEqual(nightly, .failed)
            XCTAssertNil(entries)
            requests([
                "https://api.github.com/repos/kristinasvechinskaya365/AnyPS5-ARM/releases/latest",
                "https://api.github.com/repos/kristinasvechinskaya365/AnyPS5-ARM/releases?per_page=30",
                "https://api.github.com/repos/kristinasvechinskaya365/AnyPS5-ARM/releases?per_page=40",
            ])
        }
    }

    func testUnsignedForkUpdateIsRefusedBeforeDownloading() async throws {
        let result = await Updates.lookup(current: "0.2.20", session: session, bundle: try bundle())
        guard case .newer(let release) = result else { return XCTFail("the fork release was not offered") }
        XCTAssertNil(release.signature)
        do {
            _ = try await Updates.prepare(release, bundleIdentifier: "org.anyps5.launcher", current: "0.2.20", session: session)
            XCTFail("an unsigned fork update was accepted")
        } catch let failure as Updates.Failure {
            XCTAssertEqual(failure.description, "AnyPS5 9.9.9 is not signed, so it was not installed; get it from https://github.com/kristinasvechinskaya365/AnyPS5-ARM/releases/tag/v9.9.9-arm only if you trust it")
        }
        requests(["https://api.github.com/repos/kristinasvechinskaya365/AnyPS5-ARM/releases/latest"])
    }

    func testUnsignedForkEngineIsStillRefused() async throws {
        let entries = await EngineCatalogue.fetch(session: session, bundle: try bundle())
        let entry = try XCTUnwrap(entries?.first)
        XCTAssertEqual(entry.trust, .unsignedRefused)
        requests(["https://api.github.com/repos/kristinasvechinskaya365/AnyPS5-ARM/releases?per_page=40"])
    }
}
