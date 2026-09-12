<script lang="ts">
  import { api, MIN_IMAGE_SEARCH_LENGTH } from '../api/client'
  import { t } from '../i18n/index.svelte'
  import Modal from './ui/Modal.svelte'
  import Spinner from './ui/Spinner.svelte'
  import { formatBytes } from './ui/format.svelte'
  import type {
    ImageRow,
    ImageSearchParams,
    ManualMatchCandidate,
    ManualMatchReason,
    SourceRoot,
  } from '../types/api'

  /**
   * Picking images for a post that already exists.
   *
   * Deliberately a separate dialog rather than a trip back to the library:
   * adding one more shot to an open post should not mean losing the editor and
   * rebuilding the selection from scratch.
   */

  let {
    exclude,
    onCancel,
    onPick,
    title,
    confirmLabel,
    single = false,
    referenceUrl = null,
    suggestions = [],
    suggestionsLoading = false,
    suggestionsUnavailable = false,
  }: {
    /** Already in the post - shown as taken, not offered again. */
    exclude: number[]
    onCancel: () => void
    onPick: (imageIds: number[]) => void
    title?: string
    confirmLabel?: string
    single?: boolean
    referenceUrl?: string | null
    suggestions?: ManualMatchCandidate[]
    suggestionsLoading?: boolean
    suggestionsUnavailable?: boolean
  } = $props()

  let items = $state<ImageRow[]>([])
  let sources = $state<SourceRoot[]>([])
  let folders = $state<{ folder: string; count: number }[]>([])
  let sourceId = $state<number | undefined>(undefined)
  let folder = $state('')
  let query = $state('')
  let unusedOnly = $state(false)
  let selected = $state<number[]>([])
  let loading = $state(true)
  let loadingMore = $state(false)
  let total = $state(0)

  const taken = $derived(new Set(exclude))
  const modalTitle = $derived(title ?? t('picker.title'))
  const actionLabel = $derived(confirmLabel ?? t('picker.add'))
  const searchTooShort = $derived(
    query.trim().length > 0 && query.trim().length < MIN_IMAGE_SEARCH_LENGTH,
  )

  $effect(() => {
    api.sources().then((data) => (sources = data.items)).catch(() => undefined)
  })

  $effect(() => {
    api.folders(sourceId).then((data) => (folders = data.items)).catch(() => undefined)
  })

  $effect(() => {
    const searchQuery = query.trim()
    const params: ImageSearchParams = {
      source_id: sourceId,
      folder,
      q: searchQuery,
      usage_state: unusedOnly ? 'unused' : 'all',
      limit: 120,
    }
    if (searchQuery && searchQuery.length < MIN_IMAGE_SEARCH_LENGTH) {
      items = []
      loading = false
      return
    }
    const controller = new AbortController()
    loading = true
    const timer = setTimeout(() => {
      api
        .images(params, controller.signal)
        .then((data) => {
          if (controller.signal.aborted) return
          items = data.items
          total = data.total
        })
        .catch(() => undefined)
        .finally(() => !controller.signal.aborted && (loading = false))
    }, 200)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  })

  function toggle(id: number) {
    selected = selected.includes(id)
      ? selected.filter((item) => item !== id)
      : single
        ? [id]
        : [...selected, id]
  }

  async function loadMore() {
    const searchQuery = query.trim()
    const snapshot = `${sourceId ?? ''}|${folder}|${searchQuery}|${unusedOnly}`
    loadingMore = true
    try {
      const data = await api.images({
        source_id: sourceId,
        folder,
        q: searchQuery,
        usage_state: unusedOnly ? 'unused' : 'all',
        limit: 120,
        offset: items.length,
      })
      if (`${sourceId ?? ''}|${folder}|${query.trim()}|${unusedOnly}` !== snapshot) return
      const known = new Set(items.map((image) => image.id))
      items = [...items, ...data.items.filter((image) => !known.has(image.id))]
      total = data.total
    } catch {
      // The visible page remains usable; the user can retry the same button.
    } finally {
      loadingMore = false
    }
  }

  function reasonLabel(reason: ManualMatchReason): string {
    if (reason.code === 'close_dhash') {
      return t('picker.reason.closeDhash', { distance: reason.distance ?? '?' })
    }
    const keys = {
      identical_bytes: 'picker.reason.identicalBytes',
      identical_pixels: 'picker.reason.identicalPixels',
      same_seed_prompt: 'picker.reason.sameSeedPrompt',
      same_prompt: 'picker.reason.samePrompt',
      same_seed: 'picker.reason.sameSeed',
      remote_filename: 'picker.reason.remoteFilename',
      same_dimensions: 'picker.reason.sameDimensions',
    } as const
    return t(keys[reason.code])
  }
