---
title: "Starter and CLI Options"
description: "Useful webui starter modes and command-line entry points."
weight: 45
---
The normal entry points are `webui.sh` on Linux/WSL and `webui.bat` on Windows.

## Starter options

```text
--update      update a Git checkout and refresh the environment
--dev         run with frontend development tooling
--llm         install the optional local-model environment (once, not on every start)
--skip-build  start without rebuilding the frontend
--help        print the starter's own notes
```

`--update` is only for a Git checkout. It runs `git pull --ff-only`, refreshes the application environment and built frontend when needed, then starts Livebound. A fast-forward-only pull does not overwrite local Git work: if Git cannot update safely, the starter stops instead. Because the checkout remains in the same application folder, its installation-local `.livebound/config.json` remains in place and continues to select the same data directory.

An extracted release ZIP has no Git history and should be updated by downloading and starting the newer ZIP. The newly extracted folder is a separate installation, so its first start asks which data directory to use. See [Installation]({{< relref "/getting-started/installation.md" >}}) for the complete ZIP and Git update steps.

**Anything the starter does not recognise is passed on to the application**, so
`./webui.sh --port 9000 --no-browser` works — both belong to the `serve` command
below. The one exception is `--dev`, where the starter passes `--no-browser`
itself because the frontend development server provides the interface.

Local starter settings can be placed in `webui.settings.sh` or
`webui.settings.bat` next to the starter. They set `PYTHON`, `VENV_DIR` and
`COMMANDLINE_ARGS` — the interpreter to use, where the environment lives, and
arguments to add to every start.

## Backend CLI

The Python package exposes five commands:

```text
serve     start the server (--port, --no-browser, --reload)
scan      walk every source folder once
backup    back the database up, or list existing backups
resume    clean up interrupted runs and report
starter   evaluate platform starter decisions
```

For ordinary use, prefer the browser UI and the starter scripts. Reach for the
CLI when documentation or troubleshooting specifically calls for it.
