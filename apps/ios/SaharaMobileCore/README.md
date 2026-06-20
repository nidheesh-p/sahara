# SaharaMobileCore

`SaharaMobileCore` is the shared Swift foundation for Sahara's iPhone companion
app work.

This package currently includes:

- pairing import from `sahara://pair?...` deep links or raw pairing JSON;
- `KeychainDeviceConfigurationStore` for paired-device secrets;
- `SaharaMobileAPIClient` for `/v1/memories` and `/v1/recall`;
- `EncryptedCaptureOutboxStore` for offline capture retry state.

Local package test:

```bash
swift test --package-path apps/ios/SaharaMobileCore
```
