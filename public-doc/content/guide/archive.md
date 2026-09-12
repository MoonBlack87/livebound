---
title: "Archive"
description: "Keep published history in Livebound and optionally move published files into a structured archive."
weight: 80
---
Livebound has two related but separate archive actions: archiving the **post record** and moving the **published image files**.

{{< gallery label="Archiving a post, and then its files" >}}
{{< slide src="images/docs/archive/walkthrough/01.jpg" caption="Archiving a post takes it off the board. The line beside the button says what that does not do: no file is moved." >}}
{{< slide src="images/docs/archive/walkthrough/02.jpg" caption="Done — and it says so again, because the two halves are easy to confuse." >}}
{{< slide src="images/docs/archive/walkthrough/03.jpg" caption="The archive view: the post with its pictures, and no folder beside it yet. Nothing has moved." >}}
{{< slide src="images/docs/archive/walkthrough/04.jpg" caption="The archive folder on disk at that moment: empty. Moving the files is a second decision." >}}
{{< slide src="images/docs/archive/walkthrough/05.jpg" caption="When you take it, every path is listed first — moved, not copied, and nothing overwritten." >}}
{{< slide src="images/docs/archive/walkthrough/06.jpg" caption="The word has to be typed before the button will do anything. This is the one action here that cannot be taken back." >}}
{{< slide src="images/docs/archive/walkthrough/07.jpg" caption="Afterwards the same view names the folder, and the run reports what it moved and what it tidied up." >}}
{{< slide src="images/docs/archive/walkthrough/08.jpg" caption="On disk: one folder per post, dated and named by its CivitAI id." >}}
{{< slide src="images/docs/archive/walkthrough/09.jpg" caption="And inside it, each image beside the sidecar that belongs to it." >}}
{{< /gallery >}}

## Archive a post on the Board

Archiving a published post removes it from the active Board workflow and places it in the Archive view.

This action changes Livebound's post state only. **It does not move image files.**

The Archive view keeps published history available with date filters and links back to CivitAI.

{{< callout tone="warn" mark="⚠️" title="This moves files on your disk" >}}
Livebound really moves and deletes files here, at your instruction. It has been tested to the best of our knowledge and no defect in these paths is known — but **you carry sole responsibility for your files, and no liability for data loss is accepted**. Keep your own backups of your image folders and archive. See [Terms of Use]({{< relref "/terms-of-use.md" >}}).
{{< /callout >}}

## Move published files into the archive folder

The file move is an explicit action in Settings. Livebound first shows the move plan, then moves files only after you start the operation.

Only images from posts you have already moved to Livebound's archived post state are eligible, and the file move still requires exact publication identity rather than visual similarity alone.

The normal folder format is:

```text
YYYY-MM-DD__<remote_post_id>
```

If the publication date is not known, the post ID alone can be used until the folder can later be renamed to the dated form.

Where a remote image ID is known, Livebound can include it in the archived filename. Sidecar files move with their image where supported.

Files are **moved, not copied**. Name collisions do not overwrite an existing file; Livebound chooses a free name instead.

Archived folders remain part of Livebound's view of the library. Keeping all files associated with a published post is deliberate even when several local files correspond to the same visual image.

## Back up the archive itself

The database backup does not contain the image archive. Settings can create a ZIP of the archive folder separately, either as one file or split into 500 MB, 1 GB, or 4 GB parts.

The archive ZIP uses no additional compression because the image formats are already compressed.
