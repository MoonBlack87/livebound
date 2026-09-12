import type { IssueLevel, PostState } from '../../types/api'
import type { IconName } from './Icon.svelte'

/**
 * Every state is a colour *and* an icon. Colour alone would leave the board
 * unreadable for anyone who cannot separate the blue from the green.
 *
 * The label is a translation key, not a word: the board is one of the screens
 * people read fastest, and a stale German label there is worse than none.
 */
export const STATE_STYLE: Record<PostState, { color: string; icon: IconName; label: string }> = {
  draft: { color: 'var(--color-draft)', icon: 'draft', label: 'state.draft' },
  ready: { color: 'var(--color-ready)', icon: 'ready', label: 'state.ready' },
  pushing: { color: 'var(--color-accent-500)', icon: 'pushing', label: 'state.pushing' },
  needs_reconcile: { color: 'var(--color-attention)', icon: 'needs_reconcile', label: 'state.needs_reconcile' },
  remote_draft: { color: 'var(--color-draft)', icon: 'remote_draft', label: 'state.remote_draft' },
  scheduled: { color: 'var(--color-scheduled)', icon: 'schedule', label: 'state.scheduled' },
  published: { color: 'var(--color-published)', icon: 'published', label: 'state.published' },
  failed: { color: 'var(--color-failed)', icon: 'failed', label: 'state.failed' },
  remote_missing: { color: 'var(--color-attention)', icon: 'remote_missing', label: 'state.remote_missing' },
  archived: { color: 'var(--color-ink-500)', icon: 'archive', label: 'state.archived' },
}

export const LEVEL_STYLE: Record<IssueLevel, { color: string; icon: IconName }> = {
  error: { color: 'var(--color-failed)', icon: 'failed' },
  warning: { color: 'var(--color-ready)', icon: 'warning' },
  info: { color: 'var(--color-ink-400)', icon: 'info' },
}
