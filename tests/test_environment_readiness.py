from local_model_chat.environment.discovery import DiscoveredTool, EnvironmentReport
from local_model_chat.environment.doctor import doctor_report
from local_model_chat.environment.manifests import load_manifests
from local_model_chat.environment.readiness import readiness_dashboard


def test_missing_optional_tools_keep_shell_available_with_actions() -> None:
    inventory = EnvironmentReport(
        1, "Linux", "x86_64", 4, 8 * 1024**3, 50 * 1024**3,
        "unknown", "fixture",
        (
            DiscoveredTool("git", "/usr/bin/git", "2.50.0", "installed"),
            DiscoveredTool("python", "/usr/bin/python", "3.13.0", "installed"),
            DiscoveredTool("ollama", None, None, "missing"),
            DiscoveredTool("kandev", None, None, "missing"),
        ),
        (),
    )
    dashboard = readiness_dashboard(doctor_report(inventory, load_manifests()))

    assert dashboard.shell_ready
    assert not dashboard.local_chat_ready
    assert not dashboard.local_build_ready
    assert all(item.next_action for item in dashboard.components)
