import Foundation

public enum SaharaScope: String, Codable, CaseIterable, Hashable, Sendable {
    case memoryCapture = "memory:capture"
    case memoryRecall = "memory:recall"
}

public struct SaharaPairingPayload: Codable, Equatable, Sendable {
    public let version: Int
    public let type: String
    public let deviceID: String
    public let name: String
    public let endpoint: URL
    public let token: String
    public let scopes: [SaharaScope]

    enum CodingKeys: String, CodingKey {
        case version
        case type
        case deviceID = "device_id"
        case name
        case endpoint
        case token
        case scopes
    }

    public init(
        version: Int,
        type: String,
        deviceID: String,
        name: String,
        endpoint: URL,
        token: String,
        scopes: [SaharaScope]
    ) {
        self.version = version
        self.type = type
        self.deviceID = deviceID
        self.name = name
        self.endpoint = endpoint
        self.token = token
        self.scopes = scopes
    }
}

public struct PairedDeviceConfiguration: Codable, Equatable, Identifiable, Sendable {
    public let deviceID: String
    public let name: String
    public let endpoint: URL
    public let token: String
    public let scopes: [SaharaScope]
    public let pairedAt: Date

    public var id: String { deviceID }

    public init(
        deviceID: String,
        name: String,
        endpoint: URL,
        token: String,
        scopes: [SaharaScope],
        pairedAt: Date = Date()
    ) {
        self.deviceID = deviceID
        self.name = name
        self.endpoint = endpoint
        self.token = token
        self.scopes = scopes
        self.pairedAt = pairedAt
    }
}

public enum PairingImportError: LocalizedError, Equatable {
    case emptyInput
    case invalidPairingURI
    case missingPayload
    case invalidPayload
    case unsupportedVersion(Int)
    case unsupportedType(String)
    case missingDeviceID
    case missingName
    case missingToken
    case invalidEndpoint
    case emptyScopes

    public var errorDescription: String? {
        switch self {
        case .emptyInput:
            return "Pairing input is empty."
        case .invalidPairingURI:
            return "The pairing URI is invalid."
        case .missingPayload:
            return "The pairing URI does not contain a payload."
        case .invalidPayload:
            return "The pairing payload could not be decoded."
        case .unsupportedVersion(let version):
            return "Unsupported pairing payload version \(version)."
        case .unsupportedType(let type):
            return "Unsupported pairing payload type \(type)."
        case .missingDeviceID:
            return "The pairing payload is missing a device identifier."
        case .missingName:
            return "The pairing payload is missing a device name."
        case .missingToken:
            return "The pairing payload is missing a bearer token."
        case .invalidEndpoint:
            return "The pairing payload endpoint is invalid."
        case .emptyScopes:
            return "The pairing payload does not contain any scopes."
        }
    }
}

public enum SaharaPairingImporter {
    public static func configuration(
        from input: String,
        pairedAt: Date = Date()
    ) throws -> PairedDeviceConfiguration {
        let payload = try decodePayload(from: input)
        try validate(payload)
        return PairedDeviceConfiguration(
            deviceID: payload.deviceID.trimmingCharacters(in: .whitespacesAndNewlines),
            name: payload.name.trimmingCharacters(in: .whitespacesAndNewlines),
            endpoint: try normalizedEndpoint(payload.endpoint),
            token: payload.token.trimmingCharacters(in: .whitespacesAndNewlines),
            scopes: normalizedScopes(payload.scopes),
            pairedAt: pairedAt
        )
    }

    private static func decodePayload(from input: String) throws -> SaharaPairingPayload {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw PairingImportError.emptyInput
        }

        let data: Data
        if trimmed.hasPrefix("{") {
            data = Data(trimmed.utf8)
        } else {
            guard let components = URLComponents(string: trimmed),
                  components.scheme == "sahara",
                  components.host == "pair" || components.path == "/pair"
            else {
                throw PairingImportError.invalidPairingURI
            }
            guard let payloadValue = components.queryItems?.first(where: { $0.name == "payload" })?.value,
                  !payloadValue.isEmpty
            else {
                throw PairingImportError.missingPayload
            }
            data = Data(payloadValue.utf8)
        }

        do {
            return try JSONDecoder().decode(SaharaPairingPayload.self, from: data)
        } catch {
            throw PairingImportError.invalidPayload
        }
    }

    private static func validate(_ payload: SaharaPairingPayload) throws {
        guard payload.version == 1 else {
            throw PairingImportError.unsupportedVersion(payload.version)
        }
        guard payload.type == "sahara-mobile-pairing" else {
            throw PairingImportError.unsupportedType(payload.type)
        }
        guard !payload.deviceID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PairingImportError.missingDeviceID
        }
        guard !payload.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PairingImportError.missingName
        }
        guard !payload.token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PairingImportError.missingToken
        }
        _ = try normalizedEndpoint(payload.endpoint)
        let scopes = normalizedScopes(payload.scopes)
        guard !scopes.isEmpty else {
            throw PairingImportError.emptyScopes
        }
    }

    private static func normalizedEndpoint(_ endpoint: URL) throws -> URL {
        guard let scheme = endpoint.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              endpoint.host != nil
        else {
            throw PairingImportError.invalidEndpoint
        }
        return endpoint
    }

    private static func normalizedScopes(_ scopes: [SaharaScope]) -> [SaharaScope] {
        var seen = Set<SaharaScope>()
        return scopes.filter { seen.insert($0).inserted }
    }
}
