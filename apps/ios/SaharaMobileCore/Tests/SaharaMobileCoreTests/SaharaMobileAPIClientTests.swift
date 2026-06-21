import Foundation
import XCTest
@testable import SaharaMobileCore

final class SaharaMobileAPIClientTests: XCTestCase {
    override func tearDown() {
        MockURLProtocol.requestHandler = nil
        super.tearDown()
    }

    func testCapturePostsJSONWithBearerToken() async throws {
        let session = makeSession()
        let client = SaharaMobileAPIClient(session: session)
        let configuration = PairedDeviceConfiguration(
            deviceID: "device-123",
            name: "Nidheesh iPhone",
            endpoint: URL(string: "https://desktop.tailnet.ts.net")!,
            token: "sahara_token",
            scopes: [.memoryCapture]
        )

        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.absoluteString, "https://desktop.tailnet.ts.net/v1/memories")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer sahara_token")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")

            let body = try XCTUnwrap(self.requestBody(from: request))
            let decoded = try JSONDecoder().decode(MemoryCaptureRequest.self, from: body)
            XCTAssertEqual(decoded.text, "Hotel was Hyatt Place")
            XCTAssertEqual(decoded.tags, ["travel"])
            XCTAssertEqual(decoded.idempotencyKey, "capture-123")

            let response = HTTPURLResponse(
                url: try XCTUnwrap(request.url),
                statusCode: 201,
                httpVersion: nil,
                headerFields: nil
            )!
            let data = Data(
                """
                {"status":"saved_and_indexed","title":"Hotel was Hyatt Place","relative_path":"2026/06/hotel.md","memory_id":"memory-123","indexed":true}
                """.utf8
            )
            return (response, data)
        }

        let response = try await client.capture(
            MemoryCaptureRequest(
                text: "Hotel was Hyatt Place",
                tags: ["travel"],
                idempotencyKey: "capture-123"
            ),
            using: configuration
        )

        XCTAssertEqual(response.status, "saved_and_indexed")
        XCTAssertEqual(response.memoryID, "memory-123")
        XCTAssertEqual(response.relativePath, "2026/06/hotel.md")
        XCTAssertEqual(response.indexed, true)
    }

    func testRecallSurfacesStructuredServerErrors() async throws {
        let session = makeSession()
        let client = SaharaMobileAPIClient(session: session)
        let configuration = PairedDeviceConfiguration(
            deviceID: "device-123",
            name: "Nidheesh iPhone",
            endpoint: URL(string: "https://desktop.tailnet.ts.net")!,
            token: "bad_token",
            scopes: [.memoryRecall]
        )

        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: try XCTUnwrap(request.url),
                statusCode: 401,
                httpVersion: nil,
                headerFields: nil
            )!
            let data = Data(
                """
                {"error":{"code":"missing_token","message":"Missing bearer token"}}
                """.utf8
            )
            return (response, data)
        }

        do {
            _ = try await client.recall(
                RecallRequest(query: "reno hotel", topK: 3),
                using: configuration
            )
            XCTFail("Expected recall to throw")
        } catch let error as SaharaMobileAPIError {
            XCTAssertEqual(
                error,
                .serverError(
                    statusCode: 401,
                    code: "missing_token",
                    message: "Missing bearer token"
                )
            )
        }
    }

    private func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    private func requestBody(from request: URLRequest) -> Data? {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else {
            return nil
        }
        stream.open()
        defer { stream.close() }

        let bufferSize = 4096
        var buffer = Array(repeating: UInt8(0), count: bufferSize)
        var data = Data()
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: bufferSize)
            if read < 0 {
                return nil
            }
            if read == 0 {
                break
            }
            data.append(buffer, count: read)
        }
        return data
    }
}

private final class MockURLProtocol: URLProtocol {
    nonisolated(unsafe) static var requestHandler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let handler = Self.requestHandler else {
            XCTFail("Missing request handler")
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}
