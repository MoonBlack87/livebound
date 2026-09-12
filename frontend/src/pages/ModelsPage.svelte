<script lang="ts">
  import { api } from '../api/client'
  import Empty from '../components/ui/Empty.svelte'
  import Modal from '../components/ui/Modal.svelte'
  import Spinner from '../components/ui/Spinner.svelte'
  import { formatBytes } from '../components/ui/format.svelte'
  import { plural, t } from '../i18n/index.svelte'
  import { modelUrl } from '../lib/civitai'
  import { app, invalidate, setActiveJob, toast } from '../state/store.svelte'
  import type { ModelFileDuplicateGroup, ModelFileRow } from '../types/api'

  type InventoryFilter = 'all' | 'recognized' | 'unrecognized' | 'unqueried' | 'duplicates'

  const PAGE_SIZE = 50
  const DUPLICATE_PAGE_SIZE = 50
  const FILTERS: InventoryFilter[] = [
    'all',
    'recognized',
    'unrecognized',
    'unqueried',
    'duplicates',
  ]

  let items = $state<ModelFileRow[]>([])
  let duplicateGroups = $state<ModelFileDuplicateGroup[]>([])
  let duplicateTotal = $state(0)
  let duplicateOffset = $state(0)
  let duplicateLoading = $state(true)
  let redundantBytes = $state(0)
  let total = $state(0)
  let offset = $state(0)
  let query = $state('')
  let filter = $state<InventoryFilter>('all')
  let loading = $state(true)
  let assigning = $state<ModelFileRow | null>(null)
  let assignmentUrl = $state('')
  let savingAssignment = $state(false)
  let resolving = $state<number[]>([])

  $effect(() => {
    const params = {
      filter_name: filter,
      q: query,
      limit: PAGE_SIZE,
      offset,
    }
    void app.revision
    let cancelled = false
    loading = true
    const timer = setTimeout(() => {
      api
        .modelFiles(params)
        .then((data) => {
          if (cancelled) return
          items = data.items
          total = data.total
        })
        .catch((error) => toast('error', (error as Error).message))
        .finally(() => !cancelled && (loading = false))
    }, 180)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  })

  $effect(() => {
    const params = { limit: DUPLICATE_PAGE_SIZE, offset: duplicateOffset }
    void app.revision
    let cancelled = false
    duplicateLoading = true
    api
      .modelFileDuplicates(params)
      .then((data) => {
        if (cancelled) return
        duplicateGroups = data.groups
        duplicateTotal = data.total
        redundantBytes = data.redundant_size
      })
      .catch((error) => toast('error', (error as Error).message))
      .finally(() => !cancelled && (duplicateLoading = false))
    return () => {
      cancelled = true
    }
  })

  function setFilter(next: InventoryFilter) {
    filter = next
    offset = 0
  }

  function modelLabel(row: ModelFileRow): string {
    const model = row.model_name ??
      (row.model_id ? t('modelInventory.modelId', { id: row.model_id }) : null)
    const version = row.version_name ??
      t('modelInventory.versionId', { id: row.model_version_id ?? '' })
    return model ? `${model} · ${version}` : version
  }

  function openAssignment(row: ModelFileRow) {
    if (row.id == null) return
    assigning = row
    assignmentUrl = modelUrl(row.model_id, row.model_version_id) ?? ''
  }

  // The same job the settings offer, where the models are - as the library has
  // its scan. One implementation, two places to start it from.
  async function hashAll() {
    try {
      setActiveJob(await api.hashModelRoots())
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function saveAssignment() {
    if (!assigning || !assignmentUrl.trim()) return
    savingAssignment = true
    try {
      if (assigning.id == null) return
      await api.assignModelFile(assigning.id, assignmentUrl.trim())
      toast('success', t('modelInventory.assignment.saved'))
      assigning = null
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      savingAssignment = false
    }
  }

  async function resolveOne(row: ModelFileRow) {
    if (row.id == null) return
    resolving = [...resolving, row.id]
    try {
      const result = await api.resolveModelFile(row.id)
      toast(
        'success',
        row.source === 'manual'
          ? t('modelInventory.resolve.manualKept')
          : t('modelInventory.resolve.done', {
              resolved: result.resolved,
              unknown: result.unknown,
            }),
      )
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      resolving = resolving.filter((id) => id !== row.id)
    }
  }
</script>

<div class="flex h-full flex-col">
  <header class="flex flex-wrap items-center gap-2 border-b border-ink-800 px-5 py-3">
    <h1 class="mr-2 text-sm font-semibold">{t('nav.models')}</h1>
    <button class="btn btn-sm" onclick={hashAll}>{t('settings.modelRoots.hashAll')}</button>
    <input
      class="input w-56"
      placeholder={t('modelInventory.search')}
      bind:value={query}
      oninput={() => (offset = 0)}
    />
    <div class="flex items-center gap-1" aria-label={t('modelInventory.filter.label')}>
      {#each FILTERS as option}
        <button
          type="button"
          class={filter === option ? 'btn btn-primary btn-sm' : 'btn btn-ghost btn-sm'}
          aria-pressed={filter === option}
          onclick={() => setFilter(option)}
        >
          {t(`modelInventory.filter.${option}`)}
        </button>
      {/each}
    </div>
    <div class="flex-1"></div>
    <span class="mono text-xs text-ink-400">{plural('modelInventory.entries', total)}</span>
  </header>

  <div class="min-h-0 flex-1 overflow-y-auto p-5">
    {#if loading}
      <Spinner />
    {:else}
      <section>
        <p class="mb-3 text-[11px] text-ink-500">{t('modelInventory.assignment.hint')}</p>
        {#if !items.length}
          <Empty
            icon="models"
            title={t('modelInventory.empty')}
            hint={t('modelInventory.emptyHint')}
          />
        {:else}
          <div class="space-y-1.5">
            {#each items as row (`${row.sha256}:${row.absolute_path ?? 'unavailable'}`)}
              <article
                class="grid gap-3 rounded-lg border border-ink-750 bg-ink-850 px-3 py-2.5 lg:grid-cols-[minmax(14rem,1fr)_minmax(16rem,1.3fr)_minmax(16rem,1.2fr)_auto] lg:items-center"
              >
                <div class="min-w-0">
                  <p
                    class="truncate text-xs font-medium text-ink-100"
                    title={row.file_stem ?? row.model_name ?? row.sha256}
                  >
                    {row.file_stem ?? row.model_name ?? t('modelInventory.hash', { hash: row.sha256.slice(0, 12) })}
                  </p>
                  {#if row.locally_available}
                    <p class="mono truncate text-[10.5px] text-ink-500" title={row.folder ?? ''}>
                      {row.folder}
                    </p>
                  {:else}
                    <p class="text-[10.5px] text-amber-300">
                      {t('modelInventory.localUnavailable')}
                    </p>
                  {/if}
                </div>
                <div class="min-w-0 text-[11px]">
                  {#if row.recognized}
                    {#if modelUrl(row.model_id, row.model_version_id)}
                      <a
                        class="block truncate text-teal-300 hover:underline"
                        href={modelUrl(row.model_id, row.model_version_id) ?? undefined}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {modelLabel(row)} ↗
                      </a>
                    {:else}
                      <span class="block truncate text-ink-300">{modelLabel(row)}</span>
                    {/if}
                  {:else if row.http_status === 404}
                    <span class="text-ink-500">{t('modelInventory.unrecognized')}</span>
                  {:else}
                    <span class="text-ink-500">{t('modelInventory.unqueried')}</span>
                  {/if}
                </div>
                <div class="min-w-0 space-y-0.5">
                  {#each row.identities as identity (`${identity.source}:${identity.name}`)}
                    <p class="truncate text-[10.5px] text-ink-400" title={identity.name}>
                      <span class="mono text-ink-600">
                        {t(`modelInventory.identity.${identity.source}`)}
                      </span>
                      · {identity.name}
                    </p>
                  {/each}
                </div>
                <div class="flex flex-wrap items-center justify-end gap-1.5">
                  {#if row.file_size != null}
                    <span class="mono mr-1 text-[11px] text-ink-400">
                      {formatBytes(row.file_size)}
                    </span>
                  {/if}
                  {#if row.source === 'manual'}
                    <span class="chip text-[10px]">{t('modelInventory.assignment.manual')}</span>
                  {/if}
                  {#if row.locally_available && row.id != null}
                    <button class="btn btn-ghost btn-sm" onclick={() => openAssignment(row)}>
                      {t('modelInventory.assignment.button')}
                    </button>
                    <button
                      class="btn btn-ghost btn-sm"
                      disabled={resolving.includes(row.id)}
                      title={t('modelInventory.resolve.hint')}
                      onclick={() => resolveOne(row)}
                    >
                      {t('modelInventory.resolve.button')}
                    </button>
                  {/if}
                </div>
              </article>
            {/each}
          </div>

          {#if total > PAGE_SIZE}
            <div class="mt-5 flex items-center justify-center gap-2">
              <button
                class="btn btn-sm"
                disabled={offset === 0}
                onclick={() => (offset = Math.max(0, offset - PAGE_SIZE))}
              >
                {t('paging.back')}
              </button>
              <span class="mono text-xs text-ink-400">
                {t('paging.range', {
                  from: offset + 1,
                  to: Math.min(offset + PAGE_SIZE, total),
                  total,
                })}
              </span>
              <button
                class="btn btn-sm"
                disabled={offset + PAGE_SIZE >= total}
                onclick={() => (offset += PAGE_SIZE)}
              >
                {t('paging.next')}
              </button>
            </div>
          {/if}
        {/if}
      </section>

      <section class="mt-8 border-t border-ink-800 pt-5">
        <div class="mb-3 flex flex-wrap items-baseline gap-2">
          <h2 class="text-sm font-semibold">{t('modelInventory.duplicates.title')}</h2>
          <span class="mono text-xs text-ink-400">
            {plural('modelInventory.duplicates.groups', duplicateTotal)} ·
            {formatBytes(redundantBytes)} {t('modelInventory.duplicates.redundant')}
          </span>
        </div>
        <p class="mb-3 text-[11px] text-ink-500">{t('modelInventory.duplicates.hint')}</p>
        {#if duplicateLoading}
          <Spinner />
        {:else if duplicateGroups.length}
          <div class="space-y-2">
            {#each duplicateGroups as group (group.sha256)}
              <article class="rounded-lg border border-ink-750 bg-ink-850 px-3 py-2.5">
                <div class="mb-1.5 flex flex-wrap items-center gap-2 text-[11px]">
                  <span class="mono text-ink-300">sha {group.sha256.slice(0, 12)}</span>
                  <span class="text-ink-500">
                    {plural('modelInventory.duplicates.copies', group.copies)} ·
                    {formatBytes(group.file_size)} · {formatBytes(group.redundant_size)}
                    {t('modelInventory.duplicates.redundant')}
                  </span>
                </div>
                <ul class="mono space-y-1 text-[10.5px] text-ink-400">
                  {#each group.paths as path (path.absolute_path)}
                    <li class="break-all">{path.absolute_path}</li>
                  {/each}
                </ul>
              </article>
            {/each}
          </div>
          {#if duplicateTotal > DUPLICATE_PAGE_SIZE}
            <div class="mt-5 flex items-center justify-center gap-2">
              <button
                class="btn btn-sm"
                disabled={duplicateOffset === 0}
                onclick={() =>
                  (duplicateOffset = Math.max(0, duplicateOffset - DUPLICATE_PAGE_SIZE))}
              >
                {t('paging.back')}
              </button>
              <span class="mono text-xs text-ink-400">
                {t('paging.range', {
                  from: duplicateOffset + 1,
                  to: Math.min(duplicateOffset + DUPLICATE_PAGE_SIZE, duplicateTotal),
                  total: duplicateTotal,
                })}
              </span>
              <button
                class="btn btn-sm"
                disabled={duplicateOffset + DUPLICATE_PAGE_SIZE >= duplicateTotal}
                onclick={() => (duplicateOffset += DUPLICATE_PAGE_SIZE)}
              >
                {t('paging.next')}
              </button>
            </div>
          {/if}
        {:else}
          <p class="text-xs text-ink-500">{t('modelInventory.duplicates.none')}</p>
        {/if}
      </section>
    {/if}
  </div>
</div>

{#if assigning}
  <Modal title={t('modelInventory.assignment.title')} onClose={() => (assigning = null)}>
    <div class="space-y-3 text-sm text-ink-200">
      <p class="text-ink-400">{t('modelInventory.assignment.explain')}</p>
      <label class="label" for="model-assignment-url">
        {t('modelInventory.assignment.url')}
      </label>
      <input
        id="model-assignment-url"
        class="input w-full"
        bind:value={assignmentUrl}
        placeholder="https://civitai.red/models/…?modelVersionId=…"
      />
    </div>
    {#snippet footer()}
      <button class="btn" onclick={() => (assigning = null)}>{t('ui.cancel')}</button>
      <button
        class="btn btn-primary"
        disabled={!assignmentUrl.trim() || savingAssignment}
        onclick={saveAssignment}
      >
        {t('modelInventory.assignment.save')}
      </button>
    {/snippet}
  </Modal>
{/if}
