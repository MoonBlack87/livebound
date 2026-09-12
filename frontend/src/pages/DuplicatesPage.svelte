<script lang="ts">
  import { api } from '../api/client'
  import ConfirmDialog from '../components/ui/ConfirmDialog.svelte'
  import Empty from '../components/ui/Empty.svelte'
  import Spinner from '../components/ui/Spinner.svelte'
  import { formatBytes, formatDateTime } from '../components/ui/format.svelte'
  import { plural, t } from '../i18n/index.svelte'
  import { app, invalidate, inspectImage, setActiveJob, toast } from '../state/store.svelte'
  import type {
    DuplicateGroup,
    DuplicateMember,
    DuplicateReport,
    Job,
  } from '../types/api'

  /**
   * The same picture, twice on disk.
   *
   * Certain groups have identical file bytes or decoded pixels. Probable ones
   * only look alike - matching generation details cannot identify one output
   * from a batch, so nothing is deleted without being asked twice.
   */

  let report = $state<DuplicateReport | null>(null)
  let loading = $state(true)
  let deletion = $state<{
    imageIds: number[]
    withSidecars: boolean
    paths: string[]
  } | null>(null)
  let planningDeletion = $state(false)
  let selected = $state<number[]>([])
  let withSidecars = $state(true)
  let confidenceFilter = $state<'all' | 'certain' | 'probable'>('all')
  let trashJobId = $state<number | null>(null)
  let dismissing = $state<string[]>([])

  const confidenceFilters: ('all' | 'certain' | 'probable')[] = [
    'all',
    'certain',
    'probable',
  ]

  const groups = $derived(report?.groups ?? [])
  const visibleGroups = $derived(
    confidenceFilter === 'all'
      ? groups
      : groups.filter((group) => group.confidence === confidenceFilter),
  )
  const selectedSet = $derived(new Set(selected))
  const archiveDuplicateIds = $derived(
    new Set(
      groups
        .filter((group) => group.archive_duplicate)
        .flatMap((group) => group.members.map(({ id }) => id)),
    ),
  )
  const certain = $derived(visibleGroups.filter((group) => group.confidence === 'certain'))
  const probable = $derived(visibleGroups.filter((group) => group.confidence === 'probable'))
  const wasted = $derived(
    groups.filter((group) => !group.archive_duplicate).reduce(
      (sum, group) =>
        sum +
        group.members
          .slice(1)
          .reduce((inner, member) => inner + (member.file_size ?? 0), 0),
      0,
    ),
  )

  $effect(() => {
    void app.revision
    loading = true
    api.duplicates().then((value) => {
      report = value
      const protectedIds = new Set(
        value.groups
          .filter((group) => group.archive_duplicate)
          .flatMap((group) => group.members.map(({ id }) => id)),
      )
      const visible = new Set(value.groups.flatMap((group) => group.members.map(({ id }) => id)))
      selected = selected.filter((id) => visible.has(id) && !protectedIds.has(id))
    })
      .catch((error) => toast('error', (error as Error).message))
      .finally(() => (loading = false))
  })

  $effect(() => {
    const job = app.activeJob
    if (
      !job ||
      job.kind !== 'trash' ||
      job.status === 'starting' ||
      job.status === 'running' ||
      trashJobId !== job.id
    ) {
      return
    }
    trashJobId = null
    const remaining = remainingTrashSelection(job)
    if (remaining !== null) selected = remaining
  })

  async function scan() {
    try {
      setActiveJob(await api.scanDuplicates())
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  function toggle(id: number) {
    if (archiveDuplicateIds.has(id)) return
    selected = selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]
  }

  function selectAllButFirst(chosenGroups: DuplicateGroup[]) {
    selected = [
      ...new Set([
        ...selected,
        ...chosenGroups
          .flatMap((group) => group.members.slice(1).map(({ id }) => id))
          .filter((id) => !archiveDuplicateIds.has(id)),
      ]),
    ]
  }

  function membersFor(imageIds: number[]): DuplicateMember[] {
    const byId = new Map(groups.flatMap((group) => group.members).map((member) => [member.id, member]))
    return imageIds.flatMap((id) => {
      const member = byId.get(id)
      return member ? [member] : []
    })
  }

  function confidenceCounts(imageIds: number[]) {
    const certainIds = new Set(
      groups
        .filter((group) => group.confidence === 'certain')
        .flatMap((group) => group.members.map(({ id }) => id)),
    )
    const probableIds = new Set(
      groups
        .filter((group) => group.confidence === 'probable')
        .flatMap((group) => group.members.map(({ id }) => id)),
    )
    return imageIds.reduce(
      (counts, id) => {
        if (certainIds.has(id)) counts.certain += 1
        else if (probableIds.has(id)) counts.probable += 1
        return counts
      },
      { certain: 0, probable: 0 },
    )
  }

  async function prepareDeletion(imageIds: number[]) {
    const includeSidecars = withSidecars
    planningDeletion = true
    try {
      const plan = await api.imageDeletionPlan(imageIds, includeSidecars)
      deletion = { imageIds, withSidecars: includeSidecars, paths: plan.paths }
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      planningDeletion = false
    }
  }

  async function remove(imageIds: number[], includeSidecars: boolean) {
    deletion = null
    try {
      const result = await api.deleteImages(imageIds, includeSidecars)
      const failedPaths = new Set(result.failed.map(({ path }) => path))
      selected = membersFor(imageIds)
        .filter(({ absolute_path }) => failedPaths.has(absolute_path))
        .map(({ id }) => id)
      invalidate()
      if (result.failed.length) {
        const failures = result.failed.map(({ path, reason }) => `${path}: ${reason}`).join(' · ')
        toast(
          'error',
          t('duplicates.deleteFailed', {
            deleted: result.deleted,
            failed: result.failed.length,
            failures,
          }),
        )
        return
      }
      toast('success', plural('duplicates.deleted', result.deleted))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function moveToTrash(imageIds: number[]) {
    try {
      const job = await api.trashImages(imageIds, withSidecars)
      trashJobId = job.id
      setActiveJob(job)
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function dismissGroup(group: DuplicateGroup) {
    dismissing = [...dismissing, group.key]
    try {
      await api.dismissDuplicates(group.members.map(({ id }) => id))
      selected = selected.filter((id) => !group.members.some((member) => member.id === id))
      invalidate()
      toast('success', t('duplicates.dismissed'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      dismissing = dismissing.filter((key) => key !== group.key)
    }
  }

  function remainingTrashSelection(job: Job): number[] | null {
    const failures = job.result.failed
    const unstarted = job.result.unstarted_image_ids
    if (!Array.isArray(failures) && !Array.isArray(unstarted)) return null

    const failedIds = Array.isArray(failures)
      ? failures.flatMap((failure) => {
          if (typeof failure !== 'object' || failure === null || !('image_id' in failure)) return []
          return typeof failure.image_id === 'number' ? [failure.image_id] : []
        })
      : []
    const unstartedIds = Array.isArray(unstarted)
      ? unstarted.filter((imageId): imageId is number => typeof imageId === 'number')
      : []
    return [...new Set([...failedIds, ...unstartedIds])]
  }

</script>

<div class="flex h-full flex-col">
  <header class="flex flex-wrap items-center gap-2 border-b border-ink-800 px-5 py-3">
    <h1 class="text-sm font-semibold">{t('nav.duplicates')}</h1>
    {#if report?.checked_at}
      <span class="mono text-xs text-ink-400">
        {t('duplicates.checkedAt', { when: formatDateTime(report.checked_at) })}
      </span>
    {/if}
    {#if groups.length}
      <span class="chip text-[11px]">
        {plural('duplicates.groups', groups.length)} · {formatBytes(wasted)}
        {t('duplicates.wasted')}
      </span>
      <div class="flex items-center gap-1" aria-label={t('duplicates.filter.label')}>
        {#each confidenceFilters as option}
          <button
            type="button"
            class={confidenceFilter === option ? 'btn btn-primary btn-sm' : 'btn btn-ghost btn-sm'}
            aria-pressed={confidenceFilter === option}
            onclick={() => (confidenceFilter = option)}
          >
            {t(`duplicates.filter.${option}`)}
          </button>
        {/each}
      </div>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        onclick={() => selectAllButFirst(visibleGroups)}
      >
        {t('duplicates.selectShownAllButFirst')}
      </button>
    {/if}
    <div class="flex-1"></div>
    <label class="chip cursor-pointer select-none">
      <input type="checkbox" class="accent-teal-500" bind:checked={withSidecars} />
      {t('duplicates.withSidecars')}
    </label>
    <button class="btn btn-primary btn-sm" onclick={scan}>{t('duplicates.scan')}</button>
  </header>

  {#if selected.length}
    <div class="animate-in flex items-center gap-2 border-b border-ink-800 bg-ink-850 px-5 py-2.5">
      <span class="text-xs font-medium text-ink-100">
        {plural('duplicates.selected', selected.length)}
      </span>
      <button class="btn btn-ghost btn-sm" onclick={() => (selected = [])}>
        {t('duplicates.deselect')}
      </button>
      <div class="flex-1"></div>
      {#if app.settings?.trash_folder}
        <button class="btn btn-primary btn-sm" onclick={() => moveToTrash([...selected])}>
          {t('trash.moveSelected')}
        </button>
      {/if}
      <button
        class="btn btn-danger btn-sm"
        disabled={planningDeletion}
        onclick={() => prepareDeletion([...selected])}
      >
        {t('duplicates.deleteSelected')}
      </button>
    </div>
  {/if}

  <div class="min-h-0 flex-1 overflow-y-auto p-5">
    {#if loading}
      <Spinner />
    {:else if !groups.length}
      <Empty
        icon="duplicates"
        title={report?.checked_at ? t('duplicates.none.title') : t('duplicates.never.title')}
        hint={report?.checked_at ? t('duplicates.none.hint') : t('duplicates.never.hint')}
      />
    {:else}
      <div class="space-y-6">
        {#each [{ list: certain, key: 'certain' }, { list: probable, key: 'probable' }] as section (section.key)}
          {#if section.list.length}
            <div>
              <p class="label">
                {section.key === 'certain'
                  ? t('duplicates.certain', { count: section.list.length })
                  : t('duplicates.probable', { count: section.list.length })}
              </p>
              <p class="mb-2 text-[11px] leading-relaxed text-ink-500">
                {section.key === 'certain'
                  ? t('duplicates.certainHint')
                  : t('duplicates.probableHint')}
              </p>
              <div class="space-y-2">
                {#each section.list as group (group.key)}
                  {@render groupCard(group)}
                {/each}
              </div>
            </div>
          {/if}
        {/each}
      </div>
    {/if}
  </div>
</div>

{#snippet groupCard(group: DuplicateGroup)}
  <article class="panel p-3">
    <div class="mb-2 flex items-center gap-2">
      <span class="chip text-[10px] font-semibold">
        {t(`duplicates.confidence.${group.confidence}`)}
      </span>
      {#if group.archive_duplicate}
        <span class="chip text-[10px] font-semibold text-teal-300">
          {t('duplicates.archiveDuplicate')}
        </span>
      {/if}
      <div class="flex-1"></div>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        disabled={dismissing.includes(group.key)}
        title={t('duplicates.dismissHint')}
        onclick={() => dismissGroup(group)}
      >
        {t('duplicates.dismiss')}
      </button>
      <button
        type="button"
        class="btn btn-ghost btn-sm"
        disabled={!group.members.slice(1).some(({ id }) => !archiveDuplicateIds.has(id))}
        onclick={() => selectAllButFirst([group])}
      >
        {t('duplicates.selectAllButFirst')}
      </button>
    </div>
    {#if group.archive_duplicate}
      <p class="mb-3 text-[11px] leading-relaxed text-ink-400">
        {t('duplicates.archiveDuplicateHint')}
      </p>
    {/if}
    <div class="grid gap-3" style="grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));">
      {#each group.members as member, index (member.id)}
        <div class="rounded-lg border border-ink-750 bg-ink-850 p-2">
          <div class="relative">
            <button
              type="button"
              class="block w-full"
              onclick={() => inspectImage(member.id)}
              title={t('library.detailsHint')}
            >
              <img
                src={api.thumbnailUrl(member.id, true, member.thumbnail_path)}
                alt=""
                loading="lazy"
                class="max-h-64 w-full rounded bg-ink-950 object-contain"
              />
            </button>
            {#if member.post_bound}
              <span class="pointer-events-none absolute left-2 top-2 chip text-[10px] font-semibold text-teal-300">
                {t('duplicates.postBound')}
              </span>
            {/if}
          </div>
          <p class="mono mt-2 break-all text-[10.5px] leading-relaxed text-ink-300">
            {member.absolute_path}
          </p>
          <p class="mt-1 text-[10.5px] text-ink-500">
            {member.width}×{member.height} · {formatBytes(member.file_size)}
            {member.seed ? ` · seed ${member.seed}` : ''}
          </p>
          <p class="mono truncate text-[10px] text-ink-600" title={member.sha256 ?? ''}>
            sha {member.sha256?.slice(0, 12)}
          </p>
          <div class="mt-2 flex items-center gap-1.5">
            <label class="chip cursor-pointer select-none text-[10px]">
              <input
                type="checkbox"
                class="accent-teal-500"
                disabled={archiveDuplicateIds.has(member.id)}
                checked={selectedSet.has(member.id)}
                onchange={() => toggle(member.id)}
              />
              {t('duplicates.select')}
            </label>
            {#if index === 0}
              <span class="chip text-[10px]">
                {group.first_seen_by === 'generation_time'
                  ? t('duplicates.firstGenerated')
                  : t('duplicates.firstLibrary')}
              </span>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  </article>
{/snippet}

{#if deletion}
  {@const confirmation = deletion}
  {@const targets = confirmation.imageIds}
  {@const targetConfidence = confidenceCounts(targets)}
  <ConfirmDialog
    title={plural('duplicates.confirmTitle', targets.length)}
    danger
    requireKey="ui.confirm.word.delete"
    confirmLabel={t('duplicates.deleteSelected')}
    onCancel={() => (deletion = null)}
    onConfirm={() => remove(targets, confirmation.withSidecars)}
  >
    {#snippet body()}
      <p class="text-ink-400">{plural('duplicates.confirmBody1', confirmation.paths.length)}</p>
      <ul class="mono max-h-48 list-disc space-y-1 overflow-y-auto pl-5 text-xs text-ink-300">
        {#each confirmation.paths as path (path)}
          <li class="break-all">{path}</li>
        {/each}
      </ul>
      {#if !confirmation.withSidecars}
        <p class="text-ink-400">{t('duplicates.confirmNoSidecars')}</p>
      {/if}
      <p class="text-ink-400">{t('duplicates.confirmBody2')}</p>
      <p class="font-medium text-ink-200">
        {t('duplicates.confirmConfidence', targetConfidence)}
      </p>
    {/snippet}
  </ConfirmDialog>
{/if}
