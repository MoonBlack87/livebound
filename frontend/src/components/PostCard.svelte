<script lang="ts">
  import { api } from '../api/client'
  import { plural, t } from '../i18n/index.svelte'
  import StateBadge from './ui/StateBadge.svelte'
  import { formatDateTime, relativeTo } from './ui/format.svelte'
  import type { Post } from '../types/api'

  let {
    post,
    onOpen,
    onArchive,
    selected = false,
    onToggle,
  }: {
    post: Post
    onOpen: () => void
    onArchive?: () => void
    selected?: boolean
    onToggle?: () => void
  } = $props()

  const thumbs = $derived(post.thumbnails ?? [])

  // A fixed time that has already passed is the failure mode relative planning
  // exists to prevent - CivitAI would publish such a post immediately.
  const overdue = $derived(
    post.state !== 'published' &&
      post.schedule_mode === 'absolute' &&
      post.resolved_publish_at != null &&
      new Date(post.resolved_publish_at).getTime() < Date.now(),
  )
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<article
  draggable="true"
  ondragstart={(event) => event.dataTransfer?.setData('text/post-id', String(post.id))}
  onclick={(event) => {
    // Ctrl/Cmd-click marks instead of opening - the same modifier the library
    // uses, so one idiom covers both boards.
    if (onToggle && (event.ctrlKey || event.metaKey)) {
      event.preventDefault()
      onToggle()
      return
    }
    onOpen()
  }}
  class="relative cursor-pointer rounded-lg border bg-ink-850 p-2.5 transition-colors {selected
    ? 'border-accent-500'
    : 'border-ink-750 hover:border-ink-600'}"
>
  {#if onToggle}
    <input
      type="checkbox"
      class="absolute right-2 top-2 z-10 h-3.5 w-3.5 cursor-pointer accent-[var(--color-accent-500)]"
      checked={selected}
      aria-label={t('board.select', { title: post.title || t('post.untitled') })}
      onclick={(event) => {
        event.stopPropagation()
        onToggle()
      }}
    />
  {/if}
  {#if thumbs.length}
    <div class="mb-2 flex gap-1 overflow-hidden rounded-md">
      {#each thumbs.slice(0, 4) as thumbnail (thumbnail.image_id)}
        <img
          src={api.thumbnailUrl(thumbnail.image_id, false, thumbnail.thumbnail_path)}
          alt=""
          loading="lazy"
          class="h-14 flex-1 bg-ink-800 object-cover"
        />
      {/each}
    </div>
  {/if}
  <p class="truncate text-xs font-medium text-ink-100">
    {#if post.title}{post.title}{:else}<span class="text-ink-500">{t('post.untitled')}</span>{/if}
  </p>

  <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
    <StateBadge state={post.state} />
    {#if post.dirty && post.has_remote}
      <span class="chip text-[10px]" title={t('post.unsentHint')}>● {t('post.unsent')}</span>
    {/if}
    {#if post.remote_diverged}
      <span
        class="chip text-[10px]"
        style="color: var(--color-attention);"
        title={t('post.divergedHint')}
      >
        ⇄ {t('post.diverged')}
      </span>
    {/if}
  </div>

  <div class="mt-1.5 space-y-0.5 text-[10.5px] text-ink-400">
    {#if post.resolved_publish_at}
      <p class="mono" style={overdue ? 'color: var(--color-failed);' : ''}>
        {#if overdue}<span title={t('post.overdueHint')}>⚠ </span>{/if}
        {formatDateTime(post.resolved_publish_at)}
        {#if post.state !== 'published'}
          <span class={overdue ? '' : 'text-ink-500'}>
            · {relativeTo(post.resolved_publish_at)}
          </span>
        {/if}
      </p>
    {/if}
    {#if overdue}
      <p style="color: var(--color-failed);">{t('post.overdue')}</p>
    {/if}
    <p>
      {plural('post.images', post.image_count ?? 0)}{post.model_name
        ? ` · ${post.model_name}`
        : ''}{post.tags.length ? ` · ${plural('post.tags', post.tags.length)}` : ''}
    </p>
    {#if post.last_error}
      <p class="truncate" style="color: var(--color-failed);" title={post.last_error}>
        {post.last_error}
      </p>
    {/if}
  </div>

  {#if onArchive}
    <div class="mt-2 flex justify-end">
      <button
        class="btn btn-ghost btn-sm"
        title={t('board.archiveHint')}
        onclick={(event) => {
          event.stopPropagation()
          onArchive()
        }}
      >
        {t('board.archive')}
      </button>
    </div>
  {/if}
</article>
