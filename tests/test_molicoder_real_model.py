import os
import sys

import pytest

from local_model_chat.molicoder.executor import AgentExecutor
from local_model_chat.molicoder.gateway import InferenceGateway
from local_model_chat.molicoder.tools import ToolDispatcher
from local_model_chat.molicoder.validate import ProjectValidator, ValidationCommand


pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("MOLICODER_REAL_MODEL") != "1",
    reason="set MOLICODER_REAL_MODEL=1 to exercise the installed Ollama model",
)
def test_real_model_edits_and_validates_temporary_project(tmp_path) -> None:
    (tmp_path / "sample.py").write_text(
        "def format_duration(seconds: int) -> str:\n    return str(seconds)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_sample.py").write_text(
        "from sample import format_duration\n\n"
        "def test_format_duration():\n"
        "    assert format_duration(65) == '1m 5s'\n",
        encoding="utf-8",
    )
    validator = ProjectValidator(
        tmp_path,
        [ValidationCommand("pytest", (sys.executable, "-m", "pytest", "-q"))],
    )
    executor = AgentExecutor(
        gateway=InferenceGateway(),
        dispatcher=ToolDispatcher(tmp_path, validation=validator.run_and_summarize),
        model="molicoder:latest",
        max_turns=8,
        max_seconds=240,
    )

    result = executor.run(
        "Update only format_duration in sample.py. It must format 65 as '1m 5s': "
        "show minutes and seconds and omit a zero-hours component. Inspect the existing "
        "file and test, make the smallest edit, then run validation."
    )

    assert result.status == "completed", result
    assert validator.run().passed, result
