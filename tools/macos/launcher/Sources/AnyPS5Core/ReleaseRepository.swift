import Foundation

public struct ReleaseRepository: Equatable, Sendable {
    public static let defaultName = "kristinasvechinskaya365/AnyPS5-ARM"
    public static let bundleKey = "AnyPS5Repository"
    public let name: String

    public init?(_ name: String) {
        let parts = name.split(separator: "/", omittingEmptySubsequences: false)
        let allowed = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-".utf8)
        guard parts.count == 2, parts.allSatisfy({ part in
            !part.isEmpty && part != "." && part != ".." && part.utf8.allSatisfy(allowed.contains)
        }) else { return nil }
        self.name = name
    }

    public static func configured(in bundle: Bundle = .main) -> ReleaseRepository? {
        guard let value = bundle.object(forInfoDictionaryKey: bundleKey) else { return ReleaseRepository(defaultName) }
        guard let name = value as? String else { return nil }
        return ReleaseRepository(name)
    }

    public var latestURL: URL { URL(string: "https://api.github.com/repos/\(name)/releases/latest")! }
    public var releasesPageURL: URL { URL(string: "https://github.com/\(name)/releases")! }

    public func releasesURL(perPage: Int) -> URL {
        URL(string: "https://api.github.com/repos/\(name)/releases?per_page=\(perPage)")!
    }
}
