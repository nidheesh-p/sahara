import SaharaMobileCore
import SwiftUI

public struct SaharaCompanionView: View {
    @StateObject private var viewModel: SaharaCompanionViewModel

    public init(viewModel: SaharaCompanionViewModel) {
        _viewModel = StateObject(wrappedValue: viewModel)
    }

    public var body: some View {
        NavigationStack {
            List {
                pairingSection
                captureSection
                recallSection
                outboxSection
                resultsSection
            }
            .navigationTitle("Sahara")
            .task {
                await viewModel.refresh()
            }
        }
    }

    private var pairingSection: some View {
        Section("Pairing") {
            if let device = viewModel.pairedDevice {
                LabeledContent("Device", value: device.name)
                LabeledContent("Endpoint", value: device.endpoint.absoluteString)
                LabeledContent("Scopes", value: device.scopes.map(\.rawValue).joined(separator: ", "))
                Button(role: .destructive) {
                    Task { await viewModel.clearPairing() }
                } label: {
                    Label("Remove Pairing", systemImage: "xmark.circle")
                }
            } else {
                TextField("Paste sahara://pair URI or JSON", text: $viewModel.pairingInput, axis: .vertical)
                Button {
                    Task { await viewModel.importPairing() }
                } label: {
                    Label("Import Pairing", systemImage: "qrcode.viewfinder")
                }
            }

            if let status = viewModel.statusMessage {
                Text(status)
                    .foregroundStyle(.secondary)
            }
            if let error = viewModel.errorMessage {
                Text(error)
                    .foregroundStyle(.red)
            }
        }
    }

    private var captureSection: some View {
        Section("Capture") {
            TextField("Title", text: $viewModel.captureTitle)
            TextField("Tags", text: $viewModel.captureTags)
            TextField("What should Sahara remember?", text: $viewModel.captureText, axis: .vertical)
                .lineLimit(4...8)
            Button {
                Task { await viewModel.saveCapture() }
            } label: {
                Label("Remember", systemImage: "square.and.arrow.down")
            }
            .disabled(viewModel.isWorking)
        }
    }

    private var recallSection: some View {
        Section("Recall") {
            TextField("Search memories", text: $viewModel.recallQuery)
            Button {
                Task { await viewModel.runRecall() }
            } label: {
                Label("Recall", systemImage: "magnifyingglass")
            }
            .disabled(!viewModel.canRecall || viewModel.isWorking)
        }
    }

    private var outboxSection: some View {
        Section("Outbox") {
            if viewModel.pendingCaptures.isEmpty {
                Text("No pending captures")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(viewModel.pendingCaptures) { capture in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(capture.request.title ?? "Untitled capture")
                            .font(.headline)
                        Text(capture.state.rawValue)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private var resultsSection: some View {
        Section("Results") {
            if viewModel.recallResults.isEmpty {
                Text("No recall results yet")
                    .foregroundStyle(.secondary)
            } else {
                ForEach(viewModel.recallResults, id: \.memoryID) { result in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(result.title)
                            .font(.headline)
                        Text(result.snippet)
                            .font(.body)
                        Text(result.relativePath)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}
