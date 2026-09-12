---
title: "Settings Overview"
description: "Configure Livebound’s local behaviour, sources, metadata, models, CivitAI, LLMs, and archive."
weight: 100
---
Settings are grouped by the part of Livebound they affect.

{{< gallery label="The settings, by area" >}}
{{< slide src="images/docs/settings/tour/01.jpg" caption="General — language, how close two pictures have to be to count as the same, and the switches that decide what runs by itself." >}}
{{< slide src="images/docs/settings/tour/02.jpg" caption="Further down: where the database lives, and the backups. The OAuth connection is stripped from every one of them." >}}
{{< slide src="images/docs/settings/tour/03.jpg" caption="Images — the folders Livebound reads. Watching is off by default, and a scan only ever reads." >}}
{{< slide src="images/docs/settings/tour/04.jpg" caption="The three destinations below them: archive, adopted, trash. Each says what it does before you point it anywhere." >}}
{{< slide src="images/docs/settings/tour/05.jpg" caption="Metadata — every field your own files actually carry, each switchable out of the upload, with the warning where removing it changes how CivitAI reads the picture." >}}
{{< slide src="images/docs/settings/tour/06.jpg" caption="Models and resources — the folders to hash, and how many of their files CivitAI recognised." >}}
{{< slide src="images/docs/settings/tour/07.jpg" caption="CivitAI — the connection, the five permissions it was given, and where links, site API calls and MCP calls go." >}}
{{< slide src="images/docs/settings/tour/08.jpg" caption="Local LLM — optional, on your own machine or an Ollama server, with profiles that decide how far it may go from what is visibly there." >}}
{{< slide src="images/docs/settings/tour/09.jpg" caption="About — the version, the licence, what this is and what it is not: an independent project, not affiliated with CivitAI." >}}
{{< /gallery >}}

## General

Use General settings for application behaviour, the data directory, backups, diagnostics, language, a local daily-post planning limit, and the optional [usage ping]({{< relref "/reference/usage-ping.md" >}}).

The daily limit is Livebound's own planning aid. It does not override CivitAI's server-side limits.

Moving the Livebound data directory moves the application database, thumbnails and backups together. Source images are not part of that move.

## Images

Manage:

- Library source folders;
- per-source Watch scanning;
- the archive folder;
- the adopted-images folder;
- the recoverable trash folder;
- optional generation-time filename patterns used when reviewing duplicate groups.

These locations have different purposes and should not overlap accidentally.

## Metadata

Choose upload metadata fields to exclude and maintain prompt-exclusion patterns. The list of fields grows from generation parameters Livebound has encountered during scans.

## Models and resources

Configure local model directories, scan their inventory, and review CivitAI identity for local resources.

## CivitAI

Connect or disconnect the OAuth account and review the current connection. Disconnecting forgets Livebound's local tokens; revoking an application's authorization is done in the CivitAI account itself.

## Local language model

Choose between a local model folder and an Ollama server, manage profiles, and control optional vision/model-loading settings.

## About Livebound

Shows the running version, licence, rights holder, project links, a description of Livebound, and the terms of use.
