---
title: "Automatic Checks"
description: "Understand the background checks Livebound can perform for active posts and watched folders."
weight: 65
---
Livebound performs a small number of local background checks so active work does not require constant manual refreshes.

## Watched image folders

A source folder with **Watch** enabled is rescanned every 15 minutes. The pass records new or changed images, but it leaves entries for files that vanished until you start a manual scan. It does not move or delete source images.

## Active CivitAI posts

Livebound can periodically re-check active posts that already have a remote identity. The purpose is to notice meaningful changes such as publication, missing remote state, or a local/remote divergence.

The checker does not create a post on its own and does not turn a local draft into a CivitAI draft merely because time passed.

It also looks after images that are held back as already posted because of a post that no longer exists. Deleting a post in Livebound and then deleting it on CivitAI by hand leaves nothing to sync, and those pictures would stay blocked for ever. The checker asks CivitAI about such posts, one request per post rather than per image, and releases their images only when CivitAI answers that the post is gone. A refused or failed request changes nothing: letting a picture go because the network hiccuped is how the same picture gets posted twice.

## Notifications

Background work should surface a message only when something meaningful changed or needs attention, rather than repeatedly reporting the same unchanged state.

## What it does not do

The automatic checker does **not** resolve a local/remote divergence for you and does not move image files. Archived posts are no longer part of this active checking loop.

If the checker reports a divergence or incomplete state, open the post or Review view and decide what should happen next.
