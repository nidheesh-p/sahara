import XCTest
@testable import SaharaMobileCore

final class PairingImportTests: XCTestCase {
    func testImportsPairingURIIntoConfiguration() throws {
        let date = Date(timeIntervalSince1970: 1_718_700_000)
        let payload = """
        {"version":1,"type":"sahara-mobile-pairing","device_id":"device-123","name":"Nidheesh iPhone","endpoint":"https://desktop.tailnet.ts.net","token":"sahara_token","scopes":["memory:capture","memory:recall"]}
        """
        let encoded = try XCTUnwrap(payload.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed))
        let uri = "sahara://pair?payload=\(encoded)"

        let configuration = try SaharaPairingImporter.configuration(from: uri, pairedAt: date)

        XCTAssertEqual(configuration.deviceID, "device-123")
        XCTAssertEqual(configuration.name, "Nidheesh iPhone")
        XCTAssertEqual(configuration.endpoint.absoluteString, "https://desktop.tailnet.ts.net")
        XCTAssertEqual(configuration.token, "sahara_token")
        XCTAssertEqual(configuration.scopes, [.memoryCapture, .memoryRecall])
        XCTAssertEqual(configuration.pairedAt, date)
    }

    func testRejectsPairingPayloadWithoutToken() {
        let payload = """
        {"version":1,"type":"sahara-mobile-pairing","device_id":"device-123","name":"Nidheesh iPhone","endpoint":"https://desktop.tailnet.ts.net","token":"","scopes":["memory:capture"]}
        """

        XCTAssertThrowsError(try SaharaPairingImporter.configuration(from: payload)) { error in
            XCTAssertEqual(error as? PairingImportError, .missingToken)
        }
    }

    func testRejectsUnsupportedPayloadType() {
        let payload = """
        {"version":1,"type":"other-pairing","device_id":"device-123","name":"Nidheesh iPhone","endpoint":"https://desktop.tailnet.ts.net","token":"sahara_token","scopes":["memory:capture"]}
        """

        XCTAssertThrowsError(try SaharaPairingImporter.configuration(from: payload)) { error in
            XCTAssertEqual(error as? PairingImportError, .unsupportedType("other-pairing"))
        }
    }
}
