from local_model_chat.formatting import normalize_model_markdown


def test_boxed_latex_prose_becomes_final_answer_callout() -> None:
    content = (
        r"\[ \boxed{\text{Absolutely! Pictures can be fascinating. "
        r"What would you like to discuss?}} \]"
    )

    rendered = normalize_model_markdown(content)

    assert "> **Final answer**" in rendered
    assert "Absolutely! Pictures can be fascinating." in rendered
    assert "\\boxed" not in rendered
    assert "\\text" not in rendered


def test_plain_markdown_is_unchanged() -> None:
    content = "Here is code:\n\n```python\nprint('hello')\n```"
    assert normalize_model_markdown(content) == content


def test_bracket_variant_without_latex_delimiters_is_supported() -> None:
    content = r"[ \boxed{\text{A concise answer.}} ]"
    rendered = normalize_model_markdown(content)
    assert rendered.strip() == "> **Final answer**\n>\n> A concise answer."
