# Changelog

Notable changes to Livebound are recorded here, newest first.

## 1.0.1 - 2026-10-13

- Schedule posts with CivitAI's 10-minute minimum lead time plus one minute of delivery reserve, instead of the previous 60-minute limit.
- Keep expired absolute schedules as drafts and prevent automatic retries of publish-time changes.

## 1.0.0 - 2026-09-12

- Local-first browser workspace for organising AI images from existing folders without rewriting original files.
- Generation-metadata support for common Stable Diffusion, ComfyUI, CivitAI and related creator workflows.
- Exact and perceptual duplicate detection with recoverable trash and restore tools.
- Metadata, prompt, model, LoRA and resource review before upload, including bulk editing and upload exclusions.
- CivitAI OAuth connection with draft creation, image upload, tags, scheduling and publishing from the creator's own account.
- Preflight checks, resumable publishing, and reconciliation between local post state and CivitAI.
- Board, calendar and review views for preparing, spreading and checking planned posts.
- Discovery and adoption of existing CivitAI posts, with local matching and optional image fetching.
- Explicit archive workflow for moving verified published images into dated post folders.
- Database backup and restore, first-start setup, diagnostics, and an optional minimal usage ping whose receiver source ships in the public repository.
- Optional local language-model or Ollama assistance for titles, descriptions, tags and prompt shortening.
- Windows, Linux and WSL starters, with release ZIP and Git-based installation paths.
