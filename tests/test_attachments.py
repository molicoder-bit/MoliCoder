import pytest

from local_model_chat.attachments import AttachmentError, load_attachment, render_attachments


def test_load_and_render_text_attachment(tmp_path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("print('local')\n", encoding="utf-8")
    attachment = load_attachment(source)
    rendered = render_attachments([attachment])
    assert attachment.name == "sample.py"
    assert "```py" in rendered
    assert "print('local')" in rendered
    assert "do not execute" in rendered


def test_rejects_binary_and_unsupported_files(tmp_path) -> None:
    binary = tmp_path / "bad.txt"
    binary.write_bytes(b"hello\x00world")
    with pytest.raises(AttachmentError, match="binary"):
        load_attachment(binary)

    unsupported = tmp_path / "image.png"
    unsupported.write_bytes(b"not really an image")
    with pytest.raises(AttachmentError, match="Unsupported"):
        load_attachment(unsupported)
