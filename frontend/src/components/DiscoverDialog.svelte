<script lang="ts">
  import { api, waitForJob } from '../api/client'
  import { plural, t } from '../i18n/index.svelte'
  import { app, invalidate, openPost, setActiveJob, setSettings, toast } from '../state/store.svelte'
  import type {
    AdoptBatchOutcome,
    DiscoveryItem,
    DiscoveryState,
    DiscoverySummary,
    Job,
  } from '../types/api'
  import Modal from './ui/Modal.svelte'
  import Spinner from './ui/Spinner.svelte'
  import { formatDateTime, formatDay } from './ui/format.svelte'

  /**
   * Newest-first, bounded review of the account's posts that are missing here.
   * The post.getInfinite discovery query omits drafts, so the manual post-ID
   * route stays; the post.get detail query can retrieve an owner draft by its id.
   */

  type StateFilter = 'all' | 'published' | 'scheduled'
  type SortOrder = 'newest' | 'oldest' | 'title' | 'images'
  type Tab = 'browse' | 'ids'

  const COUNTS = [30, 50, 100, 200]
  const PERIODS = [0, 7, 30, 90, 365]
  /** Mirrors MAX_POSTS_PER_BATCH; the server refuses more in one request. */
  const MAX_IDS = 200

  /** Explicit, so a new outcome cannot silently render as a missing key. */
  const OUTCOME_KEYS: Record<AdoptBatchOutcome['outcome'], string> = {
    created: 'discover.outcomeCreated',
    skipped: 'discover.outcomeSkipped',
    not_yours: 'discover.outcomeNotYours',
    not_found: 'discover.outcomeNotFound',
    failed: 'discover.outcomeFailed',
  }

  let { onClose }: { onClose: () => void } = $props()

  let items = $state<DiscoveryItem[] | null>(null)
  let summary = $state<DiscoverySummary>(emptySummary())
  let reason = $state('')
  let since = $state('')
  let nextCursor = $state<string | number | null>(null)
  let draftsUrl = $state('')
  let manualId = $state('')
  let tab = $state<Tab>('browse')
  let idText = $state('')
  let busy = $state<number | null>(null)
  let loadingMore = $state(false)
  let batchJob = $state<Job | null>(null)
  let selected = $state<number[]>([])
  let search = $state('')
  let stateFilter = $state<StateFilter>('all')
  let sortOrder = $state<SortOrder>('newest')
  let limit = $state(app.settings?.ui_discover_limit ?? 100)
  let days = $state(app.settings?.ui_discover_days ?? 0)
  let requestNumber = 0

  const selectedSet = $derived(new Set(selected))
  const batchRunning = $derived(
    batchJob?.status === 'starting' || batchJob?.status === 'running',
  )
  const batchOutcomes = $derived(readOutcomes(batchJob))
  /**
   * Every number in the pasted text, in order and without repeats.
   *
   * Pasting whole post URLs is the normal case - the id is what one copies out
   * of the address bar - so anything that is not a digit simply separates.
   */
  const pastedIds = $derived.by(() => {
    const seen = new Set<number>()
    for (const match of idText.matchAll(/\d+/g)) {
      const value = Number(match[0])
      if (value > 0) seen.add(value)
    }
    return [...seen]
  })
  const visibleItems = $derived.by(() => {
    const needle = search.trim().toLowerCase()
    const filtered = (items ?? []).filter((item) => {
      const stateMatches = stateFilter === 'all' || item.state === stateFilter
      const searchMatches =
        !needle ||
        item.title?.toLowerCase().includes(needle) ||
        String(item.remote_post_id).includes(needle)
      return stateMatches && searchMatches
    })
    return [...filtered].sort(compareItems)
  })

  $effect(() => {
    void load(limit, days)
  })

  function emptySummary(): DiscoverySummary {
    return { examined: 0, outside_period: 0, already_known: 0, adoptable: 0 }
  }

  function readOutcomes(job: Job | null): AdoptBatchOutcome[] {
    const outcomes = job?.result.outcomes
    return Array.isArray(outcomes) ? (outcomes as AdoptBatchOutcome[]) : []
  }

  function compareItems(left: DiscoveryItem, right: DiscoveryItem): number {
    if (sortOrder === 'title') {
      const a = left.title?.trim().toLowerCase() || String(left.remote_post_id)
      const b = right.title?.trim().toLowerCase() || String(right.remote_post_id)
      if (a !== b) return a < b ? -1 : 1
      return left.remote_post_id - right.remote_post_id
    }
    if (sortOrder === 'images') {
      return right.image_count - left.image_count || right.remote_post_id - left.remote_post_id
    }

    const a = timestamp(left.published_at)
    const b = timestamp(right.published_at)
    if (a === null && b !== null) return 1
    if (a !== null && b === null) return -1
    if (a !== null && b !== null && a !== b) return sortOrder === 'oldest' ? a - b : b - a
    return sortOrder === 'oldest'
      ? left.remote_post_id - right.remote_post_id
      : right.remote_post_id - left.remote_post_id
  }

  function timestamp(value: string | null): number | null {
    if (!value) return null
    const parsed = Date.parse(value)
    return Number.isNaN(parsed) ? null : parsed
  }

  function stateLabel(state: DiscoveryState): string {
    return t(`state.${state}`)
  }

  function outcomeLabel(outcome: AdoptBatchOutcome['outcome']): string {
    return t(OUTCOME_KEYS[outcome] ?? 'discover.outcomeFailed')
  }

  /** What the maintainer decides on afterwards: fetch the images, or leave them. */
  function localLabel(outcome: AdoptBatchOutcome): string {
    if (outcome.outcome !== 'created' || !outcome.images) return ''
    if (outcome.images_local === outcome.images) {
      return t('discover.imagesAllLocal', { count: outcome.images })
    }
    if (!outcome.images_local) return t('discover.imagesNoneLocal', { count: outcome.images })
    return t('discover.imagesSomeLocal', {
      found: outcome.images_local,
      count: outcome.images,
    })
  }

  async function adoptPasted() {
    if (!pastedIds.length || pastedIds.length > MAX_IDS || batchRunning) return
    await runBatch(pastedIds)
  }

  async function load(selectedLimit: number, selectedDays: number) {
    const ownRequest = ++requestNumber
    items = null
    summary = emptySummary()
    selected = []
    batchJob = null
    reason = ''
    since = ''
    nextCursor = null
    try {
      const data = await api.discover(selectedLimit, selectedDays || undefined)
      if (ownRequest !== requestNumber) return
      items = data.items
      summary = data.summary
      reason = data.reason
      since = data.since ?? ''
      nextCursor = data.next_cursor
      draftsUrl = data.drafts_url ?? ''
    } catch (error) {
      if (ownRequest !== requestNumber) return
      items = []
      reason = (error as Error).message
    }
  }

  async function loadMore() {
    const cursor = nextCursor
    if (cursor === null || loadingMore) return
    const ownRequest = requestNumber
    loadingMore = true
    reason = ''
    try {
      const data = await api.discover(limit, days || undefined, cursor)
      if (ownRequest !== requestNumber) return
      if (data.reason) {
        reason = data.reason
        return
      }
      const knownIds = new Set((items ?? []).map((item) => item.remote_post_id))
      items = [...(items ?? []), ...data.items.filter((item) => !knownIds.has(item.remote_post_id))]
      summary = {
        examined: summary.examined + data.summary.examined,
        outside_period: summary.outside_period + data.summary.outside_period,
        already_known: summary.already_known + data.summary.already_known,
        adoptable: summary.adoptable + data.summary.adoptable,
      }
      nextCursor = data.next_cursor
    } catch (error) {
      if (ownRequest === requestNumber) reason = (error as Error).message
    } finally {
      if (ownRequest === requestNumber) loadingMore = false
    }
  }

  async function rememberFilters() {
    try {
      setSettings(await api.saveSettings({ ui_discover_limit: limit, ui_discover_days: days }))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function adopt(remoteId: number) {
    busy = remoteId
    try {
      const result = await api.importRemote(remoteId)
      invalidate()
      toast('success', t('discover.adopted'))
      if ((items ?? []).some((item) => item.remote_post_id === remoteId)) {
        items = (items ?? []).filter((item) => item.remote_post_id !== remoteId)
        selected = selected.filter((id) => id !== remoteId)
        summary = {
          ...summary,
          already_known: summary.already_known + 1,
          adoptable: Math.max(summary.adoptable - 1, 0),
        }
      }
      manualId = ''
      openPost(result.post_id)
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = null
    }
  }

  function toggleSelection(remoteId: number) {
    selected = selectedSet.has(remoteId)
      ? selected.filter((id) => id !== remoteId)
      : [...selected, remoteId]
  }

  function selectFiltered() {
    selected = [
      ...new Set([...selected, ...visibleItems.map((item: DiscoveryItem) => item.remote_post_id)]),
    ]
  }

  /**
   * Adopt a list of ids and report each one.
   *
   * One entry point for both sources - the selection in the browse tab and a
   * pasted list - so the outcome handling cannot drift apart between them.
   */
  async function runBatch(ids: number[]) {
    try {
      const started = await api.importRemoteBatch(ids)
      batchJob = started
      setActiveJob(started)
      const finished = await waitForJob(
        started.id,
        (next) => {
          batchJob = next
          setActiveJob(next)
        },
        500,
      )
      batchJob = finished
      const outcomes = readOutcomes(finished)
      const adoptedIds = new Set(
        outcomes
          .filter((outcome) => outcome.outcome === 'created' || outcome.outcome === 'skipped')
          .map((outcome) => outcome.remote_post_id),
      )
      items = (items ?? []).filter((item) => !adoptedIds.has(item.remote_post_id))
      selected = selected.filter((id) => !adoptedIds.has(id))
      summary = {
        ...summary,
        already_known: summary.already_known + adoptedIds.size,
        adoptable: Math.max(summary.adoptable - adoptedIds.size, 0),
      }
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function adoptSelected() {
    if (!selected.length || batchRunning) return
    await runBatch(selected)
  }
</script>

<Modal title={t('discover.title')} wide {onClose}>
  <!-- Two ways in: review what the account has, or work from ids you already
       know. The discovery query omits drafts, while the post.get detail query can
       retrieve one by a known id. -->
  <div class="mb-3 flex gap-1 border-b border-ink-750">
    {#each [['browse', 'discover.tabBrowse'], ['ids', 'discover.tabIds']] as [value, label] (value)}
      <button
        class="border-b-2 px-3 py-1.5 text-xs"
        style="border-color: {tab === value ? 'var(--color-accent-400)' : 'transparent'};
               color: {tab === value ? 'var(--color-ink-100)' : 'var(--color-ink-400)'};"
        disabled={batchRunning}
        onclick={() => (tab = value as Tab)}
      >
        {t(label)}
      </button>
    {/each}
  </div>

  {#if tab === 'browse'}
    <div class="mb-3 grid grid-cols-2 gap-2 lg:grid-cols-5">
      <label class="space-y-1 text-[11px] text-ink-400">
        <span>{t('discover.count')}</span>
        <select
          class="input"
          value={limit}
          disabled={batchRunning}
          onchange={(event) => {
            limit = Number((event.currentTarget as HTMLSelectElement).value)
            void rememberFilters()
          }}
        >
          {#each COUNTS as count (count)}
            <option value={count}>{count}</option>
          {/each}
        </select>
      </label>
      <label class="space-y-1 text-[11px] text-ink-400">
        <span>{t('discover.period')}</span>
        <select
          class="input"
          value={days}
          disabled={batchRunning}
          onchange={(event) => {
            days = Number((event.currentTarget as HTMLSelectElement).value)
            void rememberFilters()
          }}
        >
          {#each PERIODS as period (period)}
            <option value={period}>
              {period ? t('discover.lastDays', { count: period }) : t('discover.allDates')}
            </option>
          {/each}
        </select>
      </label>
      <label class="col-span-2 space-y-1 text-[11px] text-ink-400 lg:col-span-1">
        <span>{t('discover.search')}</span>
        <input class="input" bind:value={search} placeholder={t('discover.searchPlaceholder')} />
      </label>
      <label class="space-y-1 text-[11px] text-ink-400">
        <span>{t('discover.filter')}</span>
        <select class="input" bind:value={stateFilter}>
          <option value="all">{t('discover.filterAll')}</option>
          <option value="published">{t('discover.filterPublished')}</option>
          <option value="scheduled">{t('discover.filterScheduled')}</option>
        </select>
      </label>
      <label class="space-y-1 text-[11px] text-ink-400">
        <span>{t('discover.sort')}</span>
        <select class="input" bind:value={sortOrder}>
          <option value="newest">{t('discover.sortNewest')}</option>
          <option value="oldest">{t('discover.sortOldest')}</option>
          <option value="title">{t('discover.sortTitle')}</option>
          <option value="images">{t('discover.sortImages')}</option>
        </select>
      </label>
    </div>

    <p class="mb-3 text-[11px] leading-relaxed text-ink-500">
      {since
        ? t('discover.rangeSince', { count: limit, date: formatDay(since) })
        : t('discover.rangeCount', { count: limit })}
    </p>

    {#if items !== null}
      <div class="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div class="rounded-md bg-ink-850 px-2.5 py-2">
          <p class="text-[10px] text-ink-500">{t('discover.summaryExamined')}</p>
          <p class="mono text-sm text-ink-100">{summary.examined}</p>
        </div>
        <div class="rounded-md bg-ink-850 px-2.5 py-2">
          <p class="text-[10px] text-ink-500">{t('discover.summaryAdoptable')}</p>
          <p class="mono text-sm text-ink-100">{summary.adoptable}</p>
        </div>
        <div class="rounded-md bg-ink-850 px-2.5 py-2">
          <p class="text-[10px] text-ink-500">{t('discover.summaryKnown')}</p>
          <p class="mono text-sm text-ink-100">{summary.already_known}</p>
        </div>
        <div class="rounded-md bg-ink-850 px-2.5 py-2">
          <p class="text-[10px] text-ink-500">{t('discover.summaryOutside')}</p>
          <p class="mono text-sm text-ink-100">{summary.outside_period}</p>
        </div>
      </div>
    {/if}

    <p class="mb-3 text-[11px] leading-relaxed text-ink-400">
      {t('discover.adoptedExplain')}
    </p>

    {#if items !== null && items.length}
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <button class="btn btn-ghost btn-sm" disabled={!visibleItems.length} onclick={selectFiltered}>
          {t('discover.selectFiltered', { count: visibleItems.length })}
        </button>
        <button
          class="btn btn-ghost btn-sm"
          disabled={!selected.length}
          onclick={() => (selected = [])}
        >
          {t('discover.clearSelection')}
        </button>
        <span class="text-[11px] text-ink-400">{t('discover.selected', { count: selected.length })}</span>
        <div class="flex-1"></div>
        <button
          class="btn btn-primary btn-sm"
          disabled={!selected.length || batchRunning || busy !== null}
          onclick={adoptSelected}
        >
          {t('discover.adoptSelected', { count: selected.length })}
        </button>
      </div>
    {/if}


    {#if items === null}
      <p class="flex items-center gap-2 text-xs text-ink-400">
        <Spinner />
        {t('discover.searching')}
      </p>
    {:else if !items.length}
      <p class="text-xs text-ink-400">{reason || t('discover.allKnown')}</p>
    {:else if !visibleItems.length}
      <p class="rounded-lg border border-ink-800 px-3 py-6 text-center text-xs text-ink-400">
        {t('discover.noResults')}
      </p>
    {:else}
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {#each visibleItems as item (item.remote_post_id)}
          <article
            class="overflow-hidden rounded-lg border bg-ink-850 transition-colors {selectedSet.has(
              item.remote_post_id,
            )
              ? 'border-accent-400'
              : 'border-ink-750'}"
          >
            <div class="relative aspect-[4/3] overflow-hidden bg-ink-800">
              {#if item.cover_url}
                <img
                  class="h-full w-full object-cover"
                  src={item.cover_url}
                  alt={item.title || t('drawer.dupe.post', { id: item.remote_post_id })}
                  loading="lazy"
                />
              {:else}
                <div class="flex h-full items-center justify-center text-xs text-ink-500">
                  {t('discover.noCover')}
                </div>
              {/if}
              <label class="absolute left-2 top-2 rounded bg-ink-950/85 p-1.5 shadow">
                <input
                  type="checkbox"
                  checked={selectedSet.has(item.remote_post_id)}
                  aria-label={t('discover.selectPost', { id: item.remote_post_id })}
                  onchange={() => toggleSelection(item.remote_post_id)}
                />
              </label>
              <span class="chip absolute right-2 top-2 text-[10px]">
                {stateLabel(item.state)}
              </span>
            </div>
            <div class="space-y-2 p-3">
              <div>
                <p class="line-clamp-2 min-h-8 text-xs font-medium text-ink-100">
                  {item.title || t('drawer.dupe.post', { id: item.remote_post_id })}
                </p>
                <p class="mono mt-0.5 text-[10px] text-ink-500">
                  {t('discover.remoteId', { id: item.remote_post_id })}
                </p>
              </div>
              <div class="flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-ink-400">
                <span>
                  {item.published_at ? formatDateTime(item.published_at) : t('discover.noTime')}
                </span>
                <span>{plural('discover.images', item.image_count)}</span>
              </div>
              <div class="flex items-center gap-2">
                <a
                  class="btn btn-ghost btn-sm min-w-0 flex-1"
                  href={item.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {t('discover.viewCivitai')}
                </a>
                <button
                  class="btn btn-sm shrink-0"
                  disabled={busy === item.remote_post_id || batchRunning}
                  onclick={() => adopt(item.remote_post_id)}
                >
                  {t('discover.adopt')}
                </button>
              </div>
            </div>
          </article>
        {/each}
      </div>
    {/if}

    {#if reason && items?.length}
      <p class="mt-3 text-xs text-failed">{reason}</p>
    {/if}

    {#if nextCursor !== null}
      <div class="mt-3 flex justify-center">
        <button class="btn btn-sm" disabled={loadingMore || batchRunning} onclick={loadMore}>
          {#if loadingMore}<Spinner />{/if}
          {loadingMore ? t('discover.loadingMore') : t('discover.loadMore')}
        </button>
      </div>
    {/if}
  {:else}

    <!-- The discovery query does not return drafts: draftOnly applies only inside
         post.getInfinite's owner branch. The post.get detail query returns one by a
         known id, so that route sits right next to the list. -->
    <div class="mt-4 space-y-2 border-t border-ink-800 pt-3">
      <p class="text-[11px] font-medium text-ink-200">{t('discover.draftsTitle')}</p>
      <p class="text-[11px] leading-relaxed text-ink-400">{t('discover.draftsExplain')}</p>
      <div class="flex gap-2">
        <input
          class="input"
          placeholder={t('discover.postId')}
          inputmode="numeric"
          value={manualId}
          oninput={(event) => {
            manualId = (event.currentTarget as HTMLInputElement).value.replace(/[^0-9]/g, '')
          }}
          onkeydown={(event) => {
            if (event.key === 'Enter' && manualId && !batchRunning) adopt(Number(manualId))
          }}
        />
        <button
          class="btn btn-sm"
          disabled={!manualId || busy !== null || batchRunning}
          onclick={() => adopt(Number(manualId))}
        >
          {t('discover.adopt')}
        </button>
      </div>

    <div class="space-y-2 border-t border-ink-800 pt-3">
      <p class="text-[11px] font-medium text-ink-200">{t('discover.idsTitle')}</p>
      <p class="text-[11px] leading-relaxed text-ink-400">{t('discover.idsExplain')}</p>
      <textarea
        class="input min-h-24 font-mono text-[11px]"
        placeholder={t('discover.idsPlaceholder')}
        disabled={batchRunning}
        value={idText}
        oninput={(event) => (idText = (event.currentTarget as HTMLTextAreaElement).value)}
      ></textarea>
      <div class="flex items-center gap-2">
        <span class="text-[11px] text-ink-400">
          {t('discover.idsFound', { count: pastedIds.length })}
        </span>
        {#if pastedIds.length > MAX_IDS}
          <span class="text-[11px]" style="color: var(--color-failed);">
            {t('discover.idsTooMany', { max: MAX_IDS })}
          </span>
        {/if}
        <div class="flex-1"></div>
        <button
          class="btn btn-primary btn-sm"
          disabled={!pastedIds.length || pastedIds.length > MAX_IDS || batchRunning}
          onclick={adoptPasted}
        >
          {t('discover.adoptIds', { count: pastedIds.length })}
        </button>
      </div>
    </div>
      {#if draftsUrl}
        <a
          class="inline-block text-[11px] underline decoration-dotted"
          href={draftsUrl}
          target="_blank"
          rel="noreferrer"
          style="color: var(--color-accent-300);"
        >
          {t('discover.openDrafts')}
        </a>
      {/if}
    </div>
  {/if}

  {#if batchJob}
    <div class="mb-3 rounded-lg border border-ink-700 bg-ink-850 p-3">
      <div class="flex items-center gap-2">
        {#if batchRunning}<Spinner />{/if}
        <p class="flex-1 text-xs font-medium text-ink-100">
          {batchRunning
            ? t('discover.adopting', { processed: batchJob.processed, total: batchJob.total })
            : t('job.done.adopt', {
                created: batchJob.succeeded,
                skipped: batchJob.skipped,
                failed: batchJob.failed,
              })}
        </p>
        {#if batchRunning}
          <button class="btn btn-ghost btn-sm" onclick={() => api.cancelJob(batchJob!.id)}>
            {t('ui.cancel')}
          </button>
        {/if}
      </div>
      {#if batchJob.total}
        <div class="mt-2 h-1 overflow-hidden rounded-full bg-ink-800">
          <div
            class="h-full rounded-full bg-accent-500 transition-[width] duration-300"
            style="width: {Math.round((batchJob.processed / batchJob.total) * 100)}%;"
          ></div>
        </div>
      {/if}
      {#if batchOutcomes.length}
        <div class="mt-2 max-h-36 space-y-1 overflow-y-auto">
          {#each batchOutcomes as outcome (outcome.remote_post_id)}
            <div class="flex items-start gap-2 rounded bg-ink-900 px-2 py-1 text-[10px]">
              <span class="mono text-ink-300">
                {t('discover.remoteId', { id: outcome.remote_post_id })}
              </span>
              <span class="text-ink-400">{outcomeLabel(outcome.outcome)}</span>
              {#if localLabel(outcome)}
                <span class="text-ink-500">{localLabel(outcome)}</span>
              {/if}
              {#if outcome.post_id}
                <span class="text-ink-500">
                  {t('discover.localPost', { id: outcome.post_id })}
                </span>
              {/if}
              {#if outcome.message}
                <span class="min-w-0 flex-1 break-words text-right text-failed">
                  {outcome.message}
                </span>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
  {#snippet footer()}
    <button class="btn" onclick={onClose} disabled={batchRunning}>{t('ui.close')}</button>
  {/snippet}
</Modal>
