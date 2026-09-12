---
title: "Data and Storage"
description: "Where Livebound stores application state, thumbnails, source references, and archive data."
weight: 25
---
Livebound separates its own application data from the image folders you manage with it.

## Livebound data directory

The data directory contains application-managed state such as:

- the SQLite database;
- thumbnails/cache data;
- database backups;
- the diagnostic log when used.

The database records posts, schedules, metadata edits, configured paths, duplicate decisions, publishing history, settings and other Livebound state.

Each application folder points at its own data directory, so a second installation is a second library with nothing shared between them. See [Keep two libraries separate]({{< relref "getting-started/installation#keep-two-libraries-separate" >}}).

## Source images

Source images stay in the source folders you configured. They are not copied into the SQLite database and metadata edits do not rewrite them.

The exceptions are explicit file-management commands:

- the trash can move a file into the configured recoverable trash folder, whether it was sent there from the duplicate finder or from the library;
- restore can move it back;
- archive can move a verified published file into the configured archive folder;
- permanent delete removes a selected file only after confirmation.

## Adopted images

Images fetched from existing CivitAI posts are stored in the separately configured adopted-images folder and added to the Library.

## Moving the data directory

Livebound can relocate its own data directory. It takes a backup before the move and moves the application database, thumbnail cache and backups together.

Your source, adopted, trash and archive image files are not moved merely because the Livebound data directory changes.

## Backups are not image backups

A Livebound database backup protects the application state described above. Back up your source folders and archive separately using your normal file-backup strategy or Livebound's separate archive-ZIP helper.
