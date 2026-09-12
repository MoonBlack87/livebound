---
title: "Testing"
description: "What Livebound’s automated suite covers and why live CivitAI smoke testing stays local."
weight: 60
---
Livebound's automated tests focus on behaviour where a quiet regression would be expensive: files on disk, CivitAI publishing state, scheduling rules, metadata transformations, backups, and recovery after interrupted work.

## Automated checks

The normal review checks are:

```bash
.venv/bin/python -m pytest test -q
.venv/bin/ruff check backend test
cd frontend && npm run build
```

A release carries `test/backend` and the fixtures it needs, not the tooling
tests: those cover the release export itself, which never ships, and they
necessarily quote the very words its content gate forbids.

CI runs the supported Python/frontend build path on Linux. Platform-specific starter tests also cover important Windows and packaging behaviour without requiring CI to operate a Windows desktop session.

The test fixtures include representative generation metadata so parser changes can be checked against known source examples rather than only synthetic strings.

## Areas covered by the suite

Among other things, tests exercise:

- scans, hashes, duplicate grouping, trash and restore;
- metadata reading, editing, upload copies and resource attribution;
- post preflight, draft-first push order, retry/resume and reconciliation;
- scheduling boundaries and calendar calculations;
- adoption, local matching and sync behaviour;
- archive moves and database backups;
- OAuth/security boundaries and first-start setup;
- optional language-model request/response handling.

The project does not publish a coverage-percentage promise. Passing the behavioural suite and adding regression tests for fixed failures is more useful than treating one number as a release guarantee.

## Live CivitAI smoke test

A separate live smoke test exists for maintainers/developers who deliberately provide a real CivitAI account and write access. It is not run in CI because CI must not hold a creator's credentials or make remote account changes.

The live test uses an explicit confirmation gate, marks the remote objects it creates, and is designed to clean up only its own temporary post. It is a release verification tool, not a normal user command.
