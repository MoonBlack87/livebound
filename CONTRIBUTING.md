# Contributing to Livebound

Bug reports, focused fixes, documentation improvements and parser support are welcome.

## Report a bug

Open an issue in this public repository and include:

- what you were doing;
- what you expected;
- what happened instead;
- your operating system and Livebound version;
- the smallest reproducible example you can share safely.

For a recurring error, enable **Settings → General → Record diagnostics to reproduce a recurring error and attach them to a report**, reproduce the problem, and attach `diagnostics.log` from your Livebound data directory.

The diagnostics log is designed for bug reports and omits authentication data and request bodies. A **database backup is not a diagnostic file**: it can contain private paths, prompts, post text, metadata and account-related state, so do not attach one to a public issue.

## How development reaches this repository

The public repository receives reviewed release snapshots rather than every intermediate development commit. Development happens in a separate working repository; the public repository is the release and contribution surface.

A pull request opened here is still a normal contribution. It can be reviewed and merged here, then carried back into subsequent development so it remains part of later releases. You do not need access to the working repository to contribute.

## What a change should include

A contribution should:

- solve one clear problem;
- run on the supported application path it changes;
- keep the relevant tests passing and add a regression test when behaviour changes;
- keep the frontend build clean when frontend code changes;
- avoid adding a new dependency unless the change needs it and the reason is clear;
- preserve existing user data and original image files unless the feature explicitly performs a user-confirmed move or delete.

For metadata parsers, real sample metadata and focused tests are especially valuable. Please do not copy implementation code from projects whose licences are incompatible with Livebound.

## Useful checks

From a prepared development checkout:

```bash
.venv/bin/python -m pytest test -q
.venv/bin/ruff check backend test
cd frontend && npm run build
```

Windows contributors can run the equivalent commands with the Python executable inside `.venv\Scripts\`.

See the documentation testing page for what the automated suite covers and why the live CivitAI smoke test is kept out of CI.

## Licence and copyright

Livebound is licensed under **AGPL-3.0-or-later**. There is no contributor licence agreement (CLA).

You keep the copyright in your contribution. By contributing it to this project, you agree that your contribution is provided under the project's AGPL-3.0-or-later licence.
