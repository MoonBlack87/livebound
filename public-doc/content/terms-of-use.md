---
title: "Terms of Use"
description: "Responsibilities and boundaries when using Livebound with CivitAI."
weight: 100
---
## Short version

Livebound is a tool for creator-directed preparation and publishing to your own CivitAI account. It can automate the mechanical parts of a publishing plan you configure, but it is not intended as a bot for spam, autonomous engagement, ranking/statistics manipulation, limit evasion, or access to accounts or content you are not authorised to control.

Using Livebound does not change CivitAI's rules. If CivitAI does not permit an action, performing it through Livebound does not make it permitted. You are responsible for the content you publish and the account actions you initiate.

## Software licence

Livebound itself is licensed under **AGPL-3.0-or-later**. The rights granted by that software licence are separate from the terms that apply when you access CivitAI or any other service. Nothing on this page narrows the rights the AGPL grants you to the Livebound source code.

## Using the CivitAI integration

When Livebound accesses CivitAI for you, your use remains subject to the current [CivitAI Terms of Service](https://civitai.red/content/tos), content rules, account rules, and platform limits.

In particular, CivitAI's current terms distinguish authorised automated access through interfaces it provides from prohibited automation used to manipulate the service or its statistics. Livebound is designed around user-directed publishing operations through authenticated CivitAI interfaces; it does not grant permission for conduct that CivitAI prohibits.

You are responsible for:

- using an account you are authorised to control;
- reviewing the images, text, tags, metadata and resource attribution you submit;
- respecting content restrictions and third-party rights;
- respecting CivitAI's rate limits and other platform limits;
- confirming destructive or irreversible actions only when you intend their consequences.

Do not use Livebound to spam, fabricate engagement, manipulate rankings or statistics, evade account or rate restrictions, bypass access controls, or operate on another person's account or content without permission.

## Scheduled publishing and retries

Scheduling a post or allowing Livebound to retry a publishing operation does not transfer responsibility for that post to Livebound. The creator still selects the content and publishing plan.

A remote service can change its API, limits, moderation, or other behaviour. Livebound cannot guarantee that a planned CivitAI operation will always remain available or be accepted by the platform.

## Your data and third-party services

Livebound runs locally, but CivitAI features send the data needed for the operation you request to CivitAI. Optional model integrations can send selected prompt/image context to the model backend you configure, including a remote Ollama server if you choose one.

Review the Security and Privacy documentation for the practical data-flow details.

## Independence

Livebound is an independent open-source community project and is not affiliated with, sponsored by, or endorsed by CivitAI.

CivitAI is responsible for its own service, policies and moderation. Livebound is responsible only for the software it provides; it cannot grant exceptions to CivitAI's rules or speak on CivitAI's behalf.

## Your files, and who answers for them

**Livebound moves and deletes files on your disk when you ask it to.** That is not a side effect; it is part of what the application is for. The duplicate trash moves files, restore moves them back, the archive run moves published files into your archive folder, and permanent deletion removes them for good. Every one of those is an explicit action you start, and the irreversible ones ask you to type a confirmation first.

The software has been tested to the best of our knowledge and belief, and no defect in those paths is known. That is a statement about care taken, not a guarantee.

{{< callout tone="warn" mark="⚠️" title="Responsibility for your files is yours" >}}
**By using Livebound you accept sole responsibility for your files, and no liability for data loss is accepted.**

Keep your own backups of your image folders and your archive, as you would for any other data you cannot replace. Livebound's database backup covers Livebound's own state — it does not contain your images.
{{< /callout >}}

This sits on top of the warranty and liability terms of the AGPL licence (sections 15 to 17), which apply in full and are not narrowed by anything on this page.
