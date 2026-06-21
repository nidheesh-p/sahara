import CryptoKit
import Foundation
import XCTest
@testable import SaharaMobileCore

final class CaptureOutboxTests: XCTestCase {
    func testEncryptedOutboxPersistsWithoutLeakingPlaintext() async throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let storageURL = directory.appendingPathComponent("outbox.bin")
        let key = SymmetricKey(size: .bits256)
        let store = EncryptedCaptureOutboxStore(storageURL: storageURL) { key }
        let request = MemoryCaptureRequest(
            text: "Hotel we stayed at Reno in Apr 2026 was Hyatt Place.",
            tags: ["travel", "reno"],
            idempotencyKey: "capture-123"
        )
        let firstDate = Date(timeIntervalSince1970: 1_718_700_000)
        let secondDate = Date(timeIntervalSince1970: 1_718_700_120)

        let pending = try await store.enqueue(request, now: firstDate)
        let initialEntries = try await store.list()
        XCTAssertEqual(initialEntries.count, 1)

        let onDisk = try Data(contentsOf: storageURL)
        let textPreview = String(decoding: onDisk, as: UTF8.self)
        XCTAssertFalse(textPreview.contains("Hyatt Place"))

        let sending = try await store.markSending(pending.id, now: secondDate)
        XCTAssertEqual(sending.state, .sending)
        XCTAssertEqual(sending.attemptCount, 1)
        XCTAssertEqual(sending.updatedAt, secondDate)

        let reloaded = EncryptedCaptureOutboxStore(storageURL: storageURL) { key }
        let entries = try await reloaded.list()
        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0].request.idempotencyKey, "capture-123")
        XCTAssertEqual(entries[0].attemptCount, 1)
        XCTAssertEqual(entries[0].state, .sending)

        try await reloaded.markDelivered(pending.id)
        let remainingEntries = try await reloaded.list()
        XCTAssertEqual(remainingEntries, [])
    }

    func testCorruptDataFailsToOpen() async throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let storageURL = directory.appendingPathComponent("outbox.bin")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try Data("not encrypted".utf8).write(to: storageURL)
        let key = SymmetricKey(size: .bits256)
        let store = EncryptedCaptureOutboxStore(storageURL: storageURL) { key }

        do {
            _ = try await store.list()
            XCTFail("Expected corrupt data to throw")
        } catch let error as CaptureOutboxError {
            switch error {
            case .corruptData:
                break
            default:
                XCTFail("Unexpected error: \(error)")
            }
        }
    }
}
