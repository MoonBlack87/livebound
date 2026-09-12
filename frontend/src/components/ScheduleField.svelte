<script lang="ts">
  import { untrack } from 'svelte'
  import { api } from '../api/client'
  import { t } from '../i18n/index.svelte'
  import ConfirmDialog from './ui/ConfirmDialog.svelte'
  import { formatDateTime } from './ui/format.svelte'
  import type { OffsetParse, Post } from '../types/api'

  /**
   * Choosing when a post goes live.
   *
   * The relative mode is the default, and not for convenience: a post can sit in
   * the local queue for days, and an absolute time picked while drafting may be
   * in the past by the time it is actually pushed. CivitAI does not reject a
   * past or too-near publishedAt - it publishes immediately, and there is no
   * unpublish. A relative plan is resolved against the clock at push time, so it
   * cannot rot.
   */

  let {
    post,
    minLeadMinutes,
    onChange,
    disabled = false,
  }: {
    post: Post
    minLeadMinutes: number
    onChange: (patch: Record<string, unknown>) => void
    disabled?: boolean
  } = $props()

  // Seeded once, deliberately: this is a text field the user is editing, and
  // a later refresh of `post` must not overwrite what they are typing.
  let text = $state(
    untrack(() =>
      post.schedule_offset_minutes != null ? String(post.schedule_offset_minutes) : '',
    ),
  )
  let parsed = $state<OffsetParse | null>(null)
  let confirmingNow = $state(false)

  const mode = $derived(post.publish_mode)
  const isRelative = $derived(post.schedule_mode === 'relative')

  const MODES = [
    { value: 'schedule', label: 'schedule.mode.schedule' },
    { value: 'now', label: 'schedule.mode.now' },
    { value: 'draft_only', label: 'schedule.mode.draft' },
  ] as const

  $effect(() => {
    const query = text
    if (post.schedule_mode !== 'relative' || !query.trim()) {
      parsed = null
      return
    }
    let cancelled = false
    const timer = setTimeout(() => {
      api
        .parseOffset(query)
        .then((result) => !cancelled && (parsed = result))
        .catch(() => undefined)
    }, 220)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  })

  function pickMode(value: (typeof MODES)[number]['value']) {
    // Publishing immediately is the only switch here that cannot be taken back -
    // CivitAI has no unpublish, only delete. So: not one click.
    if (value === 'now' && mode !== 'now') confirmingNow = true
    else onChange({ publish_mode: value })
  }

  function toLocalInput(iso: string | null): string {
    if (!iso) return ''
    const date = new Date(iso)
    if (Number.isNaN(date.getTime())) return ''
    const pad = (value: number) => String(value).padStart(2, '0')
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
      date.getHours(),
    )}:${pad(date.getMinutes())}`
  }
</script>

<div class="space-y-3">
  <div>
    <span class="label">{t('schedule.label')}</span>
    <div class="flex gap-1 rounded-lg border border-ink-700 bg-ink-950 p-1">
      {#each MODES as option (option.value)}
        <button
          type="button"
          {disabled}
          onclick={() => pickMode(option.value)}
          class="flex-1 rounded-md px-2 py-1.5 text-xs transition-colors {mode === option.value
            ? 'bg-ink-750 font-medium text-ink-100'
            : 'text-ink-400 hover:text-ink-200'}"
        >
          {t(option.label)}
        </button>
      {/each}
    </div>
  </div>

  {#if mode === 'now'}
    <div
      class="rounded-lg border px-3 py-2.5"
      style="border-color: color-mix(in srgb, var(--color-failed) 55%, transparent);
             background: color-mix(in srgb, var(--color-failed) 12%, transparent);"
    >
      <p class="flex items-center gap-1.5 text-xs font-semibold text-ink-100">
        <span aria-hidden="true">⚠</span>
        {t('schedule.now.warn')}
      </p>
      <p class="mt-1 text-[11px] leading-relaxed text-ink-300">{t('schedule.now.explain')}</p>
      <button class="btn btn-sm mt-2" onclick={() => onChange({ publish_mode: 'schedule' })}>
        {t('schedule.now.backOut')}
      </button>
    </div>
  {/if}

  {#if confirmingNow}
    <ConfirmDialog
      title={t('schedule.now.confirmTitle')}
      danger
      requireKey="ui.confirm.word.publish"
      confirmLabel={t('schedule.now.confirmAction')}
      onCancel={() => (confirmingNow = false)}
      onConfirm={() => {
        confirmingNow = false
        onChange({ publish_mode: 'now' })
      }}
    >
      {#snippet body()}
        <p>{t('schedule.now.confirmBody1')}</p>
        <p class="text-ink-400">{t('schedule.now.confirmBody2')}</p>
        <p class="text-ink-400">{t('schedule.now.confirmBody3')}</p>
      {/snippet}
    </ConfirmDialog>
  {/if}

  {#if mode === 'draft_only'}
    <p class="rounded-lg bg-ink-850 px-3 py-2 text-xs leading-relaxed text-ink-400">
      {t('schedule.draftOnly.explain')}
    </p>
  {/if}

  {#if mode === 'schedule'}
    <div class="flex gap-1 rounded-lg border border-ink-700 bg-ink-950 p-1">
      <button
        type="button"
        {disabled}
        onclick={() => onChange({ schedule_mode: 'relative' })}
        class="flex-1 rounded-md px-2 py-1.5 text-xs transition-colors {isRelative
          ? 'bg-ink-750 font-medium text-ink-100'
          : 'text-ink-400 hover:text-ink-200'}"
      >
        {t('schedule.relative')}
      </button>
      <button
        type="button"
        {disabled}
        onclick={() => onChange({ schedule_mode: 'absolute' })}
        class="flex-1 rounded-md px-2 py-1.5 text-xs transition-colors {!isRelative
          ? 'bg-ink-750 font-medium text-ink-100'
          : 'text-ink-400 hover:text-ink-200'}"
      >
        {t('schedule.absolute')}
      </button>
    </div>

    {#if isRelative}
      <div>
        <input
          class="input"
          placeholder={t('schedule.offsetPlaceholder')}
          bind:value={text}
          {disabled}
          onblur={() => {
            if (parsed?.ok && parsed.minutes != null) {
              onChange({ schedule_offset_minutes: parsed.minutes })
            } else if (!text.trim()) {
              onChange({ schedule_offset_minutes: null })
            }
          }}
        />
        <div class="mt-1.5 min-h-[2.2rem] text-xs">
          {#if parsed?.ok}
            <p class="text-ink-200">
              {parsed.label} → <span class="mono">{formatDateTime(parsed.publish_at)}</span>
            </p>
            {#if parsed.raised}
              <p style="color: var(--color-ready);">
                {t('schedule.raised', { minutes: minLeadMinutes })}
              </p>
            {/if}
          {:else if text.trim()}
            <p style="color: var(--color-failed);">{t('schedule.unreadable')}</p>
          {:else}
            <p class="text-ink-500">{t('schedule.relativeHint')}</p>
          {/if}
        </div>
      </div>
    {:else}
      <div>
        <input
          type="datetime-local"
          class="input"
          {disabled}
          value={toLocalInput(post.scheduled_at)}
          onchange={(event) => {
            const value = (event.currentTarget as HTMLInputElement).value
            onChange({ scheduled_at: value ? new Date(value).toISOString() : null })
          }}
        />
        <p class="mt-1.5 text-xs text-ink-500">{t('schedule.absoluteHint', { minutes: minLeadMinutes })}</p>
      </div>
    {/if}

    {#if post.resolved_publish_at}
      <div class="rounded-lg border border-ink-750 bg-ink-850 px-3 py-2">
        <p class="text-[10px] uppercase tracking-wide text-ink-400">
          {post.publish_at_is_pinned ? t('schedule.pinnedLabel') : t('schedule.plannedLabel')}
        </p>
        <p class="mono text-xs text-ink-100">{formatDateTime(post.resolved_publish_at)}</p>
        {#if post.publish_at_is_pinned}
          <p class="mt-1 text-[11px] leading-relaxed text-ink-500">{t('schedule.pinnedHint')}</p>
        {/if}
      </div>
    {/if}
  {/if}
</div>
