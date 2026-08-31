import pytest

from local_model_chat.molicoder.skills import SkillLibrary


def test_skill_library_saves_matches_and_augments_project_guidance(tmp_path) -> None:
    library = SkillLibrary(tmp_path)
    library.save("pytest-focus", "Run the focused test before the full suite.", ("pytest", "test"))
    library.save("flutter-check", "Run flutter analyze first.", ("flutter",))

    matches = library.match("Add a pytest regression test")
    augmented = library.augment("Add a pytest regression test")

    assert [skill.name for skill in matches] == ["pytest-focus"]
    assert "Run the focused test" in augmented
    assert "flutter analyze" not in augmented


def test_skill_names_cannot_escape_project_skill_directory(tmp_path) -> None:
    library = SkillLibrary(tmp_path)

    with pytest.raises(ValueError):
        library.save("../outside", "bad", ("bad",))
