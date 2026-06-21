import Foundation
import Security

public enum KeychainDeviceConfigurationStoreError: LocalizedError {
    case invalidItemData
    case unexpectedStatus(OSStatus)

    public var errorDescription: String? {
        switch self {
        case .invalidItemData:
            return "The saved paired-device configuration is not valid."
        case .unexpectedStatus(let status):
            return "Keychain operation failed with status \(status)."
        }
    }
}

public final class KeychainDeviceConfigurationStore: DeviceConfigurationStore {
    private let service: String
    private let account: String
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    public init(
        service: String = "dev.sahara.mobile",
        account: String = "paired-device"
    ) {
        self.service = service
        self.account = account
    }

    public func load() throws -> PairedDeviceConfiguration? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess else {
            throw KeychainDeviceConfigurationStoreError.unexpectedStatus(status)
        }
        guard let data = item as? Data else {
            throw KeychainDeviceConfigurationStoreError.invalidItemData
        }
        return try decoder.decode(PairedDeviceConfiguration.self, from: data)
    }

    public func save(_ configuration: PairedDeviceConfiguration) throws {
        let data = try encoder.encode(configuration)
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]

        let updateStatus = SecItemUpdate(baseQuery() as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecItemNotFound {
            var addQuery = baseQuery()
            for (key, value) in attributes {
                addQuery[key] = value
            }
            let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
            guard addStatus == errSecSuccess else {
                throw KeychainDeviceConfigurationStoreError.unexpectedStatus(addStatus)
            }
            return
        }
        guard updateStatus == errSecSuccess else {
            throw KeychainDeviceConfigurationStoreError.unexpectedStatus(updateStatus)
        }
    }

    public func clear() throws {
        let status = SecItemDelete(baseQuery() as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainDeviceConfigurationStoreError.unexpectedStatus(status)
        }
    }

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}
