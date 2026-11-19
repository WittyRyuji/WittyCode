# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub's
[private vulnerability reporting](https://github.com/WittyRyuji/WittyCode/security/advisories/new) for this repository.

Include what you found, the version (or commit) and the steps to reproduce it. You will get an answer as soon as the
maintainer can look at it; please give a reasonable time for a fix before telling anyone else.

## Supported versions

WittyCode is in alpha. Only the latest release and the `main` branch get security fixes.

## What to know before you use it

- **The agent loop runs commands and edits files on your computer.** Permissions are decided from resolved paths and
  command text; there is no operating-system sandbox yet (see the [Roadmap](ROADMAP.md)). In a repository you do not trust,
  use the `ask` level, never `full`.
- **Use an environment variable for API keys when possible.** WittyCode also supports a configured key command, or direct entry (not recommended). Newly entered keys are stored as Windows current-user DPAPI ciphertext in `models.json`, bound to the interface type and Base URL. If encryption fails, the app refuses to save the key. DPAPI does not protect against programs running as your Windows user, and providers receive the key in their requests; only enter keys for endpoints you trust. Old plaintext keys and prior backups may still exist; rotate old keys if they were exposed. ChatGPT sign-in tokens are kept in WittyCode's own folder (`%APPDATA%\WittyCode\`).
- **No telemetry.** Conversations, memory and cache stay on your machine; requests go only to the model services you set up.
- **Releases are not code-signed yet.** Check the zip against the published `.sha256` file and the build provenance
  attestation on the release page.

## Dependencies

Dependencies are pinned (`constraints.txt`, `frontend/package-lock.json`); Dependabot proposes updates after a waiting period,
and CodeQL scans the code. A new dependency or an update is reviewed before it is merged.
