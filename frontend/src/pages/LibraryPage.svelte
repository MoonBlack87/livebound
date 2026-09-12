<script lang="ts">
  import { api, MIN_IMAGE_SEARCH_LENGTH } from '../api/client'
  import LibraryTile from '../components/LibraryTile.svelte'
  import BulkEditDialog from '../components/editor/BulkEditDialog.svelte'
  import ConfirmDialog from '../components/ui/ConfirmDialog.svelte'
  import Empty from '../components/ui/Empty.svelte'
  import Modal from '../components/ui/Modal.svelte'
  import SkeletonGrid from '../components/ui/SkeletonGrid.svelte'
  import { formatBytes } from '../components/ui/format.svelte'
  import { plural, t } from '../i18n/index.svelte'
  import {
    app,
    clearSelection,
    inspectImage,
    invalidate,
    openPost,
    setActiveJob,
    setSelectedImages,
    toast,
    toggleImage,
  } from '../state/store.svelte'
  import type {
    ImageRow,
    ImageSearchParams,
    LibraryDuplicateGroup,
    PostHolder,
    SourceRoot,
    UsageState,
  } from '../types/api'

  /** Minimum column width per step. The first is what v1.0 always used. */
  const GRID_STEPS = [170, 260, 400]
  const PAGE_SIZES = [50, 100, 200, 500]
  const USAGE_STATES: UsageState[] = ['all', 'unused', 'planned', 'used']

  type TrashPlanItem = { id: number; absolutePath: string; postBound: boolean }

  let sources = $state<SourceRoot[]>([])
  let folders = $state<{ folder: string; count: number }[]>([])
  let items = $state<ImageRow[]>([])
  let total = $state(0)
  let loading = $state(true)
  let sourceId = $state<number | undefined>(undefined)
  let folder = $state('')
  let query = $state('')
  let usageState = $state<UsageState>('unused')
  let offset = $state(0)
  let lastClicked: number | null = null
  let editing = $state(false)
  let duplicateGroup = $state<LibraryDuplicateGroup | null>(null)
  let trashJobId = $state<number | null>(null)
  let trashConfirmation = $state<{ items: TrashPlanItem[] } | null>(null)
  let postConfirmation = $state<{ imageIds: number[]; holders: PostHolder[] } | null>(null)
  let postBusy = $state(false)

  const selectedSet = $derived(new Set(app.selectedImages))
  const gridStep = $derived(app.settings?.ui_grid_size ?? 0)
  const columnWidth = $derived(GRID_STEPS[gridStep] ?? GRID_STEPS[0])
  const pageSize = $derived(app.settings?.ui_page_size ?? 200)
  const searchTooShort = $derived(
    query.trim().length > 0 && query.trim().length < MIN_IMAGE_SEARCH_LENGTH,
  )

  async function saveView(patch: Record<string, number>) {
    offset = 0
    await api.saveSettings(patch)
    invalidate()
  }

  $effect(() => {
    void app.revision
    api.sources().then((data) => (sources = data.items)).catch(() => undefined)
  })

  $effect(() => {
    void app.revision
    api.folders(sourceId).then((data) => (folders = data.items)).catch(() => undefined)
  })

  $effect(() => {
    const searchQuery = query.trim()
    const params: ImageSearchParams = {
      source_id: sourceId,
      folder,
      q: searchQuery,
      usage_state: usageState,
      include_missing: true,
      limit: pageSize,
      offset,
    }
    void app.revision
    if (searchQuery && searchQuery.length < MIN_IMAGE_SEARCH_LENGTH) {
      items = []
      total = 0
      loading = false
      return
    }
    const controller = new AbortController()
    loading = true
    // Debounced: without it the search box fires one request per keystroke.
    const timer = setTimeout(() => {
      api
        .images(params, controller.signal)
        .then((data) => {
          if (controller.signal.aborted) return
          items = data.items
          total = data.total
        })
        .catch((error) => {
          if (!controller.signal.aborted) toast('error', (error as Error).message)
        })
        .finally(() => !controller.signal.aborted && (loading = false))
    }, 180)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  })

  function handleClick(image: ImageRow, event: MouseEvent) {
    if (event.shiftKey && lastClicked !== null) {
      // Shift picks the whole run between the two clicks - the usual way to grab
      // a burst of variations that belong in one post.
      const from = items.findIndex((row) => row.id === lastClicked)
      const to = items.findIndex((row) => row.id === image.id)
      if (from >= 0 && to >= 0) {
        const [start, end] = from < to ? [from, to] : [to, from]
        toggleImage(
          image.id,
          items.slice(start, end + 1).map((row) => row.id),
        )
        return
      }
    }
    lastClicked = image.id
    toggleImage(image.id)
  }

  async function createPost() {
    if (!app.selectedImages.length) return
    // Keep the order the grid shows, not the order they were clicked in.
    const ordered = items.filter((row) => selectedSet.has(row.id)).map((row) => row.id)
    const missing = app.selectedImages.filter((id) => !ordered.includes(id))
    const imageIds = [...ordered, ...missing]
    try {
      // Putting a picture into a second post is allowed. The warning names the
      // posts that already hold selected images so the choice is made with that
      // information on screen.
      const holders = (await api.postHolders(imageIds)).items.flatMap((entry) => entry.posts)
      if (holders.length) {
        const unique = new Map(holders.map((holder) => [holder.post_id, holder]))
        postConfirmation = { imageIds, holders: [...unique.values()] }
        return
      }
    } catch (error) {
      // The warning is a courtesy, not a gate. If it cannot be fetched the post
      // is still created - refusing here would break a working button over a
      // failed extra question.
      toast('error', (error as Error).message)
    }
    await submitPost(imageIds)
  }

  async function submitPost(imageIds: number[]) {
    if (postBusy) return
    postBusy = true
    try {
      const post = await api.createPost({ image_ids: imageIds })
      postConfirmation = null
      clearSelection()
      invalidate()
      openPost(post.id)
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      postBusy = false
    }
  }

  async function runScan() {
    try {
      setActiveJob(await api.scan(sourceId))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  $effect(() => {
    const job = app.activeJob
    if (
      !job ||
      job.id !== trashJobId ||
      job.status === 'starting' ||
      job.status === 'running'
    ) {
      return
    }
    trashJobId = null
    // Only the grid is refreshed here. The selection was already cleared when
    // the job was submitted, because this effect lives in a component and the
    // most natural click after trashing is the new Trash section: navigate away
    // and this never runs, leaving trashed ids selected for the next New post.
    invalidate()
  })

  async function askMoveToTrash() {
    const selected = [...app.selectedImages]
    if (!selected.length) return
    try {
      const plan = await api.trashPlan(selected)
      trashConfirmation = {
        items: plan.items.map((item) => ({
          id: item.id,
          absolutePath: item.absolute_path,
          postBound: item.post_bound,
        })),
      }
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function moveToTrash(movable: TrashPlanItem[]) {
    if (!movable.length) return
    try {
      const job = await api.trashImages(movable.map((item) => item.id))
      // Cleared now rather than on completion. Everything selected was
      // submitted, so the selection has done its job, and a selection that
      // survives into another screen is how a later action reaches a file that
      // is no longer there. A move that fails leaves the image in the grid; it
      // is simply no longer selected.
      clearSelection()
      trashConfirmation = null
      trashJobId = job.id
      setActiveJob(job)
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }
</script>

<div class="flex h-full flex-col">
  <header class="flex flex-wrap items-center gap-2 border-b border-ink-800 px-5 py-3">
    <h1 class="mr-2 text-sm font-semibold">{t('nav.library')}</h1>

    <select
      class="input w-auto"
      value={sourceId ?? ''}
      onchange={(event) => {
        const value = (event.currentTarget as HTMLSelectElement).value
        sourceId = value ? Number(value) : undefined
        folder = ''
        offset = 0
      }}
    >
      <option value="">{t('library.allSources')}</option>
      {#each sources as source (source.id)}
        <option value={source.id}>{source.label || source.path.split('/').pop()}</option>
      {/each}
    </select>

    <select
      class="input w-auto max-w-56"
      value={folder}
      onchange={(event) => {
        folder = (event.currentTarget as HTMLSelectElement).value
        offset = 0
      }}
    >
      <option value="">{t('library.allFolders')}</option>
      {#each folders as entry (entry.folder)}
        <option value={entry.folder}>
          {entry.folder || t('library.rootFolder')} ({entry.count})
        </option>
      {/each}
    </select>

    <input
      class="input w-52"
      placeholder={t('library.searchPlaceholder')}
      bind:value={query}
      oninput={() => (offset = 0)}
    />
    {#if searchTooShort}
      <span class="text-xs text-amber-400">
        {t('common.searchMinimum', { count: MIN_IMAGE_SEARCH_LENGTH })}
      </span>
    {/if}

    <div
      class="flex rounded-lg border border-ink-700 bg-ink-950 p-0.5"
      role="group"
      aria-label={t('library.usageFilter')}
    >
      {#each USAGE_STATES as state (state)}
        <button
          type="button"
          class="rounded px-2 py-1 text-xs transition-colors {usageState === state
            ? 'bg-ink-750 text-ink-100'
            : 'text-ink-400 hover:text-ink-200'}"
          aria-pressed={usageState === state}
          onclick={() => {
            usageState = state
            offset = 0
          }}
        >
          {t(`library.usage.${state}`)}
        </button>
      {/each}
    </div>

    <div class="flex-1"></div>

    <div
      class="flex gap-1 rounded-lg border border-ink-700 bg-ink-950 p-0.5"
      title={t('library.gridSize')}
    >
      {#each GRID_STEPS as _, step (step)}
        <button
          type="button"
          class="rounded px-2 py-1 text-xs transition-colors {gridStep === step
            ? 'bg-ink-750 text-ink-100'
            : 'text-ink-400 hover:text-ink-200'}"
          onclick={() => saveView({ ui_grid_size: step })}
          aria-label={t('library.gridSizeStep', { step: step + 1 })}
        >
          {'▪▫▭'[step]}
        </button>
      {/each}
    </div>

    <select
      class="input w-auto"
      value={pageSize}
      title={t('library.pageSize')}
      onchange={(event) =>
        saveView({ ui_page_size: Number((event.currentTarget as HTMLSelectElement).value) })}
    >
      {#each PAGE_SIZES as size (size)}
        <option value={size}>{t('library.perPage', { count: size })}</option>
      {/each}
    </select>

    <span class="mono text-xs text-ink-400">{t('library.count', { count: total })}</span>
    <button class="btn btn-sm" onclick={runScan}>{t('library.scan')}</button>
  </header>

  {#if app.selectedImages.length}
    <div
      class="animate-in flex items-center gap-2 border-b border-ink-800 bg-ink-850 px-5 py-2.5"
    >
      <span class="text-xs font-medium text-ink-100">
        {t('library.selected', { count: app.selectedImages.length })}
      </span>
      <button
        class="btn btn-ghost btn-sm"
        onclick={() => setSelectedImages(items.map((row) => row.id))}
      >
        {t('library.selectPage')}
      </button>
      <button class="btn btn-ghost btn-sm" onclick={clearSelection}>{t('library.deselect')}</button>
      <div class="flex-1"></div>
      <button class="btn btn-sm" onclick={() => (editing = true)}>{t('metadata.bulkEdit')}</button>
      {#if app.settings?.trash_folder}
        <button class="btn btn-sm" onclick={askMoveToTrash}>{t('trash.moveSelected')}</button>
      {/if}
      <button class="btn btn-primary btn-sm" onclick={createPost}>{t('library.newPost')}</button>
    </div>
  {/if}

  <div class="min-h-0 flex-1 overflow-y-auto p-5">
    {#if searchTooShort}
      <!-- The inline minimum-length hint owns this state. -->
    {:else if loading}
      <SkeletonGrid min={columnWidth} />
    {:else if !items.length}
      <Empty
        icon="library"
        title={sources.length ? t('library.empty.title') : t('library.noSources.title')}
        hint={sources.length ? t('library.empty.hint') : t('library.noSources.hint')}
      />
    {:else}
      <div
        class="grid gap-3"
        style="grid-template-columns: repeat(auto-fill, minmax({columnWidth}px, 1fr));"
      >
        {#each items as image (image.id)}
          <LibraryTile
            {image}
            selected={selectedSet.has(image.id)}
            large={gridStep > 0}
            onclick={(event) => handleClick(image, event)}
            onInspect={() => inspectImage(image.id)}
            onOpenGroup={() =>
              image.duplicate_confidence &&
              (duplicateGroup = {
                confidence: image.duplicate_confidence,
                members: image.duplicate_members,
              })}
          />
        {/each}
      </div>
    {/if}

    {#if total > pageSize}
      <div class="mt-5 flex items-center justify-center gap-2">
        <button
          class="btn btn-sm"
          disabled={offset === 0}
          onclick={() => (offset = Math.max(0, offset - pageSize))}
        >
          {t('paging.back')}
        </button>
        <span class="mono text-xs text-ink-400">
          {t('paging.range', {
            from: offset + 1,
            to: Math.min(offset + pageSize, total),
            total,
          })}
        </span>
        <button
          class="btn btn-sm"
          disabled={offset + pageSize >= total}
          onclick={() => (offset += pageSize)}
        >
          {t('paging.next')}
        </button>
      </div>
    {/if}
  </div>

  {#if postConfirmation}
    {@const confirmation = postConfirmation}
    <ConfirmDialog
      title={t('library.alreadyPosted.title')}
      confirmLabel={t('library.alreadyPosted.create')}
      busy={postBusy}
      onCancel={() => (postConfirmation = null)}
      onConfirm={() => void submitPost(confirmation.imageIds)}
    >
      {#snippet body()}
        <p>{plural('library.alreadyPosted.body', confirmation.holders.length)}</p>
        <ul class="max-h-32 space-y-1 overflow-y-auto text-xs text-ink-400">
          {#each confirmation.holders as holder (holder.post_id)}
            <li class="break-all">
              {holder.title || t('library.alreadyPosted.untitled')}
              <span class="mono">· {t(`state.${holder.state}`)}</span>
            </li>
          {/each}
        </ul>
      {/snippet}
    </ConfirmDialog>
  {/if}

  {#if trashConfirmation}
    {@const confirmation = trashConfirmation}
    {@const movable = confirmation.items.filter((item) => !item.postBound)}
    {@const postBound = confirmation.items.filter((item) => item.postBound)}
    <ConfirmDialog
      title={t(movable.length ? 'trash.confirm.title' : 'trash.confirm.noneTitle')}
      confirmLabel={t(movable.length ? 'trash.confirm.move' : 'ui.close')}
      onCancel={() => (trashConfirmation = null)}
      onConfirm={() => {
        if (movable.length) void moveToTrash(movable)
        else trashConfirmation = null
      }}
    >
      {#snippet body()}
        {#if movable.length}
          <p>{plural('trash.confirm.movable', movable.length)}</p>
          <ul class="mono max-h-32 space-y-1 overflow-y-auto text-xs text-ink-400">
            {#each movable as item (item.id)}
              <li class="break-all">{item.absolutePath}</li>
            {/each}
          </ul>
        {/if}
        {#if postBound.length}
          <p class="text-ink-400">{plural('trash.confirm.postBound', postBound.length)}</p>
          <ul class="mono max-h-32 space-y-1 overflow-y-auto text-xs text-ink-400">
            {#each postBound as item (item.id)}
              <li class="break-all">{item.absolutePath}</li>
            {/each}
          </ul>
        {/if}
      {/snippet}
    </ConfirmDialog>
  {/if}
</div>

{#if editing}
  <BulkEditDialog
    imageIds={app.selectedImages}
    count={app.selectedImages.length}
    onClose={() => (editing = false)}
    onApplied={() => {
      editing = false
      clearSelection()
      invalidate()
    }}
  />
{/if}

{#if duplicateGroup}
  {@const members = duplicateGroup.members}
  {@const confidence = t(`duplicates.confidence.${duplicateGroup.confidence}`)}
  <Modal
    title={t('library.group.title', { confidence, count: members.length })}
    wide
    onClose={() => (duplicateGroup = null)}
  >
    <p class="mb-3 text-xs leading-relaxed text-ink-400">
      {t(`library.group.hint.${duplicateGroup.confidence}`)}
    </p>
    <div class="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
      {#each members as member (member.id)}
        <button
          type="button"
          class="overflow-hidden rounded-lg border border-ink-750 bg-ink-850 text-left transition-colors hover:border-ink-600"
          onclick={() => {
            duplicateGroup = null
            inspectImage(member.id)
          }}
          title={t('library.detailsHint')}
        >
          <img
            src={api.thumbnailUrl(member.id, true, member.thumbnail_path)}
            alt={member.relative_path}
            loading="lazy"
            class="h-48 w-full bg-ink-950 object-contain"
          />
          <div class="p-2">
            <p class="truncate text-xs text-ink-100" title={member.absolute_path}>
              {member.relative_path.split('/').pop()}
            </p>
            <p class="mono mt-1 truncate text-[10px] text-ink-500" title={member.absolute_path}>
              {member.absolute_path}
            </p>
            <p class="mono mt-1 text-[10px] text-ink-400">
              {member.width}×{member.height} · {formatBytes(member.file_size)}
            </p>
          </div>
        </button>
      {/each}
    </div>
  </Modal>
{/if}
