# Sahara iOS App Work

This directory holds the first implementation slices for Sahara's iPhone
companion app from [#74](https://github.com/nidheesh-p/sahara/issues/74).

The current foundation package lives in:

- `SaharaMobileCore/`

That package intentionally focuses on reusable client logic first:

- pairing import from Sahara QR or deep-link payloads;
- secure paired-device configuration storage;
- authenticated mobile capture and recall requests;
- encrypted offline outbox persistence with idempotent replay support.

The future SwiftUI app, Share Extension, and Siri App Intent targets can build on
top of this package without duplicating the security and transport code.
