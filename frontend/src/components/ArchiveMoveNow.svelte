<script lang="ts">
  import { api } from '../api/client'
  import { t } from '../i18n/index.svelte'
  import { app, invalidate, setActiveJob, toast } from '../state/store.svelte'
  import ConfirmDialog from './ui/ConfirmDialog.svelte'
  import Spinner from './ui/Spinner.svelte'
  import type { ArchivePlanEntry, ArchiveStatus } from '../types/api'

  /**
   * Moving already-published images out of the working folders, as one control.
   *
   * It lives in its own component because it is offered in two places - the
   * settings, where the archive folder is configured, and the archive view,
   * where somebody stands when they want it. One implementation: the same
   * confirmation, the same file list, the same job.
   *
   * Manual on purpose: this moves real files and cannot be undone. Nothing here
   * runs as a side effect of anything else.
   */

  type StatusState = 'loading' | 'resolved' | 'failed'

  /**
   * The archive status, when the caller already has it. `archiveStatus()` walks
   * the library and the file system, so a settings page that fetches it for its
   * own display must not make this control fetch it a second time - on Windows
   * that walk is one of the expensive paths. Without it, this fetches its own.
   */
  let { given = null }: { given?: ArchiveStatus | null } = $props()

  let ownState = $state<StatusState>('loading')
  let own = $state<ArchiveStatus | null>(null)
  const statusState = $derived(given ? 'resolved' : ownState)
  const status = $derived(given ?? own)
  let statusRequest = 0
  let confirming = $state(false)

  /** The plan is only fetched when it is about to be read: it walks the whole
   *  library, so loading it on every settings render would cost for nothing. */
  const PREVIEW_LIMIT = 500
  let previewState = $state<StatusState>('loading')
  let preview = $state<ArchivePlanEntry[]>([])

  /** One entry per archive sub-folder (`yyyy-mm-dd__POSTID`), files inside.
   *  The folder is shown relative to the archive root, which the sentence above
   *  the list already names in full. */
  const previewFolders = $derived.by(() => {
    const root = status?.archive?.path ?? ''
    const folders = new Map<string, { folder: string; files: ArchivePlanEntry[] }>()
    for (const entry of preview) {
      const full = entry.target.slice(0, entry.target.lastIndexOf('/'))
      const folder = full.startsWith(root) ? full.slice(root.length).replace(/^\//, '') : full
      const known = folders.get(folder)
      if (known) known.files.push(entry)
      else folders.set(folder, { folder, files: [entry] })
    }
    return [...folders.values()]
  })

  async function openConfirm() {
    confirming = true
    previewState = 'loading'
    preview = []
    try {
      preview = (await api.archivePlan(PREVIEW_LIMIT)).items
      previewState = 'resolved'
    } catch {
      previewState = 'failed'
    }
  }

  function loadStatus() {
    if (given) return
    const request = ++statusRequest
    ownState = 'loading'
    own = null
    api
      .archiveStatus()
      .then((value) => {
        if (request !== statusRequest) return
        own = value
        ownState = 'resolved'
      })
      .catch(() => {
        if (request !== statusRequest) return
        ownState = 'failed'
      })
  }

  $effect(() => {
    void app.revision
    loadStatus()
    return () => {
      statusRequest += 1
    }
  })

  async function move() {
    confirming = false
    try {
      setActiveJob(await api.runArchive())
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }
</script>

{#if statusState === 'resolved' && status && !status.blocked}
  <button class="btn btn-danger btn-sm" disabled={!status.candidates} onclick={openConfirm}>
    {t('settings.archive.moveNow')}
  </button>
{/if}

  {#if confirming}
    <ConfirmDialog
      title={t('settings.archive.confirmTitle')}
      danger
      requireKey="ui.confirm.word.move"
      confirmLabel={t('settings.archive.confirmAction', { count: status?.candidates ?? 0 })}
      onCancel={() => (confirming = false)}
      onConfirm={move}
    >
      {#snippet body()}
        <p>
          {t('settings.archive.confirmBody1', {
            count: status?.candidates ?? 0,
            path: status?.archive?.path ?? '',
          })}
        </p>

        <!-- The list before the press: this moves originals and is not undone
             by a button, so the maintainer sees the files, not only a count. -->
        {#if previewState === 'loading'}
          <div class="flex items-center gap-2 text-xs text-ink-400" aria-live="polite">
            <Spinner />
            <span>{t('settings.archive.previewLoading')}</span>
          </div>
        {:else if previewState === 'failed'}
          <p class="text-xs" style="color: var(--color-ready);">
            {t('settings.archive.previewFailed')}
          </p>
        {:else}
          <div class="max-h-56 space-y-2 overflow-y-auto">
            {#each previewFolders as group (group.folder)}
              <div>
                <p class="mono text-[11px] text-ink-200 break-all">{group.folder}/</p>
                <ul class="mono list-disc space-y-0.5 pl-5 text-[11px] text-ink-400">
                  {#each group.files as file (file.image_id)}
                    <li class="break-all" title={file.source_path}>{file.name}</li>
                  {/each}
                </ul>
              </div>
            {/each}
          </div>
          {#if (status?.candidates ?? 0) > preview.length}
            <p class="text-[11px]" style="color: var(--color-ready);">
              {t('settings.archive.previewTruncated', {
                shown: preview.length,
                total: status?.candidates ?? 0,
              })}
            </p>
          {/if}
        {/if}

        <p class="text-ink-400">{t('settings.archive.confirmBody2')}</p>
      {/snippet}
    </ConfirmDialog>
  {/if}
