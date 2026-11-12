# Roadmap

English | [简体中文](ROADMAP.zh-CN.md)

What WittyCode plans to do, in rough order of priority. There are no dates: items move when they are ready, and the order can change. Done items leave this page and go into the release notes.

## Now

- **Finish the independence work**: WittyCode runs on its own agent loop (ChatGPT Plan, API providers) with Devin as an optional engine, and depends on no other agent program. Left to do: stop reading other apps' skill settings (`disabledSkills`, plugins) and keep only their skill folders as a read-only fallback; clean the last mentions in comments and docs; retake the README screenshots.
- **First alpha release** (`v0.1.0-alpha.1`): built and published by GitHub Actions, with a zip, its SHA-256 and a build provenance attestation. Not code-signed.
- **CI that stays green**: the unit and page checks on every push and pull request; the whole-window tests and mutation tests stay local.

## Next

- **A sandbox for the agent loop**: today the loop decides permissions from resolved paths and command text, with no OS-level isolation. The plan is to run commands in an isolated environment, reusing an existing open-source sandbox rather than writing one. Candidates (MIT-licensed first, licenses to be reviewed before anything is taken): [microsoft/quicksand](https://github.com/microsoft/quicksand), [vndee/llm-sandbox](https://github.com/vndee/llm-sandbox).
- **Safer defaults for unattended runs**: tighter defaults for `/goal` and long loops, clearer warnings before the `full` level.
- **Code signing**: apply to a free code-signing program for open-source projects (for example SignPath Foundation) once the project meets its requirements; until then releases are unsigned.
- **Automatic updates**: check for a new release and update in place, verified against the published checksum.
- **Package managers**: winget and Scoop manifests.
- **English interface**, then other interface languages.

## Later

- **Sub-agents**: the page can show sub-agents, but no engine of this version starts one yet.
- **Bedrock on a real account**: complete a real request end to end (the request signing is already checked against botocore).
- **Agent-loop review follow-ups** (items L2–L12 of the loop review in `docs/`).
- **Asking mid-turn on more engines** where the engine supports it.
- **Skill and plugin safety checks** before their instructions reach a model.

## Not planned for now

- macOS and Linux: the app, its tests and its release are Windows-only for now.

Ideas and requests are welcome in [Discussions](https://github.com/WittyRyuji/WittyCode/discussions); bugs go to [Issues](https://github.com/WittyRyuji/WittyCode/issues).
