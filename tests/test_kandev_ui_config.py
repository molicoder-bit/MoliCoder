import pytest

from local_model_chat.kandev.ui_config import configured_kandev_adapter, kandev_board_url
from local_model_chat.storage import ChatStore


def test_kandev_build_config_requires_explicit_loopback_settings(tmp_path):
    store = ChatStore(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="KANDEV_ENDPOINT"):
        configured_kandev_adapter(store, {})

    values = {
        "KANDEV_ENDPOINT": "http://127.0.0.1:38429/mcp",
        "KANDEV_VERSION": "0.97.0",
        "KANDEV_WORKSPACE_ID": "workspace-1",
        "KANDEV_WORKFLOW_ID": "workflow-1",
        "KANDEV_WORKFLOW_STEP_ID": "step-1",
        "KANDEV_REPOSITORY_ID": "repo-1",
        "KANDEV_AGENT_PROFILE_ID": "agent-1",
    }
    adapter = configured_kandev_adapter(store, values)
    assert adapter.connection.endpoint == values["KANDEV_ENDPOINT"]
    assert adapter.api.config.repository_id == "repo-1"
    with pytest.raises(ValueError, match="loopback"):
        configured_kandev_adapter(store, {**values, "KANDEV_ENDPOINT": "https://example.com"})


def test_kandev_board_url_uses_web_origin_without_task_settings():
    assert kandev_board_url({"KANDEV_ENDPOINT": "http://127.0.0.1:38429/mcp"}) == "http://127.0.0.1:38429/"
    assert kandev_board_url({"KANDEV_PORT": "40000"}) == "http://127.0.0.1:40000/"
    with pytest.raises(ValueError, match="loopback"):
        kandev_board_url({"KANDEV_ENDPOINT": "https://example.com/mcp"})
    with pytest.raises(ValueError, match="port"):
        kandev_board_url({"KANDEV_ENDPOINT": "http://127.0.0.1:99999/mcp"})
