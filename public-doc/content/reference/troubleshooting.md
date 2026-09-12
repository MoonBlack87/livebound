---
title: "Troubleshooting"
description: "Common startup, metadata, publishing, backup, and local-model problems."
weight: 50
---
## Livebound cannot find Python on Windows

Livebound requires Python 3.12 or newer. Windows can expose Microsoft Store placeholders named `python.exe` or `python3.exe` even when they are not usable interpreters.

Either disable those aliases under Windows **App execution aliases**, or set an explicit interpreter in `webui.settings.bat`:

```bat
set "PYTHON=C:\Path\To\Python312\python.exe"
```

## The browser did not open

The backend normally prints its local address when it starts. Open:

```text
http://127.0.0.1:8430
```

If you changed the port, use the address shown by the starter.

## A released ZIP says the frontend is missing

A proper public release contains the built frontend. If a checkout contains only frontend source and no built assets, it is likely a development tree rather than a release export.

Download the ZIP again from the [Livebound Releases page](https://github.com/MoonBlack87/livebound/releases) and follow the [installation steps]({{< relref "/getting-started/installation.md" >}}). For frontend development, install Node.js 20+ and use `--dev` or build the frontend.

## `--update` stops instead of replacing local files

This is intentional for a Git checkout. The starter uses `git pull --ff-only`, so it refuses an update that Git cannot apply as a safe fast-forward instead of overwriting local Git work. Resolve the checkout's Git state, then run `--update` again.

If you installed Livebound from a release ZIP, do not use `--update`. Download and extract the newer ZIP as a new application folder. That new folder has its own `.livebound/config.json`, so first start appears again; choose the existing Livebound data directory to keep using the same state. If the old installation's data is still under its own `.livebound/data`, do not delete that old folder until the data has been moved elsewhere. The [installation guide]({{< relref "/getting-started/installation.md" >}}) gives the full update sequence.

## A scan cannot read one image

The scanner continues past unreadable images. Check the scan/job result for the affected file, verify the file can be opened normally, and rescan after replacing or repairing it.

## Generation metadata is missing

The image may have no embedded generation data, may use a metadata layout Livebound does not currently support, or may have had its metadata removed before it reached your library.

The image can still be used in a post. Add or correct the information you actually know rather than assuming missing fields.

## A CivitAI post differs from the local post

Run **Reconcile with CivitAI**. Livebound represents local/remote divergence instead of silently selecting one side. Open the post and choose whether to take the remote state or send the local state where possible.

If the difference affects a field CivitAI does not allow Livebound to change after creation, the UI may require a remote rebuild. Read the confirmation carefully because a rebuild deletes and recreates the post.

## A scheduled post is now too close to publish

CivitAI requires advance lead time for scheduled publication. An absolute time that no longer clears the platform floor is not silently shifted by Livebound. Choose a later time or explicitly switch the publishing mode.

## A backup restore disconnected CivitAI

This is expected. OAuth credentials are removed from all backups. Connect the CivitAI account again after restore.

## A recurring error needs a bug report

1. Open Settings → General.
2. Enable **Record diagnostics to reproduce a recurring error and attach them to a report**.
3. Reproduce the issue.
4. Find `diagnostics.log` in the Livebound data directory.
5. Review it and attach it to the public issue if appropriate.

Do not attach a database backup to a public issue.