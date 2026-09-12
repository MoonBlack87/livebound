---
title: "Board and Post States"
description: "Understand the Board and the local states Livebound uses around publication."
weight: 55
---
The Board is the overview of posts that are still part of the active publishing workflow.

{{< gallery label="One post, from the board to CivitAI" >}}
{{< slide src="images/docs/publishing/walkthrough/01.jpg" caption="The board — five columns, and a post sits in exactly one of them." >}}
{{< slide src="images/docs/publishing/walkthrough/02.jpg" caption="The editor — title, description and tags, with the checklist saying what is still missing." >}}
{{< slide src="images/docs/publishing/walkthrough/03.jpg" caption="A relative time under CivitAI’s minimum is raised to the earliest allowed one, and says so rather than failing later." >}}
{{< slide src="images/docs/publishing/walkthrough/04.jpg" caption="Ready — the planned time is fixed, and Push to CivitAI is the only thing left." >}}
{{< slide src="images/docs/publishing/walkthrough/05.jpg" caption="The push uploads one image at a time, and it can be cancelled while it runs." >}}
{{< slide src="images/docs/publishing/walkthrough/06.jpg" caption="Then the text and the tags. Each step is its own, so an interrupted push resumes rather than restarts." >}}
{{< slide src="images/docs/publishing/walkthrough/07.jpg" caption="Done — and the post has moved a column, from Ready to Scheduled." >}}
{{< slide src="images/docs/publishing/walkthrough/08.jpg" caption="Inside it, the time CivitAI holds is shown apart from the one in the field: changing it here needs Reschedule to travel. The images still carry a question mark." >}}
{{< slide src="images/docs/publishing/walkthrough/09.jpg" caption="That question mark is CivitAI’s rating, which arrives minutes later on its own." >}}
{{< slide src="images/docs/publishing/walkthrough/10.jpg" caption="The post on CivitAI, before it has been rated there." >}}
{{< slide src="images/docs/publishing/walkthrough/11.jpg" caption="Scrolled down: the tags and the description arrived with it." >}}
{{< slide src="images/docs/publishing/walkthrough/12.jpg" caption="And in CivitAI’s own editor, under Resources, the model the pictures were made with — the credit that carried across." >}}
{{< slide src="images/docs/publishing/walkthrough/13.jpg" caption="Finally the rating, on the picture, on CivitAI." >}}
{{< /gallery >}}

## Board columns

Posts are grouped into practical states such as:

- **Draft** — still being prepared;
- **Ready** — locally ready for the next publishing action;
- **Scheduled** — planned for future publication;
- **Published** — confirmed as published;
- **Needs attention** — a failed, missing, divergent, or otherwise unresolved state needs review.

The exact remote situation is shown on the post rather than being hidden behind the column name.

## Common Board actions

From the Board you can:

- open or select posts;
- create a post from images;
- reconcile known posts with CivitAI;
- discover and adopt posts from the connected account;
- find missing local image files for adopted posts;
- archive published post records.

Archiving a post from the Board does not move files. File movement into the configured archive folder is a separate explicit Settings operation.

## Local and remote state are different things

A local plan can exist before any CivitAI post exists. Likewise, a remote post can change or disappear after Livebound last saw it.

Livebound therefore records confirmed remote facts separately from the creator's local intent. When the two differ, the UI asks you to resolve the difference instead of silently overwriting one side.

## Publishing runs

A publishing run can be in progress, completed, failed, or waiting for reconciliation after an uncertain remote result. Open Review or the post itself to see what happened and whether the run can be resumed safely.
