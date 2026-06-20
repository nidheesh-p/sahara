import Foundation

public protocol DeviceConfigurationStore {
    func load() throws -> PairedDeviceConfiguration?
    func save(_ configuration: PairedDeviceConfiguration) throws
    func clear() throws
}

public final class InMemoryDeviceConfigurationStore: DeviceConfigurationStore {
    private var configuration: PairedDeviceConfiguration?

    public init(configuration: PairedDeviceConfiguration? = nil) {
        self.configuration = configuration
    }

    public func load() throws -> PairedDeviceConfiguration? {
        configuration
    }

    public func save(_ configuration: PairedDeviceConfiguration) throws {
        self.configuration = configuration
    }

    public func clear() throws {
        configuration = nil
    }
}
