# Platform security limits

`environment.security.platform_security` reports the protection MoliCoder can verify in its own code. The report is also shown by the read-only doctor and in provider settings. Tests cover Windows, macOS, and Linux decisions and check that macOS/Linux provider-key storage fails closed.

| OS | Encrypted provider-key backend | Metered/trial API route | Strict isolation of opaque CLI native tools |
| --- | --- | --- | --- |
| Windows | User-scoped DPAPI, with a dummy-key round trip in Windows tests | Allowed only after the existing route approval, price/terms, privacy, account, and budget checks | Unverified |
| macOS | None verified in this release | Blocked | Unverified |
| Linux | None verified in this release | Blocked | Unverified |

The router and final inference policy share `evaluate_eligibility`, so an unsupported metered route is rejected both during selection and before dispatch. Provider settings disables key storage and paid-route enable controls where the backend is unavailable. This does not claim that every Windows third-party CLI is sandboxed or that an opaque subscription's internal tokens can be capped. The app records opaque costs as unknown, and its retry policy stops after an uncertain native-tool outcome. A future macOS Keychain or Linux Secret Service backend needs real host tests before the paid API restriction changes.
