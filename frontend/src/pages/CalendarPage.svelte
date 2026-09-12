<script lang="ts">
  import { api } from '../api/client'
  import SpreadDialog from '../components/SpreadDialog.svelte'
  import StateBadge from '../components/ui/StateBadge.svelte'
  import { formatTime, relativeTo, weekdayLabels } from '../components/ui/format.svelte'
  import { t } from '../i18n/index.svelte'
  import { app, invalidate, openPost, toast } from '../state/store.svelte'
  import type { CalendarEntry, CalendarView } from '../types/api'

  let data = $state<CalendarView | null>(null)
  let weeks = $state(3)
  let spreadOpen = $state(false)
  /** Weeks away from the current one. Negative looks back. */
  let shift = $state(0)

  /**
   * The window anchor as a timestamp, not a Date.
   *
   * The effect below reads this and writes `data`, and `data.now` anchors the
   * week - so a Date here would be a fresh object on every response, the effect
   * would see a changed dependency, fetch again, and never stop. A number is
   * equal to the previous one, so the loop closes after the first load.
   */
  const windowStartMs = $derived.by(() => {
    const start = startOfWeek(data ? new Date(data.now) : new Date())
    start.setDate(start.getDate() + shift * 7)
    return start.getTime()
  })

  const days = $derived(
    Array.from({ length: weeks * 7 }, (_, index) => {
      const date = new Date(windowStartMs)
      date.setDate(date.getDate() + index)
      return date
    }),
  )

  $effect(() => {
    void app.revision
    void app.postRevision
    const from = new Date(windowStartMs)
    const to = new Date(windowStartMs)
    to.setDate(to.getDate() + weeks * 7)
    api
      .calendar(from.toISOString(), to.toISOString())
      .then((value) => (data = value))
      .catch((error) => toast('error', (error as Error).message))
  })

  const byDay = $derived.by(() => {
    const map = new Map<string, CalendarEntry[]>()
    for (const entry of data?.entries ?? []) {
      const list = map.get(entry.day) ?? []
      list.push(entry)
      map.set(entry.day, list)
    }
    return map
  })

  async function drop(entry: CalendarEntry, day: Date, view: CalendarView) {
    if (entry.locked) {
      toast('error', t('calendar.lockedPublished'))
      return
    }
    // Keep the time of day, move the date - the usual intent when dragging.
    const original = new Date(entry.publish_at)
    const target = new Date(day)
    target.setHours(original.getHours(), original.getMinutes(), 0, 0)
    if (target < new Date(view.earliest)) {
      toast('error', t('calendar.tooEarly', { time: formatTime(view.earliest) }))
      return
    }
    try {
      await api.reschedule(entry.post_id, { scheduled_at: target.toISOString() })
      invalidate()
      toast('success', t('calendar.moved'))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  function onDrop(event: DragEvent, day: Date, view: CalendarView) {
    const id = Number(event.dataTransfer?.getData('text/entry-id'))
    const entry = view.entries.find((item) => item.post_id === id)
    if (entry) drop(entry, day, view)
  }

  /**
   * The eight that are actually coming.
   *
   * This used to slice the first eight off a list sorted ascending over all
   * time - which is the eight *oldest*, in practice a column of published posts.
   */
  function upNext(view: CalendarView): CalendarEntry[] {
    const now = Date.now()
    return view.entries
      .filter(
        (entry) =>
          entry.state !== 'published' && new Date(entry.publish_at).getTime() >= now,
      )
      .slice(0, 8)
  }

  function stateColor(state: string): string {
    const map: Record<string, string> = {
      draft: 'var(--color-draft)',
      ready: 'var(--color-ready)',
      scheduled: 'var(--color-scheduled)',
      remote_draft: 'var(--color-draft)',
      published: 'var(--color-published)',
      failed: 'var(--color-failed)',
      needs_reconcile: 'var(--color-attention)',
      remote_missing: 'var(--color-attention)',
    }
    return map[state] ?? 'var(--color-ink-600)'
  }

  function startOfWeek(date: Date): Date {
    const result = new Date(date)
    const day = (result.getDay() + 6) % 7 // Monday = 0
    result.setDate(result.getDate() - day)
    result.setHours(0, 0, 0, 0)
    return result
  }

  function toKey(date: Date): string {
    const pad = (value: number) => String(value).padStart(2, '0')
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  }
</script>

{#if !data}
  <div class="p-5"><div class="skeleton h-96 w-full"></div></div>
{:else}
  {@const view = data}
  {@const now = new Date(view.now)}
  <div class="flex h-full flex-col">
    <header class="flex flex-wrap items-center gap-2 border-b border-ink-800 px-5 py-3">
      <h1 class="text-sm font-semibold">{t('nav.calendar')}</h1>
      <span class="mono text-xs text-ink-400">
        {t('calendar.planned', { count: view.entries.length })}
      </span>
      <span class="chip text-[11px]">
        {view.daily_limit === null
          ? t('calendar.createdToday', { used: view.used_today })
          : t('calendar.today', { used: view.used_today, limit: view.daily_limit })}
      </span>
      <div class="flex-1"></div>
      <div class="flex items-center gap-1.5">
        <span
          class="blocked-hatch inline-block h-3 w-6 rounded border border-ink-700"
          aria-hidden="true"
        ></span>
        <span class="text-[11px] text-ink-400">
          {t('calendar.blockedLegend', { minutes: view.min_lead_minutes })}
        </span>
      </div>
      <div class="flex items-center gap-1">
        <button class="btn btn-sm" onclick={() => (shift -= 1)} aria-label={t('calendar.earlier')}>
          ◀
        </button>
        <button class="btn btn-sm" disabled={shift === 0} onclick={() => (shift = 0)}>
          {t('calendar.today.button')}
        </button>
        <button class="btn btn-sm" onclick={() => (shift += 1)} aria-label={t('calendar.later')}>
          ▶
        </button>
      </div>
      <select
        class="input w-auto"
        value={weeks}
        onchange={(event) => (weeks = Number((event.currentTarget as HTMLSelectElement).value))}
      >
        <option value={2}>{t('calendar.weeks', { count: 2 })}</option>
        <option value={3}>{t('calendar.weeks', { count: 3 })}</option>
        <option value={6}>{t('calendar.weeks', { count: 6 })}</option>
      </select>
      <button class="btn btn-sm" onclick={() => (spreadOpen = true)}>{t('calendar.spread')}</button>
    </header>

    <div class="min-h-0 flex-1 overflow-y-auto p-5">
      {#if !view.entries.length}
        <div class="mb-4 rounded-lg border border-dashed border-ink-700 px-4 py-6 text-center">
          <p class="text-xs text-ink-300">
            {shift === 0 ? t('calendar.empty.title') : t('calendar.windowEmpty')}
          </p>
          <p class="mt-1 text-[11px] text-ink-500">{t('calendar.empty.hint')}</p>
        </div>
      {/if}
        <div
          class="grid grid-cols-7 gap-px overflow-hidden rounded-xl border border-ink-750 bg-ink-750"
        >
          {#each weekdayLabels() as label (label)}
            <div
              class="bg-ink-900 px-2 py-1.5 text-center text-[11px] font-semibold text-ink-400"
            >
              {label}
            </div>
          {/each}
          {#each days as day (toKey(day))}
            {@const key = toKey(day)}
            {@const entries = (byDay.get(key) ?? [])
              .slice()
              .sort((a, b) => a.publish_at.localeCompare(b.publish_at))}
            {@const isToday = toKey(now) === key}
            {@const isPast = day < new Date(now.getFullYear(), now.getMonth(), now.getDate())}
            {@const over = view.daily_limit !== null && entries.length > view.daily_limit}
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div
              ondragover={(event) => event.preventDefault()}
              ondrop={(event) => onDrop(event, day, view)}
              class="min-h-28 bg-ink-900 p-1.5 {isPast ? 'opacity-45' : ''}"
            >
              {#if isToday}
                <div
                  class="blocked-hatch mb-1 rounded border border-ink-750 px-1.5 py-1"
                  title={t('calendar.leadHint', { minutes: view.min_lead_minutes })}
                >
                  <p class="mono text-[9.5px] leading-tight text-ink-300">
                    {t('calendar.blockedUntil', { time: formatTime(view.earliest) })}
                  </p>
                </div>
              {/if}
              <div class="mb-1 flex items-baseline justify-between">
                <span class="text-[11px] {isToday ? 'font-bold text-accent-300' : 'text-ink-400'}">
                  {day.getDate()}.{day.getMonth() + 1}.
                </span>
                {#if entries.length}
                  <span
                    class="mono text-[10px]"
                    style="color: {over ? 'var(--color-failed)' : 'var(--color-ink-500)'};"
                    title={over ? t('calendar.overLimit') : ''}
                  >
                    {entries.length}
                  </span>
                {/if}
              </div>
              <div class="space-y-1">
                {#each entries as entry (entry.post_id)}
                  {@const past =
                    entry.state !== 'published' &&
                    new Date(entry.publish_at).getTime() < now.getTime()}
                  <button
                    draggable={!entry.locked}
                    ondragstart={(event) =>
                      event.dataTransfer?.setData('text/entry-id', String(entry.post_id))}
                    onclick={() => openPost(entry.post_id)}
                    class="block w-full truncate rounded px-1.5 py-1 text-left text-[10.5px] transition-colors hover:brightness-125"
                    style="background: {past
                      ? 'color-mix(in srgb, var(--color-failed) 22%, transparent)'
                      : `color-mix(in srgb, ${stateColor(entry.state)} 18%, transparent)`};
                           color: var(--color-ink-100);
                           cursor: {entry.locked ? 'default' : 'grab'};"
                    title="{entry.title || t('post.untitled')} · {formatTime(
                      entry.publish_at,
                    )}{entry.is_relative ? ` (${entry.offset_label})` : ''}{past
                      ? ` · ${t('post.overdueHint')}`
                      : ''}"
                  >
                    {#if past}<span aria-hidden="true">⚠ </span>{/if}
                    <span class="mono opacity-70">{formatTime(entry.publish_at)}</span>
                    {#if entry.is_relative}<span title={t('calendar.relativeHint')}>~</span>{/if}
                    {#if entry.pinned}<span title={t('calendar.pinnedHint')} aria-hidden="true">📌</span>{/if}
                    {entry.title || t('post.untitled')}
                  </button>
                {/each}
              </div>
            </div>
          {/each}
        </div>

        <div class="mt-4 rounded-lg border border-ink-750 bg-ink-900 p-3">
          <p class="label mb-2">{t('calendar.upNext')}</p>
          <div class="space-y-1.5">
            {#if !upNext(view).length}
              <p class="py-2 text-center text-[11px] text-ink-500">{t('calendar.upNextEmpty')}</p>
            {/if}
            {#each upNext(view) as entry (entry.post_id)}
              <button
                onclick={() => openPost(entry.post_id)}
                class="flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-ink-850"
              >
                <StateBadge state={entry.state} />
                <span class="min-w-0 flex-1 truncate text-xs text-ink-200">
                  {entry.title || t('post.untitled')}
                </span>
                {#if entry.is_relative && entry.offset_label}
                  <span class="chip text-[10px]" title={t('calendar.offsetHint')}>
                    {entry.offset_label}
                  </span>
                {/if}
                {#if entry.pinned}
                  <span class="chip text-[10px]" title={t('schedule.pinnedLabel')}>
                    {t('calendar.onCivitai')}
                  </span>
                {/if}
                <span
                  class="mono shrink-0 text-[10.5px]"
                  style="color: {new Date(entry.publish_at).getTime() < Date.now() &&
                  entry.state !== 'published'
                    ? 'var(--color-failed)'
                    : 'var(--color-ink-400)'};"
                >
                  {relativeTo(entry.publish_at)}
                </span>
              </button>
            {/each}
          </div>
        </div>
    </div>

    {#if spreadOpen}
      <SpreadDialog onClose={() => (spreadOpen = false)} />
    {/if}
  </div>
{/if}
