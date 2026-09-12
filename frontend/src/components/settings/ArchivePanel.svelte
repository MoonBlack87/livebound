<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { app, invalidate, toast } from '../../state/store.svelte'
  import ConfirmDialog from '../ui/ConfirmDialog.svelte'
  import Panel from '../ui/Panel.svelte'
  import Spinner from '../ui/Spinner.svelte'
  import ArchiveMoveNow from '../ArchiveMoveNow.svelte'
  import DirBrowser from './DirBrowser.svelte'
  import type { ArchiveStatus, SourceDeletionImpact, SourceRoot } from '../../types/api'

  /**
   * Moving already-published images out of the working folders.
   *
   * Manual on purpose, and stated as such: this moves real files and cannot be
   * undone. Nothing here runs as a side effect of anything else.
   */

  type StatusState = 'loading' | 'resolved' | 'failed'

  let statusState = $state<StatusState>('loading')
  let status = $state<ArchiveStatus | null>(null)
  let statusRequest = 0
  let browsing = $state(false)
  let deletion = $state<{ source: SourceRoot; impact: SourceDeletionImpact } | null>(null)


  function loadStatus() {
    const request = ++statusRequest
    statusState = 'loading'
    status = null
    api
      .archiveStatus()
      .then((value) => {
        if (request !== statusRequest) return
        status = value
        statusState = 'resolved'
      })
      .catch(() => {
        if (request !== statusRequest) return
        statusState = 'failed'
      })
  }

  $effect(() => {
    void app.revision
    loadStatus()
    return () => {
      statusRequest += 1
    }
  })

  async function requestDeletion() {
    const source = status?.archive
    if (!source) return
    try {
      deletion = { source, impact: await api.sourceDeletionImpact(source.id) }
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function deleteArchiveFolder() {
    if (!deletion) return
    try {
      await api.deleteSource(deletion.source.id)
      deletion = null
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }
</script>

<Panel title={t('settings.archive.title')}>
  {#snippet actions()}
    {#if statusState === 'resolved' && status && !status.configured}
      <button class="btn btn-sm" onclick={() => (browsing = true)}>
        {t('settings.archive.choose')}
      </button>
    {:else if statusState === 'resolved' && status?.archive}
      <button class="btn btn-danger btn-sm" onclick={requestDeletion}>
        {t('settings.archive.removeFolder')}
      </button>
    {/if}
  {/snippet}

  <div class="space-y-3">
    <p class="text-xs leading-relaxed text-ink-300">{t('settings.archive.explain')}</p>

    {#if statusState === 'loading'}
      <div class="flex items-center gap-2 text-xs text-ink-400" aria-live="polite">
        <Spinner />
        <span>{t('settings.archive.loading')}</span>
      </div>
    {:else if statusState === 'failed'}
      <div class="flex flex-wrap items-center gap-2" role="alert">
        <p class="text-xs" style="color: var(--color-failed);">
          {t('settings.archive.loadFailed')}
        </p>
        <button class="btn btn-sm" onclick={loadStatus}>{t('settings.archive.retry')}</button>
      </div>
    {:else if status && !status.configured}
      <p class="text-xs text-ink-400">{t('settings.archive.notConfigured')}</p>
    {:else if status}
      <div class="flex flex-wrap items-center gap-2">
        <span class="chip mono text-[11px]">{status.archive?.path}</span>
        {#if !status.blocked}
          <span class="text-xs text-ink-400">
            {#if status.candidates}
              {t('settings.archive.due', {
                images: status.candidates,
                posts: status.posts ?? 0,
              })}
            {:else}
              {t('settings.archive.noneMarked')}
            {/if}
          </span>
        {/if}
        <div class="flex-1"></div>
        <ArchiveMoveNow given={status} />
      </div>

      {#if status.blocked}
        <p class="text-xs" style="color: var(--color-failed);">{status.blocked}</p>
      {:else if status.unresolved}
        <p class="text-[11px]" style="color: var(--color-ready);">
          {t('settings.archive.unresolved', { count: status.unresolved ?? 0 })}
        </p>
      {/if}

      {#if status.duplicates?.length}
        <div class="rounded-lg border border-ink-750 bg-ink-850 p-2.5">
          <p class="text-xs font-medium text-ink-100">
            {t('settings.archive.dupesFound', { count: status.duplicates.length })}
          </p>
          <p class="mt-0.5 text-[11px] text-ink-400">{t('settings.archive.dupesExplain')}</p>
          <ul class="mt-1.5 space-y-1">
            {#each status.duplicates.slice(0, 5) as group (group.remote_image_id)}
              <li class="text-[10.5px] text-ink-400">
                <span class="mono">_{group.remote_image_id}</span>:
                {group.paths.map((path) => path.split('/').pop()).join(', ')}
              </li>
            {/each}
          </ul>
        </div>
      {/if}
    {/if}
  </div>

  {#if browsing}
    <DirBrowser asArchive onClose={() => (browsing = false)} />
  {/if}


  {#if deletion}
    {@const target = deletion}
    <ConfirmDialog
      title={t('settings.archive.removeTitle')}
      danger
      requireKey="ui.confirm.word.delete"
      confirmLabel={t('settings.archive.removeFolder')}
      onCancel={() => (deletion = null)}
      onConfirm={deleteArchiveFolder}
    >
      {#snippet body()}
        <p class="mono break-all text-xs text-ink-100">{target.source.path}</p>
        <p class="text-ink-400">{t('settings.archive.removeBody')}</p>
        <ul class="list-disc space-y-1 pl-5">
          <li>{t('settings.archive.removeImages', { count: target.impact.images })}</li>
          <li>{t('settings.archive.removeEdits', { count: target.impact.edits })}</li>
          <li>{t('settings.archive.removeOverrides', { count: target.impact.resource_overrides })}</li>
        </ul>
        <p class="text-ink-400">{t('settings.archive.removeFiles')}</p>
      {/snippet}
    </ConfirmDialog>
  {/if}
</Panel>
