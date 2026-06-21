import CryptoKit
import Foundation

public enum PendingCaptureState: String, Codable, Equatable, Sendable {
    case pending
    case sending
    case failed
}

public struct PendingCapture: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let request: MemoryCaptureRequest
    public let createdAt: Date
    public let updatedAt: Date
    public let attemptCount: Int
    public let state: PendingCaptureState
    public let lastError: String?

    public init(
        id: UUID = UUID(),
        request: MemoryCaptureRequest,
        createdAt: Date = Date(),
        updatedAt: Date = Date(),
        attemptCount: Int = 0,
        state: PendingCaptureState = .pending,
        lastError: String? = nil
    ) {
        self.id = id
        self.request = request
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.attemptCount = attemptCount
        self.state = state
        self.lastError = lastError
    }
}

public enum CaptureOutboxError: LocalizedError {
    case cryptographicFailure
    case corruptData
    case entryNotFound(UUID)

    public var errorDescription: String? {
        switch self {
        case .cryptographicFailure:
            return "The offline outbox could not be encrypted."
        case .corruptData:
            return "The offline outbox data is corrupt or uses the wrong key."
        case .entryNotFound(let id):
            return "Pending capture \(id.uuidString) was not found."
        }
    }
}

private struct PendingCaptureSnapshot: Codable {
    let entries: [PendingCapture]
}

public actor EncryptedCaptureOutboxStore {
    private let storageURL: URL
    private let keyProvider: @Sendable () throws -> SymmetricKey
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    public init(
        storageURL: URL,
        keyProvider: @escaping @Sendable () throws -> SymmetricKey
    ) {
        self.storageURL = storageURL
        self.keyProvider = keyProvider
    }

    public func list() throws -> [PendingCapture] {
        try loadEntries()
    }

    public func enqueue(
        _ request: MemoryCaptureRequest,
        now: Date = Date()
    ) throws -> PendingCapture {
        var entries = try loadEntries()
        let entry = PendingCapture(request: request, createdAt: now, updatedAt: now)
        entries.append(entry)
        try saveEntries(entries)
        return entry
    }

    public func markSending(
        _ id: UUID,
        now: Date = Date()
    ) throws -> PendingCapture {
        try update(id, now: now) { entry in
            PendingCapture(
                id: entry.id,
                request: entry.request,
                createdAt: entry.createdAt,
                updatedAt: now,
                attemptCount: entry.attemptCount + 1,
                state: .sending,
                lastError: nil
            )
        }
    }

    public func markFailed(
        _ id: UUID,
        message: String,
        now: Date = Date()
    ) throws -> PendingCapture {
        try update(id, now: now) { entry in
            PendingCapture(
                id: entry.id,
                request: entry.request,
                createdAt: entry.createdAt,
                updatedAt: now,
                attemptCount: entry.attemptCount,
                state: .failed,
                lastError: message
            )
        }
    }

    public func markDelivered(_ id: UUID) throws {
        var entries = try loadEntries()
        guard let index = entries.firstIndex(where: { $0.id == id }) else {
            throw CaptureOutboxError.entryNotFound(id)
        }
        entries.remove(at: index)
        try saveEntries(entries)
    }

    private func update(
        _ id: UUID,
        now: Date,
        transform: (PendingCapture) -> PendingCapture
    ) throws -> PendingCapture {
        var entries = try loadEntries()
        guard let index = entries.firstIndex(where: { $0.id == id }) else {
            throw CaptureOutboxError.entryNotFound(id)
        }
        let updated = transform(entries[index])
        entries[index] = updated
        try saveEntries(entries)
        return updated
    }

    private func loadEntries() throws -> [PendingCapture] {
        guard FileManager.default.fileExists(atPath: storageURL.path()) else {
            return []
        }
        let encrypted = try Data(contentsOf: storageURL)
        let key = try keyProvider()
        do {
            let sealedBox = try AES.GCM.SealedBox(combined: encrypted)
            let plaintext = try AES.GCM.open(sealedBox, using: key)
            let snapshot = try decoder.decode(PendingCaptureSnapshot.self, from: plaintext)
            return snapshot.entries
        } catch {
            throw CaptureOutboxError.corruptData
        }
    }

    private func saveEntries(_ entries: [PendingCapture]) throws {
        try FileManager.default.createDirectory(
            at: storageURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let plaintext = try encoder.encode(PendingCaptureSnapshot(entries: entries))
        let key = try keyProvider()
        let sealedBox = try AES.GCM.seal(plaintext, using: key)
        guard let combined = sealedBox.combined else {
            throw CaptureOutboxError.cryptographicFailure
        }
        try combined.write(to: storageURL, options: .atomic)
    }
}
