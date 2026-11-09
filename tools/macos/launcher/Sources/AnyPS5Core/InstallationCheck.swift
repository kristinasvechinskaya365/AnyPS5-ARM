import Foundation

public enum InstallationCheck {
    public struct Report: Codable, Equatable, Sendable {
        public let schemaVersion: Int
        public let repository: String
        public let revision: String
        public let launcherVersion: String
        public let toolkitVersion: String
        public let libraryCount: Int
        public let resourceBundles: [String]
    }

    public static func inspect(bundle: Bundle = .main) throws -> Report {
        let manager = FileManager.default
        guard bundle.bundleIdentifier == "org.anyps5.launcher",
              let resources = bundle.resourceURL,
              let version = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
              !version.isEmpty else {
            throw InstallError("the launcher bundle identity or version is missing")
        }
        guard let repository = bundle.object(forInfoDictionaryKey: "AnyPS5Repository") as? String,
              ReleaseRepository(repository) != nil,
              let revision = bundle.object(forInfoDictionaryKey: "AnyPS5Commit") as? String,
              revision.range(of: "\\A[0-9a-f]{40}\\z", options: .regularExpression) != nil else {
            throw InstallError("the launcher source identity is missing or invalid")
        }
        let bundleRoot = bundle.bundleURL.resolvingSymlinksInPath().path + "/"
        func required(_ file: URL, executable: Bool = false) throws {
            let resolved = file.resolvingSymlinksInPath()
            guard resolved.path.hasPrefix(bundleRoot),
                  try manager.attributesOfItem(atPath: resolved.path)[.type] as? FileAttributeType == .typeRegular,
                  (try manager.attributesOfItem(atPath: resolved.path)[.size] as? NSNumber)?.uint64Value ?? 0 > 0,
                  !executable || manager.isExecutableFile(atPath: resolved.path) else {
                throw InstallError("missing or invalid bundled file: \(file.lastPathComponent)")
            }
        }
        try required(bundle.bundleURL.appendingPathComponent("Contents/MacOS/anyps5-tool"), executable: true)
        for name in ["fonts/NotoSans-Regular.ttf", "fonts/OFL.txt", "browser-pad/index.html", "l10n/en.json", "licenses/AnyPS5-LICENSE.txt"] {
            try required(resources.appendingPathComponent(name))
        }
        for name in ["WebRTC", "LiveKitWebRTC", "RustLiveKitUniFFI"] {
            try required(bundle.bundleURL.appendingPathComponent("Contents/Frameworks/\(name).framework/\(name)"), executable: true)
        }
        let resourceBundles = try manager.contentsOfDirectory(at: resources, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "bundle" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
        guard resourceBundles.contains(where: { $0.lastPathComponent == "LiveKit_LiveKit.bundle" }) else {
            throw InstallError("the LiveKit resource bundle is missing")
        }
        for resource in resourceBundles {
            guard resource.resolvingSymlinksInPath().path.hasPrefix(bundleRoot), Bundle(url: resource) != nil else {
                throw InstallError("invalid resource bundle: \(resource.lastPathComponent)")
            }
        }
        let toolkit = Toolkit(root: resources.appendingPathComponent("toolkit"))
        if let problem = Toolkit.layoutProblem(at: toolkit.root) { throw InstallError("the engine toolkit has \(problem)") }
        try required(toolkit.relinker, executable: true)
        let libraries = try manager.contentsOfDirectory(at: toolkit.libraries, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "prx" }
        for library in libraries { try required(library) }
        for name in ["libvulkan.1.dylib", "libMoltenVK.dylib", "MoltenVK_icd.json"] {
            try required(toolkit.vulkan.appendingPathComponent(name))
        }
        guard let fingerprint = Toolkit.fingerprint(of: toolkit.root), toolkit.version.hasSuffix("-" + fingerprint) else {
            throw InstallError("the engine toolkit does not match its recorded fingerprint")
        }
        return Report(schemaVersion: 1, repository: repository, revision: revision, launcherVersion: version,
                      toolkitVersion: toolkit.version, libraryCount: libraries.count,
                      resourceBundles: resourceBundles.map(\.lastPathComponent))
    }
}
