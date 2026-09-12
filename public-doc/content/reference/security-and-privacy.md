---
title: "Security and Privacy"
description: "Local-first boundaries, credentials, diagnostics, optional model backends, and LAN considerations."
weight: 30
---
Livebound is designed as a local, single-user application. Local-first does not mean network-free: publishing and CivitAI features necessarily communicate with CivitAI, and an optional model backend can introduce another network destination.

## Local application and browser

By default Livebound listens only on `127.0.0.1` and opens a normal browser interface on the same machine.

The current application has no Livebound user-login layer. Do not bind or proxy it onto an untrusted network and treat it as a secured multi-user service.

## CivitAI OAuth

Livebound uses OAuth rather than an API key. The connection requests the permissions needed to read the connected user's profile/models/media and to create, update or delete media as requested by publishing workflows.

OAuth credentials are stored in the local Livebound database. They are not returned to the browser frontend, are excluded from application diagnostics, and are stripped from every database backup.

Disconnecting in Livebound removes the local connection. If you also want to revoke the application's authorization at CivitAI, do that from the CivitAI account's authorized-app settings.

## What publishing sends to CivitAI

A publishing run can send the information needed for the post you configured, including:

- image upload data;
- title and description;
- tags;
- publish timing;
- selected generation metadata;
- resource/model attribution;
- per-image settings such as CivitAI's metadata-visibility choice.

The preflight view exists so you can review what is about to happen before remote writes begin.

## hideMeta

CivitAI's hide-metadata setting controls public display. It does **not** mean the generation metadata was never sent to CivitAI. If data is included in the upload, CivitAI receives it even when public display is disabled.

## Diagnostics

Diagnostics recording is off by default. When enabled for a bug report, Livebound writes a bounded local log that excludes authentication data and request bodies. Review the file before sharing it.

A database backup is not suitable as a public diagnostic attachment because it contains much more local application state.

## Optional usage ping

The usage ping is off until you opt in. Once enabled it sends only one stable random installation UUID on application start. It does not include your account, prompts, posts, image library, OS, model names, or application version.

Both sides are readable: [`backend/usage_ping.py`](https://github.com/MoonBlack87/livebound/blob/main/backend/usage_ping.py) is what sends, [`ping-server/main.go`](https://github.com/MoonBlack87/livebound/blob/main/ping-server/main.go) is what receives, and [Usage Ping]({{< relref "/reference/usage-ping.md" >}}) walks through both.

## This documentation website

The pages you are reading are not the application. They are a static site on GitHub Pages, and they count visits with [Umami](https://umami.is), a self-hosted analytics service on the maintainer's own server. It records which page was opened, where the visit came from, and coarse technical details such as browser, operating system and country. It sets no cookies, stores no IP address, and does not follow you to other sites.

**It is the same analytics installation the usage ping ends up in.** The ping does not go there directly — it goes to Livebound's own receiver, which forwards the installation UUID as a startup event, as [Usage Ping]({{< relref "/reference/usage-ping.md" >}}) describes. Saying the two were unrelated would be neater and untrue.

Livebound sends no shared identifier that links the two data sets. A page view here carries no installation UUID; a startup event carries no page, no referrer and no browser. Reading this documentation therefore sends no Livebound identifier, and the ping is off until you switch it on.

The site's own source carries neither the analytics address nor its identifier: both are supplied by the build, so anyone who forks this repository and builds these pages reports nowhere.

## Optional model backends

A local model worker runs as a separate local process when configured. An Ollama server can be local or remote. If you configure a remote Ollama endpoint, prompt text and any selected image context are sent to that server.

## Backups

Database backups remove Livebound's OAuth connection before being kept. They can still contain private prompts, paths, post content, schedules and history, so keep them private.
