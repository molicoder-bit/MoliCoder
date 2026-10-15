# Platform smoke evidence

MoliCoder's desktop runtime uses Python 3.13 and PySide6. The CI platform workflow runs the full automated suite, the read-only environment doctor, a PyInstaller build, and a packaged executable check. The packaged check opens the Chat and Build UI with an isolated temporary database and runs doctor discovery without Ollama, an agent login, or a paid call. It does not prove real model inference or external agent execution.

| Platform | Build path | Smoke scope | Current limits |
| --- | --- | --- | --- |
| Windows x64 | `scripts/build.ps1` or Windows CI | Full tests; package UI and doctor smoke in CI | Native Ollama chat/Build still need installed models. |
| Linux x64 | `python scripts/build_unix.py` on the GitHub Ubuntu runner | Full tests; source doctor; PyInstaller package, UI, and doctor smoke | Metered API routes are blocked without an OS key store. Kandev managed installer has a pinned Linux archive but no live service check. No strict OS sandbox claim for external CLIs. |
| macOS x64 | `python scripts/build_unix.py` on the GitHub macOS Intel runner | Full tests; source doctor; PyInstaller package, UI, and doctor smoke | Metered API routes are blocked without an OS key store. No pinned macOS managed Kandev archive. No strict OS sandbox claim for external CLIs. |

The platform smoke uses fixed test fixtures for Chat and Build behavior. It verifies startup and packaging on each CI host, not a downloaded model, a logged-in coding agent, or Kandev ownership against a live service. Those integrations stay conditional on local setup and their separate readiness tasks. The macOS and Linux outputs are CI artifacts; they are not signed installers.
