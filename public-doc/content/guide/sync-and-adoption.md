---
title: "Sync and Existing CivitAI Posts"
description: "Bring existing CivitAI posts into Livebound and reconcile local and remote state."
weight: 70
---
Livebound keeps local post state and CivitAI post state separate so a change on one side does not silently overwrite the other.

{{< gallery label="Bringing a post that already exists on CivitAI into Livebound" >}}
{{< slide src="images/docs/civitai/adoption/01.jpg" caption="What CivitAI has and Livebound does not. Browse the account, or paste post ids; nothing is adopted until you say so." >}}
{{< slide src="images/docs/civitai/adoption/02.jpg" caption="Adopted — the post is on the board with its state, and the toolbar says how many of its images have no local file yet." >}}
{{< slide src="images/docs/civitai/adoption/03.jpg" caption="Inside, each of those is marked only on CivitAI. You can search your own folders for them, or download them." >}}
{{< slide src="images/docs/civitai/adoption/04.jpg" caption="The search runs over your own folders and matches what it recognises; what it cannot find stays marked." >}}
{{< slide src="images/docs/civitai/adoption/05.jpg" caption="Matched: the pictures are local files again, and the match on any one of them can be changed by hand." >}}
{{< slide src="images/docs/civitai/adoption/06.jpg" caption="A downloaded one lands in the adopted folder with its prompt and parameters — and the duplicate memory already knows it was in a post." >}}
{{< /gallery >}}

## Reconcile with CivitAI

Use **Reconcile with CivitAI** from the Board to refresh remote facts for posts Livebound already knows.

If local and remote editable values differ, Livebound marks the divergence for review. It does not automatically decide which side should win. The post editor lets you choose whether to take the remote state or send the local state where the platform allows it.

If a post was deleted on CivitAI, Livebound can represent that as a missing-remote state instead of silently recreating it.

## Discover posts that are not local yet

The discovery flow can find posts from the connected CivitAI account that are not recorded locally and adopt them into Livebound.

Discovery can be narrowed by date and remote state. CivitAI drafts that are not discoverable through the available listing interface can still be adopted by entering the post ID from CivitAI.

Adoption is for the connected user's own posts.

## Matching remote images to local files

For adopted posts, Livebound can search the configured local sources for the corresponding local image. Exact image identity is preferred; other image facts are used to present candidates when a perfect local identity is not already known.

When no local copy exists, Livebound can fetch remote post images into a configured **adopted images** folder and add them to the Library. Configure that folder separately from source and archive locations before fetching.

The local library is image-focused; remote video media is not downloaded into the image library by this workflow.
