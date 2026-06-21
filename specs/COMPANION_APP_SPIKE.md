# Sahara Mobile Companion App Spike

## Status

Evaluation for [#63](https://github.com/nidheesh-p/sahara/issues/63).

This spike reviews whether Sahara should move from the current private API plus
Apple Shortcuts workflow to a lightweight companion app, and if so, what shape
that app should take.

## Executive Summary

Recommendation:

1. Keep the Shortcut-based flow as the supported validation path in the near term.
2. If we approve app development next, build an iOS-first native companion in
   SwiftUI as the first implementation.
3. Do not start with a true cross-platform build yet.
4. Revisit Android and broader cross-platform investment only after the iPhone
   flow shows repeated use and the app scope stabilizes.

Why:

- The strongest current demand is iPhone capture and recall.
- The hardest parts of the product are iOS-specific: share extension, Siri App
  Intent support, pairing UX, background retry, and secure local storage.
- A cross-platform-first choice adds framework complexity before Sahara has a
  stable mobile product surface.
- The desktop Sahara instance should remain the authority for storage, indexing,
  sync, and retrieval.

This spike therefore recommends using [#74](https://github.com/nidheesh-p/sahara/issues/74)
as the first mobile app implementation path after Shortcut-based onboarding is
validated.

## Current State

Sahara already has the key building blocks for a companion app:

- durable Markdown-backed memory capture;
- filtered memory recall;
- authenticated private mobile API;
- named, scoped, revocable device tokens;
- iPhone setup and Shortcut onboarding for capture and recall.

What still feels manual for a normal iPhone user:

- pairing and token installation;
- creating or importing Shortcuts;
- editing JSON and headers when something drifts;
- remembering which Shortcut to run for capture vs recall;
- handling offline capture or retry behavior.

That is exactly the gap a companion app should close.

## Product Goals

The app should make the existing Sahara mobile workflow feel native without
expanding the trust boundary too far.

The app should provide:

- fast text capture;
- voice capture and dictated note entry;
- share-sheet capture from other apps;
- lightweight recall against the desktop Sahara instance;
- secure pairing and revocation;
- offline capture with safe retries;
- clear success, failure, and pending-sync status.

The app should not provide:

- local embeddings or local semantic search;
- full archive browsing or arbitrary file access;
- storage configuration editing;
- hosted multi-user sync;
- unofficial automation of WhatsApp or other third-party apps.

## Candidate Implementation Paths

### Option A: Stay with Shortcuts only

Pros:

- lowest implementation cost;
- already works with the current private API;
- no App Store or native release burden.

Cons:

- fragile setup for non-technical users;
- hard to make pairing and secrets feel polished;
- weak offline story;
- limited status, error handling, and background retry;
- difficult to provide a reliable recall experience.

Verdict:

Good validation path, not a good mainstream product path.

### Option B: Native iOS-first app in SwiftUI

Pros:

- best fit for current demand;
- first-class support for Share Extension, App Intents, and Siri flows;
- straightforward Keychain integration and iOS background behavior;
- lower product complexity than designing iOS and Android at once;
- easiest path to polished pairing UX and QR scanning.

Cons:

- not cross-platform;
- Android work would come later as a separate effort;
- some logic may need to be reimplemented if Android is added later.

Verdict:

Best next implementation path once Shortcut validation is sufficient.

### Option C: React Native / Expo-first app

Pros:

- eventual shared UI and business logic across iOS and Android;
- easier future Android expansion than a pure native iOS path.

Cons:

- still requires native modules for Share Extension, Siri/App Intents, secure
  background behavior, and pairing polish;
- Expo support for the most iOS-specific features is not the easy path here;
- higher debugging and release complexity before product scope is stable.

Verdict:

Reasonable only if cross-platform demand becomes immediate and strong. Not the
best first move for Sahara today.

### Option D: Flutter-first app

Pros:

- strong cross-platform UI story;
- good long-term portability.

Cons:

- same platform-integration problem as React Native for Siri/App Intents and
  share-extension-heavy workflows;
- less aligned with the current iOS-first product pressure;
- additional tooling and maintenance surface for a small project.

Verdict:

Not recommended as the first mobile app path.

## Recommendation

Choose native iOS-first implementation next, then reevaluate Android after the
mobile workflow is proven in normal use.

That means:

- treat Shortcuts as the bridge solution;
- treat [#74](https://github.com/nidheesh-p/sahara/issues/74) as the first app
  implementation issue;
- keep [#63](https://github.com/nidheesh-p/sahara/issues/63) as the spike and
  decision record, not the place where app code begins.

If Android demand later becomes real, we should revisit whether:

- Android should ship as its own native client; or
- the shared logic is stable enough to justify a true cross-platform framework.

## Proposed App Architecture

The desktop Sahara instance remains authoritative.

The mobile app is only a trusted capture and recall client with a local retry
queue. It should never own the archive or local embedding model.

### Desktop responsibilities

- pair and revoke devices;
- store token hashes and scopes;
- accept capture and recall requests;
- write Markdown memories;
- perform indexing and retrieval;
- keep audit records.

### Mobile responsibilities

- store paired endpoint and device secret securely;
- submit capture requests to `/v1/memories`;
- submit recall requests to `/v1/recall`;
- cache only lightweight recent state needed for UX;
- queue offline captures for retry;
- show sync status and retry outcomes.

## Security Model

### Pairing

- Desktop Sahara generates a named device with scoped credentials.
- Pairing is transferred by QR code or one-time deep link payload.
- The mobile app stores the resulting device identity and bearer token locally.
- Tokens remain individually revocable from the desktop Sahara CLI.

### Secret storage

- iOS: store device secrets in Keychain.
- Android, when implemented: store secrets in Keystore-backed encrypted storage.

### Request scopes

- `memory:capture` for write-only mobile capture.
- `memory:recall` only when the paired device should be allowed to search.

### Network model

- Default access remains private-network only.
- Tailscale or another private endpoint is preferred over direct LAN exposure.
- Public internet exposure is out of scope by default.

### Local data protection

- The app should not keep an unbounded local archive copy.
- The offline outbox should be encrypted at rest with a platform-protected key.
- Recent capture status may be cached, but recall results should be stored
  minimally and expire quickly.

## Offline Behavior

The app needs an outbox, not a full local sync engine.

Each capture should be written locally with:

- a generated idempotency key;
- capture body;
- timestamps;
- delivery state such as `pending`, `sending`, `sent`, or `failed`;
- retry metadata.

Expected behavior:

- captures created offline stay queued;
- retries use the same idempotency key;
- successful replay marks the item as sent without duplication;
- failures are visible to the user and can be retried manually;
- recall is online-only in the first app version.

This keeps the mobile client simple while still solving the real “I had the
thought when I was away from my desktop” problem.

## Platform Flows

### iOS first-release flows

- app launch for quick text capture;
- voice capture from an in-app dictate flow;
- Share Extension for selected text and URLs from other apps;
- QR pairing scanner;
- App Intent for “Remember in Sahara”;
- App Intent for lightweight recall;
- recent activity and retry status screen.

### Android design targets for later evaluation

- `ACTION_SEND` share target;
- quick capture activity;
- App Actions for capture and recall;
- equivalent secure storage and retry queue;
- QR pairing and revocation model aligned with iOS.

## Platform Limitations And Design Consequences

- Siri/App Intents and Share Extensions are most naturally built in native iOS
  tooling, which weakens the case for a cross-platform-first start.
- The app should avoid speaking recall results automatically because saved
  memories may be sensitive.
- Mobile capture should prefer explicit user-provided text or shared URLs; it
  should not scrape app content in the background.
- Background retry support must be best-effort and user-visible, not a promise
  of immediate delivery under every mobile OS constraint.

## Maintenance Cost

### Shortcuts path

- Low code cost.
- High user-support cost.
- Hard to make robust for normal users.

### Native iOS-first path

- Moderate engineering cost.
- Lowest risk for the next actual product improvement.
- Best chance of delivering pairing, Siri, and share-sheet quality quickly.

### Cross-platform-first path

- Highest upfront architecture cost.
- More release complexity.
- Greater chance of spending time on framework glue before the product surface
  settles.

## Decision

Approve the following order:

1. Finish and validate the Shortcut-based onboarding flow.
2. Build the iOS-first native companion app in [#74](https://github.com/nidheesh-p/sahara/issues/74).
3. Reassess Android and broader cross-platform investment after real usage.

## Suggested Success Gates Before Starting Full App Work

- Pairing and Shortcut setup are stable enough to serve as a fallback path.
- Mobile capture and recall have been used repeatedly in real workflows.
- The private API contract is stable enough that the app will not immediately
  churn.
- The remaining pain is clearly UX and reliability, not missing backend
  capability.

## Non-Goals For The First Companion App

- browsing arbitrary Sahara files;
- editing storage backends or sync policies;
- running embeddings on-device;
- syncing the full archive to the phone;
- automatic extraction from third-party messaging apps without explicit share or
  copy actions.
