---
title: "Installation"
description: "Run Livebound from a release ZIP or Git checkout on Windows, Linux, or WSL."
weight: 10
---
Livebound is a local browser application. The backend runs on your machine and opens the interface in your normal web browser.

## Supported and tested paths

- **Windows:** use `webui.bat`.
- **Linux:** use `./webui.sh`.
- **WSL:** use `./webui.sh` inside WSL.
- **macOS:** the shell path is present but has not been tested. Treat it as use-at-your-own-risk.

Python **3.12 or newer** is required.

A released version already includes the built frontend, so **Node.js is not required to run Livebound**. Node.js 20 or newer is only needed for frontend development or `--dev` mode.

## Recommended: install from Git

Git is the recommended route because one command updates the same folder while keeping its installation-local configuration and virtual environment. Install [Git](https://git-scm.com/) once, and updates are a command from then on.

Linux/WSL:

```bash
git clone https://github.com/MoonBlack87/livebound.git
cd livebound
./webui.sh
```

Windows:

```bat
git clone https://github.com/MoonBlack87/livebound.git
cd livebound
webui.bat
```

The public `main` branch represents the current released source. Public release tags can be used when you need a specific version.

### Update a Git checkout

Close Livebound and run the starter's update mode from the checkout.

Linux/WSL:

```bash
./webui.sh --update
```

Windows:

```bat
webui.bat --update
```

The starter runs `git pull --ff-only`, refreshes the local application environment and built frontend when needed, then starts Livebound again. Fast-forward-only means it does not overwrite local Git work. If Git cannot update the checkout safely because the histories or local work need attention, the command stops instead; resolve that Git state before trying the update again.

Because this updates the same checkout in place, its installation-local `.livebound/config.json` remains there and continues to select the same data directory.

## ZIP installation

The ZIP route is fully supported and does not require Git.

1. Open the [Livebound Releases page](https://github.com/MoonBlack87/livebound/releases).
2. Open the release you want and download **Source code (zip)**.
3. Extract the ZIP to a normal writable folder. Do not run Livebound from inside the ZIP viewer.
4. Start Livebound with `webui.bat` on Windows or `./webui.sh` on Linux/WSL.
5. Complete [first start]({{< relref "/getting-started/first-start.md" >}}). Choose the data directory this installation should use for its database, settings and other application state.

### Updating a ZIP installation quickly

Close Livebound, extract the newer ZIP, and copy its contents over the existing application folder. Then start Livebound again. The existing `.livebound/config.json` and `.venv/` remain, so configuration survives and the environment does not need to be rebuilt. The cost is that files removed from the new release remain as leftovers in the folder.

### Updating a ZIP installation cleanly

Extract the newer ZIP into a fresh folder, then bring the existing `.livebound/` directory across before starting. This leaves no old application files behind. The `.venv/` is not carried across, so the next start rebuilds it and takes a few minutes.

One thing to watch: if you accepted the default at first start, your data is in `<old-application-folder>/.livebound/data` — database, thumbnails and backups, easily gigabytes. Move that directory, do not copy it, or you will have two complete data sets and may keep working on the wrong one. Confirm the new copy opens the expected library and settings before removing the old folder.

## Windows Python detection

The Windows starter looks for a usable Python 3.12+ installation. If Windows exposes only the Microsoft Store `python.exe` placeholder, either disable the App Execution Alias for that placeholder or point Livebound at the real interpreter in `webui.settings.bat`:

```bat
set "PYTHON=C:\Path\To\Python312\python.exe"
```

## Keep two libraries separate

To keep two bodies of work separate today, extract a second Livebound ZIP into
its own application folder. Start it and, at first start, choose a new data
directory rather than the one used by the first installation. Complete its own
CivitAI connection as well.

The two installations cannot use the default address at the same time. Start
the second one with a different port, for example:

Linux/WSL:

```bash
./webui.sh --port 8431
```

Windows:

```bat
webui.bat --port 8431
```

To keep that port for later starts, use the platform-specific setting in the
second installation.

Linux/WSL, in `webui.settings.sh`:

```bash
COMMANDLINE_ARGS=(--port 8431)
```

Windows, in `webui.settings.bat`:

```bat
set "COMMANDLINE_ARGS=--port 8431"
```

See [Local starter settings](#local-starter-settings) for the other settings
these files can hold.

Two installations cost two of everything. Each has to be updated separately, and
neither knows the other's posts: the question "have I already posted this?" comes
straight back at the boundary between the two libraries, which is the question
the usage history exists to answer. This is the route available today. A better
way to keep work apart is intended and not built yet.

## Local starter settings

Optional files next to the starters can hold machine-specific settings:

- `webui.settings.sh`
- `webui.settings.bat`

They can set the Python executable, virtual-environment location, or command-line arguments without changing the starter itself.

## Default address

The normal backend listens on:

```text
http://127.0.0.1:8430
```

The starter opens that address automatically unless `--no-browser` is passed through to the server.
