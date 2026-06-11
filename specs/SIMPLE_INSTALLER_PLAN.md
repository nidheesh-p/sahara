# Sahara Simple Installer Plan

## Goal

Make Sahara useful to someone who does not have Git, Python, pip, or pipx and does not
want to clone a repository.

The primary onboarding path should become:

1. Download and run the installer, or install Sahara with the platform package manager.
2. Run `sahara setup`.
3. Select one or more folders.
4. Search locally, or connect an MCP client.

The existing PyPI and pipx distribution remains supported for Python users and
contributors.

## Definition of Done

A new user on a supported platform can:

- install Sahara without separately installing Python or cloning GitHub;
- complete a basic, index-only setup without configuring storage or an answer provider;
- index a folder and run `sahara search`;
- connect Claude Desktop with `sahara mcp install-claude`;
- reach a cited result in under five minutes, excluding the embedding-model download;
- upgrade or uninstall the application without deleting `~/.sahara`, indexed data, or
  user configuration unless they explicitly request data removal.

The initial native release is complete when this flow is verified on a clean macOS
Apple Silicon machine and a clean Windows x64 machine.

## Product Decisions

### The first installer is CLI-first

The installer provides the `sahara` command and a guided terminal setup. A desktop GUI,
menu-bar process, and graphical search experience are not required for the first
native release.

### Basic search is the default

`sahara setup` should default to local semantic indexing:

- no external drive;
- no AWS account or credentials;
- no Ollama installation;
- no OpenAI API key;
- no standalone answer provider.

Local-drive and AWS storage remain optional extensions. Ollama and OpenAI remain
optional answer-generation providers. An MCP client can use Sahara's retrieval tools
without either provider.

### Bundle runtime dependencies, not user data or models

Native packages should include the Python runtime and the dependencies required for
semantic search and MCP. They should not bundle:

- the embedding model;
- an Ollama model or Ollama itself;
- user indexes or example personal data;
- AWS credentials.

The local embedding model remains a first-use download. Issue #36 tracks a command to
prepare that model before indexing, which can later be incorporated into the setup
wizard.

### Preserve the existing command and data model

Native and PyPI installations must expose the same `sahara` command and use the same
configuration and index locations. Switching installation methods or upgrading must
not require re-indexing.

## User Experience

### Installation

The first supported artifacts should be:

- macOS Apple Silicon: signed and notarized installer package;
- Windows x64: signed installer;
- Linux x86_64: portable archive after the first two platforms are stable.

Package-manager distribution follows stable native artifacts:

- Homebrew tap for macOS;
- WinGet manifest for Windows.

The download page and release notes must publish SHA-256 checksums. Python users may
continue to use:

```bash
pipx install "sahara-memory[search,mcp]"
```

### Guided Setup

`sahara setup` should orchestrate the existing commands rather than duplicate their
business logic. The default flow should:

1. choose a primary content folder;
2. create or update a basic/index-only configuration;
3. offer to add more folders;
4. offer to download the embedding model and build the first index;
5. offer to install the Claude Desktop MCP configuration when Claude Desktop is
   detected;
6. print the first useful search command.

The command must be idempotent and resumable. Existing configuration, content roots,
MCP client settings, and indexes must be preserved. Non-interactive options are
required so the flow can be exercised in automated tests.

Storage and answer-provider setup should be linked as next steps, not placed in the
default wizard path.

## Packaging Architecture

### Standalone Runtime

Start with a PyInstaller one-folder bundle. It is easier to inspect and debug than a
single-file executable and avoids unpacking the full runtime on every invocation.
Evaluate another freezer only if required dependencies cannot be packaged reliably.

The bundle must include and validate resources used by:

- FastEmbed and ONNX Runtime;
- sqlite-vec;
- pypdf and python-docx;
- the MCP SDK;
- keyring and cryptography;
- Sahara's `.saharaignore` template and package metadata.

Builds must be produced on their target operating system. Cross-compilation is out of
scope.

### Platform Installers

The macOS package should install the bundle in a stable application-support location
and expose `sahara` on `PATH`. It must be signed with a Developer ID certificate and
notarized before being described as a supported public installer.

The Windows installer should install per-user by default, add `sahara` to the user's
`PATH`, and avoid requiring administrator access when possible. It should support
quiet installation for clean-machine tests.

