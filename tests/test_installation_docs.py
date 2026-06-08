"""Keep public installation guidance compatible with managed Python installs."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
PUBLIC_INSTALL_DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "GETTING_STARTED.md",
    ROOT / "docs" / "CLAUDE_DESKTOP.md",
    ROOT / "docs" / "integrations" / "chat-agents.md",
)


def test_public_install_docs_recommend_pipx() -> None:
    for path in PUBLIC_INSTALL_DOCS:
        text = path.read_text()
        assert 'pipx install "sahara-memory[search,mcp]"' in text, path
        assert "git+https://github.com/nidheesh-p/sahara.git" not in text, path


def test_installation_guide_covers_pep_668_without_unsafe_override() -> None:
    text = (ROOT / "docs" / "INSTALLATION.md").read_text()

    assert "externally-managed-environment" in text
    assert "virtual environment" in text
    assert '--python python3.12' in text
    assert "Do not use" in text
    assert "--break-system-packages" in text
