# Contributing

Thanks for your interest in WittyCode.

## Issues and ideas

- Bugs: open an [issue](https://github.com/WittyRyuji/WittyCode/issues) with the version, what you did, what you expected
  and what happened. Leave out personal data (paths with your user name, keys, account details).
- Ideas and questions: [Discussions](https://github.com/WittyRyuji/WittyCode/discussions).
- Security problems: see [SECURITY.md](SECURITY.md), not a public issue.

## Pull requests

**Every outside pull request is reviewed and audited by the maintainer before anything is merged, and not every pull request
will be merged.** WittyCode runs commands on users' computers, so changes are read line by line, and a change can be declined
when it does not fit the project's direction, adds a dependency, or cannot be checked. For anything larger than a small fix,
please open an issue or a discussion first so the approach can be agreed on before you spend time on it.

Workflows of a pull request from a fork run only after the maintainer approves them.

What a pull request needs:

- **Only Python and React (TypeScript).** No other languages, not even for small tools or installers.
- **Tests for every change**, and the whole suite green:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
  cd frontend; npm run typecheck; npm run unit; npm run build:app; node scripts/check.mjs; node scripts/check-live.mjs
  ```

  A change to the interface needs the page rebuilt (`npm run build:app`) and a look at it (a screenshot in the pull request
  helps).
- **No new dependency without a reason** given in the pull request; versions are pinned in `constraints.txt` /
  `package-lock.json`, and a version published less than a week ago is not taken.
- **Nothing private in the repository**: no real paths or user names, keys or tokens, e-mail addresses, account or session
  ids. Tests and examples use neutral names (`demo@example.com`, `C:\Users\me\…`).
- **WittyCode stays independent**: the core must not depend on another agent program's CLI, SDK or settings files.
- Commit titles in English, one imperative line; the body says what and why.

By contributing you agree that your contribution is licensed under the project's [Apache License 2.0](LICENSE).
