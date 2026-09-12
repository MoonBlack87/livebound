---
title: "Supported Formats"
description: "Image file types and generation-metadata families Livebound can currently read."
weight: 10
---
## Image files

Livebound's local library recognises:

- PNG
- JPEG / JPG
- WebP

Readable images remain usable even when their embedded generation metadata is missing or unsupported.

## Generation metadata

The current parser set includes metadata produced by or associated with:

| Source | Notes |
| --- | --- |
| Automatic1111 / Forge-style | Stable Diffusion parameter text and related resource information |
| ComfyUI | Workflow/prompt metadata used by common ComfyUI generation paths |
| CivitAI-generated images | CivitAI generation metadata where present in the image |
| SwarmUI | SwarmUI parameter metadata and resources |
| Fooocus | Fooocus metadata |
| FooocusPlus | FooocusPlus metadata |
| RuinedFooocus | RuinedFooocus metadata |
| NovelAI | NovelAI generation metadata |
| InvokeAI | InvokeAI metadata |
| TensorArt | TensorArt-exported generation metadata |
| Hugging Face Space-style exports | Supported metadata shapes found in compatible Space outputs |

Support means Livebound can extract useful generation fields from known examples of that format. Individual tools and workflows evolve, and custom nodes or exporters can write different structures.

If a file is readable as an image but its generation metadata is not supported, Livebound can still keep the image in the Library.

## Sidecars

Some workflows use text sidecars alongside an image. Where Livebound recognises a supported sidecar form, the sidecar can participate in metadata reading and can travel with file-management actions such as trash or archive moves.
