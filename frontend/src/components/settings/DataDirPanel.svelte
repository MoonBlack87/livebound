<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { app, invalidate, setActiveJob, toast } from '../../state/store.svelte'
  import ConfirmDialog from '../ui/ConfirmDialog.svelte'
  import Panel from '../ui/Panel.svelte'
  import { formatBytes } from '../ui/format.svelte'
  import DirBrowser from './DirBrowser.svelte'

  /**
   * Where the database, the thumbnail cache and the backups live.
   *
   * All three travel together: the thumbnails are the bulk of it, and leaving
   * them behind would defeat the usual reason for moving in the first place.
   */

  interface DataDirStatus {
    path: string
    default_path: string
    pointer_file: string
    source: 'environment' | 'pointer' | 'default'
    is_default: boolean
    bytes: number
    free_bytes: number
  }

  let status = $state<DataDirStatus | null>(null)
  let browsing = $state(false)
  let target = $state('')

  $effect(() => {
    void app.revision
    api
      .dataDir()
      .then((value) => (status = value as unknown as DataDirStatus))
      .catch(() => undefined)
  })

  async function move() {
    const chosen = target
    target = ''
    try {
      setActiveJob(await api.moveDataDir(chosen))
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }
</script>

<Panel title={t('settings.dataDir.title')}>
  {#snippet actions()}
    <button class="btn btn-sm" onclick={() => (browsing = true)}>
      {t('settings.dataDir.move')}
    </button>
  {/snippet}

  <div class="space-y-3">
    <p class="text-xs leading-relaxed text-ink-300">{t('settings.dataDir.explain')}</p>

    {#if status}
      <div class="rounded-lg border border-ink-750 bg-ink-850 px-3 py-2.5">
        <p class="mono break-all text-[11px] text-ink-100">{status.path}</p>
        <p class="mt-1 text-[11px] text-ink-400">
          {formatBytes(status.bytes)} · {t('settings.dataDir.free', {
            size: formatBytes(status.free_bytes),
          })}{status.is_default ? ` · ${t('settings.dataDir.isDefault')}` : ''}
        </p>
      </div>
      <p class="text-[10px] text-ink-500">
        {#if status.source === 'environment'}
          {t('settings.dataDir.environment')}
        {:else if status.source === 'pointer'}
          {t('settings.dataDir.pointer', { path: status.pointer_file })}
        {:else}
          {t('settings.dataDir.default')}
        {/if}
      </p>
    {/if}
  </div>

  {#if browsing}
    <DirBrowser
      title={t('settings.dataDir.pickTitle')}
      onPick={(path) => (target = path)}
      onClose={() => (browsing = false)}
    />
  {/if}

  {#if target}
    <ConfirmDialog
      title={t('settings.dataDir.confirmTitle')}
      requireKey="ui.confirm.word.move"
      confirmLabel={t('settings.dataDir.move')}
      onCancel={() => (target = '')}
      onConfirm={move}
    >
      {#snippet body()}
        <p class="mono break-all text-xs">{target}</p>
        <p class="text-ink-400">{t('settings.dataDir.confirmBody1')}</p>
        <p class="text-ink-400">{t('settings.dataDir.confirmBody2')}</p>
      {/snippet}
    </ConfirmDialog>
  {/if}
</Panel>