</script>

<Modal title={modalTitle} wide onClose={onCancel}>
  {#if referenceUrl}
    <div class="mb-4 grid gap-3 rounded-lg border border-ink-750 bg-ink-850 p-3 sm:grid-cols-[132px_1fr]">
      <div>
        <p class="label mb-1.5">{t('picker.remoteImage')}</p>
        <img
          src={referenceUrl}
          alt=""
          class="aspect-[3/4] w-full rounded-md bg-ink-900 object-cover"
        />
      </div>
      <div class="min-w-0">
        <p class="label mb-1">{t('picker.suggestions')}</p>
        <p class="mb-2 text-[11px] leading-relaxed text-ink-400">
          {t('picker.suggestionsHint')}
        </p>
        {#if suggestionsLoading}
          <div class="flex justify-center py-8"><Spinner /></div>
        {:else if suggestionsUnavailable}
          <p class="rounded-md border border-amber-500/30 bg-amber-950/20 p-2 text-xs text-amber-200">
            {t('picker.suggestionsUnavailable')}
          </p>
        {:else if !suggestions.length}
          <p class="rounded-md border border-ink-750 p-2 text-xs text-ink-400">
            {t('picker.noSuggestions')}
          </p>
        {:else}
          <div class="grid grid-cols-[repeat(auto-fill,minmax(118px,1fr))] gap-2">
            {#each suggestions as image (image.id)}
              {@const already = taken.has(image.id)}
              {@const active = selected.includes(image.id)}
              <button
                type="button"
                disabled={already}
                onclick={() => toggle(image.id)}
                class="overflow-hidden rounded-lg border text-left transition-all {active
                  ? 'border-accent-400 ring-2 ring-accent-500/40'
                  : already
                    ? 'border-ink-800 opacity-35'
                    : 'border-ink-750 hover:border-ink-600'}"
                title={already ? t('picker.alreadyIn') : image.relative_path}
              >
                <span class="relative block">
                  <img
                    src={api.thumbnailUrl(image.id, false, image.thumbnail_path)}
                    alt=""
                    loading="lazy"
                    class="aspect-[3/4] w-full bg-ink-900 object-cover"
                  />
                  {#if image.used}
                    <span
                      class="absolute left-1 top-1 rounded px-1 py-0.5 text-[9px] font-semibold"
                      style="background: rgb(220 38 38 / 0.85); color: #fff;"
                    >
                      {t('picker.posted')}
                    </span>
                  {/if}
                  {#if active}
                    <span
                      class="absolute right-1 top-1 flex h-4 w-4 items-center justify-center rounded-full text-[10px] font-bold"
                      style="background: var(--color-accent-500); color: #04211f;"
                    >✓</span>
                  {/if}
                </span>
                <span class="block truncate px-1.5 pt-1 text-[9px] text-ink-300">
                  {image.relative_path}
                </span>
                <span class="flex flex-wrap gap-1 p-1.5 pt-1">
                  {#each image.reasons as reason}
                    <span class="rounded bg-ink-750 px-1 py-0.5 text-[9px] text-ink-300">
                      {reasonLabel(reason)}
                    </span>
                  {/each}
                </span>
              </button>
            {/each}
          </div>
        {/if}
      </div>
    </div>
    <p class="label mb-2">{t('picker.allLocalImages')}</p>
  {/if}

  <div class="mb-3 flex flex-wrap items-center gap-2">
    <select
      class="input w-auto"
      value={sourceId ?? ''}
      onchange={(event) => {
        const value = (event.currentTarget as HTMLSelectElement).value
        sourceId = value ? Number(value) : undefined
        folder = ''
      }}
    >
      <option value="">{t('library.allSources')}</option>
      {#each sources as source (source.id)}
        <option value={source.id}>
          {source.label || source.path.split('/').filter(Boolean).pop()}
        </option>
      {/each}
    </select>
    <select class="input w-auto max-w-48" bind:value={folder}>
      <option value="">{t('library.allFolders')}</option>
      {#each folders as entry (entry.folder)}
        <option value={entry.folder}>
          {entry.folder || t('library.rootFolder')} ({entry.count})
        </option>
      {/each}
    </select>
    <input class="input w-44" placeholder={t('picker.search')} bind:value={query} />
    {#if searchTooShort}
      <span class="text-xs text-amber-400">
        {t('common.searchMinimum', { count: MIN_IMAGE_SEARCH_LENGTH })}
      </span>
    {/if}
    <label class="chip cursor-pointer select-none">
      <input type="checkbox" class="accent-teal-500" bind:checked={unusedOnly} />
      {t('library.availableOnly')}
    </label>
  </div>

  {#if searchTooShort}
    <!-- The inline minimum-length hint owns this state. -->
  {:else if loading}
    <div class="flex justify-center py-10"><Spinner /></div>
  {:else if !items.length}
    <p class="py-10 text-center text-xs text-ink-500">{t('picker.empty')}</p>
  {:else}
    <div class="grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-2">
      {#each items as image (image.id)}
        {@const already = taken.has(image.id)}
        {@const active = selected.includes(image.id)}
        <button
          type="button"
          disabled={already}
          onclick={() => toggle(image.id)}
          class="relative overflow-hidden rounded-lg border transition-all {active
            ? 'border-accent-400 ring-2 ring-accent-500/40'
            : already
              ? 'border-ink-800 opacity-35'
              : 'border-ink-750 hover:border-ink-600'}"
          title={already ? t('picker.alreadyIn') : image.relative_path}
        >
          <img
            src={api.thumbnailUrl(image.id, false, image.thumbnail_path)}
            alt=""
            loading="lazy"
            class="aspect-[3/4] w-full bg-ink-850 object-cover"
          />
          {#if already}
            <span
              class="absolute inset-x-0 bottom-0 bg-black/80 py-0.5 text-[10px] text-ink-300"
            >
              {t('picker.alreadyInShort')}
            </span>
          {:else if image.used}
            <span
              class="absolute left-1 top-1 rounded px-1 py-0.5 text-[9px] font-semibold"
              style="background: rgb(220 38 38 / 0.85); color: #fff;"
            >
              {t('picker.posted')}
            </span>
          {:else if image.planned}
            <span
              class="absolute left-1 top-1 rounded px-1 py-0.5 text-[9px] font-semibold"
              style="background: rgb(37 99 235 / 0.85); color: #fff;"
              title={t('library.badge.plannedHint')}
            >
              {t('library.badge.planned')}
            </span>
          {/if}
          {#if active}
            <span
              class="absolute right-1 top-1 flex h-4 w-4 items-center justify-center rounded-full text-[10px] font-bold"
              style="background: var(--color-accent-500); color: #04211f;"
            >
              ✓
            </span>
          {/if}
          <span
            class="absolute inset-x-0 bottom-0 truncate bg-gradient-to-t from-black/85 to-transparent px-1 pb-0.5 pt-3 text-[9px] text-ink-300"
          >
            {image.width}×{image.height} · {formatBytes(image.file_size)}
          </span>
        </button>
      {/each}
    </div>
    {#if items.length < total}
      <div class="flex justify-center pt-3">
        <button class="btn btn-sm" disabled={loadingMore} onclick={loadMore}>
          {loadingMore
            ? t('picker.loadingMore')
            : t('picker.loadMore', { count: total - items.length })}
        </button>
      </div>
    {/if}
  {/if}

  {#snippet footer()}
    <span class="mr-auto text-xs text-ink-400">{t('library.selected', { count: selected.length })}</span>
    <button class="btn" onclick={onCancel}>{t('ui.cancel')}</button>
    <button class="btn btn-primary" disabled={!selected.length} onclick={() => onPick(selected)}>
      {actionLabel}
    </button>
  {/snippet}
</Modal>
