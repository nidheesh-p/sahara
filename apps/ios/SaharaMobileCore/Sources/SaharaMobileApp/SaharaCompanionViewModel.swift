import Combine
import Foundation
import SaharaMobileCore

public protocol SaharaMobileAPIProviding: Sendable {
    func capture(
        _ request: MemoryCaptureRequest,
        using configuration: PairedDeviceConfiguration
    ) async throws -> MemoryCaptureResponse

    func recall(
        _ request: RecallRequest,
        using configuration: PairedDeviceConfiguration
    ) async throws -> RecallResponse
}

extension SaharaMobileAPIClient: SaharaMobileAPIProviding {}
extension SaharaMobileAPIClient: @unchecked Sendable {}

public protocol CaptureOutboxProviding: Sendable {
    func list() async throws -> [PendingCapture]
    func enqueue(_ request: MemoryCaptureRequest) async throws -> PendingCapture
}

extension EncryptedCaptureOutboxStore: CaptureOutboxProviding {
    public func enqueue(_ request: MemoryCaptureRequest) async throws -> PendingCapture {
        try enqueue(request, now: Date())
    }
}

@MainActor
public final class SaharaCompanionViewModel: ObservableObject {
    @Published public private(set) var pairedDevice: PairedDeviceConfiguration?
    @Published public private(set) var pendingCaptures: [PendingCapture] = []
    @Published public private(set) var recallResults: [RecallResult] = []
    @Published public private(set) var statusMessage: String?
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var isWorking = false

    @Published public var pairingInput = ""
    @Published public var captureText = ""
    @Published public var captureTitle = ""
    @Published public var captureTags = ""
    @Published public var recallQuery = ""

    private let configurationStore: DeviceConfigurationStore
    private let outbox: CaptureOutboxProviding
    private let apiClient: SaharaMobileAPIProviding
    private let idempotencyKey: () -> String

    public init(
        configurationStore: DeviceConfigurationStore,
        outbox: CaptureOutboxProviding,
        apiClient: SaharaMobileAPIProviding = SaharaMobileAPIClient(),
        idempotencyKey: @escaping () -> String = { UUID().uuidString }
    ) {
        self.configurationStore = configurationStore
        self.outbox = outbox
        self.apiClient = apiClient
        self.idempotencyKey = idempotencyKey
    }

    public var canCaptureOnline: Bool {
        pairedDevice?.scopes.contains(.memoryCapture) == true
    }

    public var canRecall: Bool {
        pairedDevice?.scopes.contains(.memoryRecall) == true
    }

    public func refresh() async {
        clearMessages()
        do {
            pairedDevice = try configurationStore.load()
            pendingCaptures = try await outbox.list()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    public func importPairing() async {
        clearMessages()
        do {
            let configuration = try SaharaPairingImporter.configuration(from: pairingInput)
            try configurationStore.save(configuration)
            pairedDevice = configuration
            pairingInput = ""
            statusMessage = "Paired \(configuration.name)"
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    public func clearPairing() async {
        clearMessages()
        do {
            try configurationStore.clear()
            pairedDevice = nil
            recallResults = []
            statusMessage = "Pairing removed"
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    public func saveCapture() async {
        let text = captureText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            errorMessage = "Capture text is required."
            return
        }

        clearMessages()
        isWorking = true
        defer { isWorking = false }

        let request = MemoryCaptureRequest(
            text: text,
            title: normalizedOptional(captureTitle),
            tags: normalizedTags(captureTags),
            idempotencyKey: idempotencyKey()
        )

        guard let configuration = pairedDevice, configuration.scopes.contains(.memoryCapture) else {
            await enqueueOffline(request, status: "Saved to outbox until this iPhone is paired.")
            return
        }

        do {
            let response = try await apiClient.capture(request, using: configuration)
            captureText = ""
            captureTitle = ""
            captureTags = ""
            statusMessage = "Captured \(response.title)"
        } catch {
            await enqueueOffline(request, status: "Desktop unreachable. Saved to outbox for retry.")
            errorMessage = error.localizedDescription
        }
    }

    public func runRecall() async {
        let query = recallQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else {
            errorMessage = "Recall query is required."
            return
        }
        guard let configuration = pairedDevice, configuration.scopes.contains(.memoryRecall) else {
            errorMessage = "This paired device does not have recall access."
            return
        }

        clearMessages()
        isWorking = true
        defer { isWorking = false }

        do {
            let response = try await apiClient.recall(RecallRequest(query: query), using: configuration)
            recallResults = response.results
            statusMessage = response.results.isEmpty ? "No memories matched." : "Found \(response.results.count) memories."
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func enqueueOffline(_ request: MemoryCaptureRequest, status: String) async {
        do {
            _ = try await outbox.enqueue(request)
            pendingCaptures = try await outbox.list()
            captureText = ""
            captureTitle = ""
            captureTags = ""
            statusMessage = status
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func clearMessages() {
        statusMessage = nil
        errorMessage = nil
    }

    private func normalizedOptional(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func normalizedTags(_ value: String) -> [String] {
        value.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }
}
