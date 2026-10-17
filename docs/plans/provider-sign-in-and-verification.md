# Provider sign-in and connection verification

Release: **1.4.3**. Documentation and installed CLI help checked on **2026-10-17**. Baseline: `7fe6857` / 1.4.2.

## User flow

**Settings → Models & providers → Set up / sign in** now runs setup. It checks existing authentication and makes one small, isolated request with the selected model. When login is required, it starts the official provider login flow, then repeats the model check. Existing working credentials are reused. No project files are sent during the test, and signing in does not grant permission to share a project.

The popup displays progress, browser/device instructions and a sign-in page button for recognized official login URLs. Interactive provider selection, API-key entry or SSO can happen in a dedicated PowerShell window. After that process exits, MoliCoder verifies model access automatically. **Use device code** offers the documented alternative for Codex and Copilot. The user can switch an active browser login to device code or PowerShell immediately; the previous owned login is cancelled before the replacement starts. **Official help** remains a secondary action.

A zero exit code from login, a saved credential, a CLI version, or an advertised free model cannot produce **Ready**. The model must return the exact fresh nonce in the strict JSON test contract. Wrong output, login errors, account limits and network failures remain visible failures. Quota or model errors do not automatically trigger a login loop.

## Provider contracts and sources

| Provider | Official sign-in used by the app | Verification and source |
| --- | --- | --- |
| Codex | Browser login with `codex login`; optional device code with `codex login --device-auth`. PowerShell is available as a fallback. | Read `codex login status`, then verify a normal model at medium effort. An unrelated config/status-command failure can fall through to the actual isolated test. [Official OpenAI authentication documentation](https://learn.chatgpt.com/docs/auth). |
| Claude | Run `claude auth login` in PowerShell so browser, SSO and interactive prompts remain available. | Parse the boolean from `claude auth status`, then test the model after login. The signed-out JSON state is not accepted even with an inconsistent success exit code. [Claude CLI reference](https://code.claude.com/docs/en/cli-reference). |
| Copilot | Browser OAuth via `copilot login --web-flow`, with `--device-code` as an alternative. | There is no assumed headless Copilot account-status command. Successful model inference confirms the CLI's actual access, including entitlement. A GitHub CLI login alone is insufficient. [GitHub authentication guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli), [command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference). |
| OpenCode | Use its credential/provider selector in PowerShell; pass the configured provider namespace when supported. External plugins are disabled, matching application execution. | Test the exact selected model. An anonymous/free route that works requires no extra login. Credential inventories and free-price metadata remain discovery facts. [OpenCode CLI authentication](https://opencode.ai/docs/cli/#auth). |
| Kilo | Open its official authentication/provider selector in PowerShell. Its installed CLI does not advertise OpenCode's provider-selection flag, so that flag is not assumed. | Test the configured Kilo model after the process exits; the pinned free model is checked through the existing free-route policy. [Kilo CLI reference](https://kilo.ai/docs/code-with-ai/platforms/cli-reference). |

Local Ollama continues to use the local runtime and does not need these account login flows. Missing provider installations are reported before authentication; supported managed installers are offered directly. Account creation and browser/MFA approval remain in the provider's official flow.

## Lifecycle and stored state

- Connection results are cached for 15 minutes per provider and model, with an executable modification fingerprint. A different model, changed binary or expired result needs a new test.
- **Refresh connections** checks available account metadata without generation. A confirmed logout or missing executable invalidates previous connection results. Unknown authentication metadata does not erase a successful model test. Free model discovery is labeled separately from verified access.
- Each setup worker captures its provider/model. Closing or cancelling the popup stops only its owned login/test processes; a late result cannot publish Ready. Closing the app waits for cancellation to finish.
- Official CLIs own credential storage. Browser URLs and device codes are shown only as transient popup output and are cleared after success. SQLite stores the redacted result, provider/model, executable fingerprint and check time.
- The popup states that the small model request may use provider quota or incur account charges.

## CLI

```powershell
# Check without opening a login flow
.venv\Scripts\python.exe -m local_model_chat.environment.provider_connection codex --report codex-check.json

# Sign in only if needed, then confirm model access
.venv\Scripts\python.exe -m local_model_chat.environment.provider_connection claude --sign-in --powershell --report claude-check.json

# The same service in the packaged app
.\dist\MoliCoder\MoliCoder.exe --provider-connection opencode --report opencode-check.json
```

Exit 0 means the selected model answered the connection test. Exit 2 means installation, sign-in, access or test attention is required. Reports contain no captured login URLs, device codes or credentials.

## Verification

The deterministic suite covers every provider's success and login path, nonce failures, incorrect login exit codes, quota/network handling, credential-cache scope, actual hidden PowerShell execution of a harmless CLI fixture, real subprocess cancellation, timeout presentation and the setup button's worker lifecycle. A fixture environment initially omitted Windows system variables and could not start PowerShell; the test now uses the same restricted environment as real provider execution, and the real wrapper test passed.

Real CLI checks on this machine:

- Codex: model access verified with normal `gpt-6.1-sol`, medium effort.
- OpenCode: selected free route verified.
- Kilo: selected free route verified.
- Claude: installed; its official status JSON confirms sign-in is required. Its interactive login completion needs the user's browser/terminal approval.
- Copilot: not installed; its login and verification branches are covered with fixtures, not claimed as a live authenticated account.

The opt-in `tests/test_live_provider_connection.py` drives the real setup dialog offscreen, verifies OpenCode access, confirms that project trust was not silently granted, then runs a real two-turn project conversation after explicit fixture-profile consent. Both answers passed, including missing-flag behavior; original files remained unchanged. Raw evidence is under `artifacts/provider-login-20261017/`.

## Package-specific verification

A candidate build exposed a packaging omission: the constant dynamic import of the new dialog was not included by PyInstaller. The button now uses an ordinary static import. The release gate includes `--package-provider-smoke-check REPORT`, which opens the actual settings page offscreen, clicks its real OpenCode setup button, waits for the model connection check and checks the saved/listed result. It verifies that setup did not grant project trust. Only this explicit test flag suppresses interactive login; model inference and nonce verification remain real, and an authentication failure fails the gate.

The layout fixture preview is `artifacts/provider-login-20261017/login-popup-example.png`. It uses an explicitly fake example code and no real authorization URL or secret from a live account.

A subsequent package gate caught a real host-tool fallback failure: the local model invented an existing target, then OpenCode's built-in plan agent refused even a JSON write proposal because of its injected read-only planning instruction. OpenCode/Kilo now use their standard execution agent for host proposals with read-only native permissions: writes, commands and external tools remain denied; MoliCoder reviews and performs the proposed operation. Separate repository analysis and Build planning still use their planning modes. Adapter tests check these permissions, and a coordinator test verifies local failure → external proposal → approved host write → exact disk checks.

A discarded fully tool-disabled profile was rejected by OpenCode's free service with HTTP 403; it is preserved as failed evidence. No credential, endpoint, header or provider identity was changed to bypass that response. The final profile uses the documented native execution agent with ordinary read-only tools and passed real proposal checks for both OpenCode and Kilo, without native file writes. [OpenCode agents and permission configuration](https://opencode.ai/docs/agents/).

## Qualified release results

Final source qualification: **788 passed, 10 opt-in tests skipped, zero failures/errors** (798 total). All four shards exited successfully, and the source/test/config snapshot remained unchanged throughout. Source digest: `ecce142133db304a7ce6dbfab432061e7eba06dc09acea90598aa59748f3283b`.

All **eight packaged gates passed**: startup/storage migration, WebEngine, actual provider setup button and live nonce verification, General project identity, greeting, source analysis, exact file creation and exact file editing. The actual create/edit cases both retained local-model failures and then completed through OpenCode host proposals, with the expected bytes confirmed on disk. Packaged executable SHA-256: `d8b56909c850638579b6ab1f5b56ff7e4fadb4168ccdad2242921442e2714697`.

The opt-in real setup/conversation test was repeated against the final source: **1 passed in 73.48 seconds**. The assistant described Seaside from its README, then correctly explained that a missing `archived` flag defaults to `False` using an exact `notes.py:2` citation. The source files remained unchanged. Final machine-readable evidence is in `artifacts/provider-login-20261017/qualified/`, including retained reports from the earlier failed candidate under the sibling `release/` directory.

Installation is awaiting the user closing the running MoliCoder window, as required by `scripts/install.ps1` to preserve drafts.