Signing credentials and certificates are external release prerequisites. Unsigned
artifacts may be used for development, but documentation must not present operating
system security bypasses as normal installation steps.

## Release Automation and Cost

Native builds are substantially more expensive than the current Python package build.
To control GitHub Actions usage:

- build native release artifacts only on `workflow_dispatch` and release tags;
- do not run the full native matrix on every push or pull request;
- keep a lightweight bundle configuration or import check in normal CI;
- upload each native artifact once and reuse it for installer assembly and release;
- retain build artifacts only as long as needed;
- avoid duplicate push and pull-request runs, as already tracked by issue #37.

Each release build should produce:

- versioned, platform-specific filenames;
- SHA-256 checksums;
- a software bill of materials or bundled dependency inventory;
- smoke-test output from a machine without a system Sahara installation.

## Verification Matrix

Every supported native artifact must be tested in a clean VM or clean user account
with no Git checkout and no usable system Python.

Required checks:

1. Install and run `sahara --version`.
2. Run non-interactive basic setup against a fixture folder.
3. Download or prepare the embedding model.
4. Run `sahara index`.
5. Run `sahara search` and verify a known cited result.
6. Start the stdio MCP server.
7. On supported Claude Desktop platforms, run `sahara mcp install-claude` and verify
   the generated executable path.
8. Upgrade over a previous native version and confirm configuration and index data
   remain usable.
9. Uninstall the application and confirm user data is preserved by default.

The release checklist should record the tested operating-system versions and elapsed
time for the first successful search.

## Milestones

### Milestone 1: One-command onboarding

- Add the idempotent `sahara setup` command.
- Keep basic local search as the default.
- Add non-interactive coverage and onboarding documentation.

### Milestone 2: Standalone runtime

- Build and smoke-test a macOS Apple Silicon one-folder bundle.
- Resolve runtime resource and native-library packaging.
- Confirm the bundle works without a system Python installation.

### Milestone 3: Repeatable release artifacts

- Add manually triggered and release-tag native builds.
- Generate checksums and dependency inventory.
- Reuse built bundles in platform installer jobs.

### Milestone 4: Supported macOS installer

- Create the installer package.
- Add signing, notarization, upgrade, and uninstall validation.
- Publish a clean-machine installation guide.

### Milestone 5: Supported Windows installer

- Build the Windows x64 bundle and installer.
- Add per-user installation, `PATH`, upgrade, and uninstall validation.
- Add signing when release credentials are available.

### Milestone 6: Package managers and Linux

- Publish a Homebrew tap and WinGet manifest from stable release artifacts.
- Add a portable Linux x86_64 archive and document its compatibility scope.

## Non-Goals for the First Native Release

- a graphical Sahara application;
- bundling the embedding model, Ollama, or an answer-generation model;
- automatic background updates;
- every Linux distribution or CPU architecture;
- configuring local-drive or AWS storage during default onboarding;
- replacing the PyPI distribution;
- changing Sahara's configuration or index formats.

## Issue Breakdown

The implementation is tracked by
[#54: Install and use Sahara without Git or Python](https://github.com/nidheesh-p/sahara/issues/54).
Individual changes remain independently reviewable:

1. [#49: Add guided `sahara setup` onboarding for local search](https://github.com/nidheesh-p/sahara/issues/49)
2. [#47: Prototype a standalone macOS Apple Silicon Sahara bundle](https://github.com/nidheesh-p/sahara/issues/47)
3. [#51: Add release-only native artifact builds and verification](https://github.com/nidheesh-p/sahara/issues/51)
4. [#50: Create a signed and notarized macOS installer](https://github.com/nidheesh-p/sahara/issues/50)
5. [#52: Build and validate a Windows x64 Sahara installer](https://github.com/nidheesh-p/sahara/issues/52)
6. [#53: Publish Homebrew and WinGet installation paths](https://github.com/nidheesh-p/sahara/issues/53)
7. [#48: Provide a portable Linux x86_64 Sahara artifact](https://github.com/nidheesh-p/sahara/issues/48)

Existing related work:

- [#36: Add a command to prepare the local embedding model before indexing](https://github.com/nidheesh-p/sahara/issues/36)
- [#37: Avoid duplicate CI runs for branches with open pull requests](https://github.com/nidheesh-p/sahara/issues/37)
