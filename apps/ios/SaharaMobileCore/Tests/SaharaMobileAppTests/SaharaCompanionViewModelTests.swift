import Foundation
import SaharaMobileCore
import XCTest
@testable import SaharaMobileApp

@MainActor
final class SaharaCompanionViewModelTests: XCTestCase {
    func testImportPairingStoresConfiguration() async throws {
        let store = InMemoryDeviceConfigurationStore()
        let outbox = FakeOutbox()
        let viewModel = SaharaCompanionViewModel(
            configurationStore: store,
            outbox: outbox,
            apiClient: FakeAPIClient()
        )
        viewModel.pairingInput = pairingJSON(scopes: [.memoryCapture, .memoryRecall])

        await viewModel.importPairing()

        XCTAssertEqual(viewModel.pairedDevice?.name, "Nidheesh iPhone")
        XCTAssertEqual(viewModel.pairedDevice?.scopes, [.memoryCapture, .memoryRecall])
        XCTAssertEqual(try store.load()?.deviceID, "device-123")
        XCTAssertEqual(viewModel.statusMessage, "Paired Nidheesh iPhone")
    }

    func testCaptureWithoutPairingQueuesOffline() async {
        let outbox = FakeOutbox()
        let viewModel = SaharaCompanionViewModel(
            configurationStore: InMemoryDeviceConfigurationStore(),
            outbox: outbox,
            apiClient: FakeAPIClient(),
            idempotencyKey: { "fixed-key" }
        )
        viewModel.captureText = "Remember the hotel was Hyatt Place."
        viewModel.captureTags = "travel, reno"

        await viewModel.saveCapture()

        XCTAssertEqual(outbox.entries.count, 1)
        XCTAssertEqual(outbox.entries[0].request.idempotencyKey, "fixed-key")
        XCTAssertEqual(outbox.entries[0].request.tags, ["travel", "reno"])
        XCTAssertEqual(viewModel.pendingCaptures.count, 1)
        XCTAssertEqual(viewModel.captureText, "")
    }

    func testCaptureUsesAPIWhenPairedForCapture() async throws {
        let configuration = pairedConfiguration(scopes: [.memoryCapture])
        let store = InMemoryDeviceConfigurationStore(configuration: configuration)
        let apiClient = FakeAPIClient()
        let viewModel = SaharaCompanionViewModel(
            configurationStore: store,
            outbox: FakeOutbox(),
            apiClient: apiClient,
            idempotencyKey: { "online-key" }
        )
        await viewModel.refresh()
        viewModel.captureText = "Remember the garage code changed."
        viewModel.captureTitle = "Garage code"

        await viewModel.saveCapture()

        XCTAssertEqual(apiClient.capturedRequests.count, 1)
        XCTAssertEqual(apiClient.capturedRequests[0].title, "Garage code")
        XCTAssertEqual(apiClient.capturedRequests[0].idempotencyKey, "online-key")
        XCTAssertEqual(viewModel.statusMessage, "Captured Garage code")
    }

    func testRecallRequiresRecallScope() async throws {
        let configuration = pairedConfiguration(scopes: [.memoryCapture])
        let viewModel = SaharaCompanionViewModel(
            configurationStore: InMemoryDeviceConfigurationStore(configuration: configuration),
            outbox: FakeOutbox(),
            apiClient: FakeAPIClient()
        )
        await viewModel.refresh()
        viewModel.recallQuery = "garage code"

        await viewModel.runRecall()

        XCTAssertEqual(viewModel.errorMessage, "This paired device does not have recall access.")
    }

    func testRecallSurfacesResults() async throws {
        let configuration = pairedConfiguration(scopes: [.memoryCapture, .memoryRecall])
        let apiClient = FakeAPIClient()
        apiClient.recallResponse = RecallResponse(results: [
            RecallResult(
                score: 0.91,
                sourceType: "mobile",
                memoryID: "memory-1",
                title: "Garage code",
                snippet: "The garage code changed.",
                relativePath: "2026/07/garage-code.md",
                updatedAt: "2026-07-31T00:00:00Z",
                sourceURL: "",
                tags: ["home"]
            )
        ])
        let viewModel = SaharaCompanionViewModel(
            configurationStore: InMemoryDeviceConfigurationStore(configuration: configuration),
            outbox: FakeOutbox(),
            apiClient: apiClient
        )
        await viewModel.refresh()
        viewModel.recallQuery = "garage code"

        await viewModel.runRecall()

        XCTAssertEqual(viewModel.recallResults.count, 1)
        XCTAssertEqual(viewModel.recallResults[0].title, "Garage code")
        XCTAssertEqual(viewModel.statusMessage, "Found 1 memories.")
    }

    private func pairingJSON(scopes: [SaharaScope]) -> String {
        let payload = SaharaPairingPayload(
            version: 1,
            type: "sahara-mobile-pairing",
            deviceID: "device-123",
            name: "Nidheesh iPhone",
            endpoint: URL(string: "https://desktop.tailnet.ts.net")!,
            token: "token-123",
            scopes: scopes
        )
        let data = try! JSONEncoder().encode(payload)
        return String(decoding: data, as: UTF8.self)
    }

    private func pairedConfiguration(scopes: [SaharaScope]) -> PairedDeviceConfiguration {
        PairedDeviceConfiguration(
            deviceID: "device-123",
            name: "Nidheesh iPhone",
            endpoint: URL(string: "https://desktop.tailnet.ts.net")!,
            token: "token-123",
            scopes: scopes
        )
    }
}

private final class FakeOutbox: CaptureOutboxProviding, @unchecked Sendable {
    var entries: [PendingCapture] = []

    func list() async throws -> [PendingCapture] {
        entries
    }

    func enqueue(_ request: MemoryCaptureRequest) async throws -> PendingCapture {
        let entry = PendingCapture(request: request)
        entries.append(entry)
        return entry
    }
}

private final class FakeAPIClient: SaharaMobileAPIProviding, @unchecked Sendable {
    var capturedRequests: [MemoryCaptureRequest] = []
    var recallResponse = RecallResponse(results: [])

    func capture(
        _ request: MemoryCaptureRequest,
        using configuration: PairedDeviceConfiguration
    ) async throws -> MemoryCaptureResponse {
        capturedRequests.append(request)
        return MemoryCaptureResponse(
            status: "saved_and_indexed",
            title: request.title ?? "Untitled capture",
            relativePath: "2026/07/capture.md",
            memoryID: "memory-123",
            indexReason: nil,
            indexed: true
        )
    }

    func recall(
        _ request: RecallRequest,
        using configuration: PairedDeviceConfiguration
    ) async throws -> RecallResponse {
        recallResponse
    }
}
