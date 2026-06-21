import Foundation

public struct MemoryCaptureRequest: Codable, Equatable, Sendable {
    public let text: String
    public let title: String?
    public let sourceType: String
    public let sourceURL: String
    public let sourceID: String
    public let tags: [String]
    public let idempotencyKey: String

    enum CodingKeys: String, CodingKey {
        case text
        case title
        case sourceType = "source_type"
        case sourceURL = "source_url"
        case sourceID = "source_id"
        case tags
        case idempotencyKey = "idempotency_key"
    }

    public init(
        text: String,
        title: String? = nil,
        sourceType: String = "mobile",
        sourceURL: String = "",
        sourceID: String = "",
        tags: [String] = [],
        idempotencyKey: String
    ) {
        self.text = text
        self.title = title
        self.sourceType = sourceType
        self.sourceURL = sourceURL
        self.sourceID = sourceID
        self.tags = tags
        self.idempotencyKey = idempotencyKey
    }
}

public struct MemoryCaptureResponse: Codable, Equatable, Sendable {
    public let status: String
    public let title: String
    public let relativePath: String?
    public let memoryID: String
    public let indexReason: String?
    public let indexed: Bool?

    enum CodingKeys: String, CodingKey {
        case status
        case title
        case relativePath = "relative_path"
        case memoryID = "memory_id"
        case indexReason = "index_reason"
        case indexed
    }
}

public struct RecallRequest: Codable, Equatable, Sendable {
    public let query: String
    public let topK: Int

    enum CodingKeys: String, CodingKey {
        case query
        case topK = "top_k"
    }

    public init(query: String, topK: Int = 5) {
        self.query = query
        self.topK = topK
    }
}

public struct RecallResult: Codable, Equatable, Sendable {
    public let score: Double
    public let sourceType: String
    public let memoryID: String
    public let title: String
    public let snippet: String
    public let relativePath: String
    public let updatedAt: String
    public let sourceURL: String
    public let tags: [String]

    enum CodingKeys: String, CodingKey {
        case score
        case sourceType = "source_type"
        case memoryID = "memory_id"
        case title
        case snippet
        case relativePath = "relative_path"
        case updatedAt = "updated_at"
        case sourceURL = "source_url"
        case tags
    }
}

public struct RecallResponse: Codable, Equatable, Sendable {
    public let results: [RecallResult]
}

public enum SaharaMobileAPIError: LocalizedError, Equatable {
    case invalidEndpoint
    case invalidResponse
    case transport(String)
    case serverError(statusCode: Int, code: String?, message: String?)

    public var errorDescription: String? {
        switch self {
        case .invalidEndpoint:
            return "The paired endpoint is invalid."
        case .invalidResponse:
            return "The Sahara mobile API returned an invalid response."
        case .transport(let message):
            return "The request could not be completed: \(message)"
        case .serverError(let statusCode, let code, let message):
            return "Sahara mobile API error \(statusCode): \(code ?? "unknown_error") - \(message ?? "No message")"
        }
    }
}

private struct ErrorEnvelope: Decodable {
    struct APIErrorBody: Decodable {
        let code: String
        let message: String
    }

    let error: APIErrorBody
}

public final class SaharaMobileAPIClient {
    private let session: URLSession
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    public init(session: URLSession = .shared) {
        self.session = session
    }

    public func capture(
        _ request: MemoryCaptureRequest,
        using configuration: PairedDeviceConfiguration
    ) async throws -> MemoryCaptureResponse {
        try await send(
            request,
            path: "/v1/memories",
            using: configuration,
            responseType: MemoryCaptureResponse.self
        )
    }

    public func recall(
        _ request: RecallRequest,
        using configuration: PairedDeviceConfiguration
    ) async throws -> RecallResponse {
        try await send(
            request,
            path: "/v1/recall",
            using: configuration,
            responseType: RecallResponse.self
        )
    }

    private func send<RequestBody: Encodable, ResponseBody: Decodable>(
        _ body: RequestBody,
        path: String,
        using configuration: PairedDeviceConfiguration,
        responseType: ResponseBody.Type
    ) async throws -> ResponseBody {
        let url = try endpointURL(base: configuration.endpoint, path: path)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(configuration.token)", forHTTPHeaderField: "Authorization")
        request.httpBody = try encoder.encode(body)

        let responseData: Data
        let response: URLResponse
        do {
            (responseData, response) = try await session.data(for: request)
        } catch {
            throw SaharaMobileAPIError.transport(error.localizedDescription)
        }

        guard let httpResponse = response as? HTTPURLResponse else {
            throw SaharaMobileAPIError.invalidResponse
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            let envelope = try? decoder.decode(ErrorEnvelope.self, from: responseData)
            throw SaharaMobileAPIError.serverError(
                statusCode: httpResponse.statusCode,
                code: envelope?.error.code,
                message: envelope?.error.message
            )
        }

        do {
            return try decoder.decode(responseType, from: responseData)
        } catch {
            throw SaharaMobileAPIError.invalidResponse
        }
    }

    private func endpointURL(base: URL, path: String) throws -> URL {
        guard base.scheme != nil, base.host != nil else {
            throw SaharaMobileAPIError.invalidEndpoint
        }
        var url = base
        for component in path.split(separator: "/") {
            url.appendPathComponent(String(component))
        }
        return url
    }
}
