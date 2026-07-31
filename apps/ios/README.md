# Sahara iOS App Work

This directory holds the first implementation slices for Sahara's iPhone
companion app from [#74](https://github.com/nidheesh-p/sahara/issues/74).

The current foundation package lives in:

- `SaharaMobileCore/`

That package intentionally focuses on reusable client and app-shell logic first:

- pairing import from Sahara QR or deep-link payloads;
- secure paired-device configuration storage;
- authenticated mobile capture and recall requests;
- encrypted offline outbox persistence with idempotent replay support.
- a SwiftUI-facing `SaharaMobileApp` target with pairing, quick capture, recall,
  and outbox-status state that a future iOS app target can host.

The future SwiftUI app, Share Extension, and Siri App Intent targets can build on
top of this package without duplicating the security, transport, or core view-model
code.
