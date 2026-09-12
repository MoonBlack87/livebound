---
title: "FAQ"
description: "Short answers to common questions about Livebound’s local workflow and CivitAI integration."
weight: 70
---
## Is Livebound a native desktop application?

No. Livebound runs a local backend and provides its interface as a browser web application. The default server is available only on the local machine. You can use multiple browser tabs or windows if that fits your workflow.

## Does Livebound upload my whole library?

No. Scanning and organising the Library are local operations. CivitAI receives media and post data when you perform CivitAI actions such as publishing, syncing, resource lookup, or fetching remote images.

## Does editing metadata change my original image?

No. Metadata edits are stored in Livebound. Publishing uses a temporary upload copy when it needs to reflect those edits.

File-management actions are separate: trash and archive can move files when you explicitly run them, and permanent deletion requires confirmation.

## Can I run Livebound from a ZIP?

Yes. Extract a released ZIP and run `webui.bat` on Windows or `./webui.sh` on Linux/WSL.

A Git checkout is also supported and has the convenience of `--update`.

## Do I need Node.js?

Not to run a release. Node.js 20+ is needed when changing the frontend or using frontend development mode.

## Do I need a local language model?

No. It is optional. All core publishing features work without it.

## Can I use an Ollama server on another machine?

Yes, if it is reachable at the URL you configure. Remember that prompt text and any selected image context are then sent to that server.

## Why did my CivitAI connection disappear after restoring a backup?

Livebound deliberately strips OAuth credentials from every backup. Reconnect the CivitAI account after restore.

## Does "hide metadata" keep generation data away from CivitAI?

No. It controls CivitAI's public display of the generation data. Metadata selected for the upload is still sent to CivitAI.

## Can Livebound schedule posts automatically?

Livebound can carry out a schedule you configure and can retry/reconcile publishing work. You remain the person choosing the post, content, account and publishing plan. It is not an autonomous engagement or statistics-manipulation bot.

## Can I expose Livebound on my LAN?

The current release is designed for local single-user use and has no Livebound login layer. Do not expose it to an untrusted network as though it were a secured multi-user server.

## Can Livebound discover every draft on my CivitAI account?

Not through the same remote listing used for published/scheduled discovery. If a CivitAI draft is not discoverable there, enter its post ID manually to adopt it.

## Can I undo Publish now?

CivitAI does not provide a normal unpublish operation. Deleting the remote post is the way back, and that can lose remote reactions/statistics. Livebound therefore asks for confirmation before immediate publication.

## What does the optional usage ping send?

Only one stable random installation UUID, and only after you opt in. The receiver source is published with Livebound as well. See [Usage Ping]({{< relref "/reference/usage-ping.md" >}}) for the exact request and receiver behaviour.
