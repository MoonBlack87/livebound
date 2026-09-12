---
title: "Preparing and Publishing Posts"
description: "Turn selected images into CivitAI drafts, review them, and publish when ready."
weight: 50
---
Create a post from selected Library images or start from the Board and choose images.

{{< screenshot src="images/docs/publishing/post-editor.jpg" title="Post editor" alt="Post editor" request="Capture a prepared post with multiple images and the main title/description/tag/resource controls visible. Use publishable sample content." >}}

A local post can hold:

- ordered images;
- title and description;
- tags;
- per-image generation metadata visibility;
- resource attribution;
- a draft, immediate, absolute, or relative publishing plan.

## Preflight

Before a push, Livebound builds a preflight view. It separates blockers from warnings and shows the remote operations the run expects to perform.

Examples of blockers include missing local files, invalid scheduling, or media that does not meet the upload requirements. Warnings can include unresolved resource attribution or other items that need a creator's judgement rather than an automatic stop.

## Draft-first publishing

A normal push creates or uses a CivitAI draft first. Images and their settings are applied before Livebound sets the final publication time.

While that run is going, the post belongs to it. The editor still shows everything, but the actions that would change what is being uploaded — editing, reordering, removing an image, syncing, deleting, marking ready — are unavailable until the push finishes. It is usually a short wait, and it is the difference between a post that arrives as you left it and one that does not.

This means a failure part-way through an upload is designed to leave a recoverable draft rather than a partly assembled public post.

Publishing runs retain progress information so an interrupted run can be resumed. When the outcome of a remote create request is uncertain, Livebound reconciles before attempting another create so it does not casually duplicate the post.

## Publish immediately

Immediate publication is an explicit choice and asks for confirmation.

CivitAI has no ordinary "unpublish" operation for a published post. Returning from published to private means deleting the remote post, which can lose reactions and other remote state. Treat **Publish now** as an irreversible publication decision.

## Twenty images to a post

A post holds at most twenty images, which is where CivitAI's own uploader stops. Livebound refuses a larger selection when you build the post rather than letting you find out at the push, and it tells you how many you chose so you know how many to take out.

It does not quietly drop the extras. Which images to leave out is a decision about your own work, and an application that makes it for you silently is one you cannot trust with the rest.

Posts adopted from CivitAI are not affected: those mirror what is already on the site.

## Remote constraints

Some CivitAI fields cannot be freely changed after remote creation. When Livebound detects a change that requires a new remote post, it explains the situation and offers a rebuild flow rather than pretending the edit succeeded.

A rebuild deletes and recreates the CivitAI post and therefore requires explicit confirmation.
