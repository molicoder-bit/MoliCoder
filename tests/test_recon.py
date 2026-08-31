from local_model_chat.molicoder.recon import Reconnaissance


def test_recon_builds_source_grounded_packet_and_excludes_secrets(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='shop'\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pricing.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_pricing.py").write_text(
        "from src.pricing import calculate_total\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")

    packet = Reconnaissance(tmp_path).build("Add tax support to calculate_total")

    assert packet.project_type == "python"
    assert "src/pricing.py" in packet.relevant_files
    assert "tests/test_pricing.py" in packet.tests
    assert any(ref.path == "src/pricing.py" and ref.line_start == 1 for ref in packet.evidence_refs)
    rendered = packet.to_prompt()
    assert "API_KEY" not in rendered
    assert ".env" not in rendered
    assert "src/pricing.py:1" in rendered


def test_recon_reads_project_instructions_as_constraints(tmp_path) -> None:
    state = tmp_path / ".molicoder"
    state.mkdir()
    (state / "instructions.md").write_text("Never change public API names.\n", encoding="utf-8")

    packet = Reconnaissance(tmp_path).build("Refactor helper")

    assert packet.constraints == ("Never change public API names.",)

