<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { app, invalidate, toast } from '../../state/store.svelte'
  import ConfirmDialog from '../ui/ConfirmDialog.svelte'
  import Panel from '../ui/Panel.svelte'
  import Spinner from '../ui/Spinner.svelte'
  import { formatDateTime } from '../ui/format.svelte'
  import ArchiveBackup from './ArchiveBackup.svelte'
  import type { BackupInfo } from '../../types/api'

  /**
   * Database backups.
   *
   * The database is the entire state: posts, plans, schedules, the duplicate
   * history and the settings. Images are not part of it - they live in the
   * source folders and are never overwritten.
   */

  let items = $state<BackupInfo[]>([])
  let folder = $state('')
  let busy = $state(false)
  let restoring = $state<BackupInfo | null>(null)
  let deleting = $state<BackupInfo | null>(null)

  function reload() {
    api
      .backups()
      .then((data) => {
        items = data.items
        folder = data.folder
      })
      .catch(() => undefined)
  }

  $effect(() => {
    void app.revision
    reload()
  })

  async function create() {
    busy = true
    try {
      const created = await api.createBackup()
      toast('success', t('settings.backup.created', { name: created.name }))
      reload()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = false
    }
  }

  async function restore(item: BackupInfo) {
    restoring = null
    try {
      const result = await api.restoreBackup(item.name)
      toast('success', t('settings.backup.restored', { name: String(result.safety_copy) }))
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function remove(item: BackupInfo) {
    deleting = null
    try {
      await api.deleteBackup(item.name)
      toast('success', t('settings.backup.deleted', { name: item.name }))
      reload()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }
</script>

<Panel title={t('settings.backup.title')}>
  {#snippet actions()}
    <button class="btn btn-sm" disabled={busy} onclick={create}>
      {#if busy}<Spinner />{:else}{t('settings.backup.now')}{/if}
    </button>
  {/snippet}

  <div class="space-y-3">
    <p class="text-xs leading-relaxed text-ink-300">{t('settings.backup.explain')}</p>

    <p
      class="rounded-lg px-3 py-2 text-[11px] leading-relaxed"
      style="background: color-mix(in srgb, var(--color-ready) 10%, transparent);
             color: var(--color-ink-200);"
    >
      {t('settings.backup.connectionWarning')}
    </p>

    {#if !items.length}
      <p class="text-xs text-ink-500">{t('settings.backup.empty')}</p>
    {:else}
      <div class="space-y-1">
        {#each items.slice(0, 8) as item (item.name)}
          <div
            class="flex items-center gap-2.5 rounded-lg border border-ink-750 bg-ink-850 px-3 py-1.5"
          >
            <span class="chip shrink-0 text-[10px]">
              {item.automatic ? t('settings.backup.auto') : t('settings.backup.manual')}
            </span>
            <div class="min-w-0 flex-1">
              <p class="mono truncate text-[11px] text-ink-200">
                {formatDateTime(item.created_at)}
              </p>
              <p class="text-[10px] text-ink-500">
                {(item.size / 1024 ** 2).toFixed(1)} MB · {item.contents?.posts ?? '?'}
                {t('settings.backup.posts')} · {item.contents?.image_usage ?? '?'}
                {t('settings.backup.usages')}
              </p>
            </div>
            <button class="btn btn-sm" onclick={() => (restoring = item)}>
              {t('settings.backup.restore')}
            </button>
            <button
              class="btn btn-danger btn-sm"
              onclick={() => (deleting = item)}
            >
              ✕
            </button>
          </div>
        {/each}
      </div>
    {/if}

    {#if folder}
      <p class="mono text-[10px] text-ink-500">{folder}</p>
    {/if}

    <ArchiveBackup />
  </div>

  {#if restoring}
    {@const target = restoring}
    <ConfirmDialog
      title={t('settings.backup.restoreTitle')}
      danger
      requireKey="ui.confirm.word.restore"
      confirmLabel={t('settings.backup.restore')}
      onCancel={() => (restoring = null)}
      onConfirm={() => restore(target)}
    >
      {#snippet body()}
        <p>
          {t('settings.backup.restoreBody1', {
            when: formatDateTime(target.created_at),
            posts: target.contents?.posts ?? '?',
          })}
        </p>
        <p class="text-ink-400">{t('settings.backup.restoreBody2')}</p>
      {/snippet}
    </ConfirmDialog>
  {/if}

  {#if deleting}
    {@const target = deleting}
    <ConfirmDialog
      title={t('settings.backup.deleteTitle')}
      danger
      requireKey="ui.confirm.word.delete"
      confirmLabel={t('settings.backup.delete')}
      onCancel={() => (deleting = null)}
      onConfirm={() => remove(target)}
    >
      {#snippet body()}
        <p>
          {t('settings.backup.deleteBody', {
            name: target.name,
            when: formatDateTime(target.created_at),
            folder,
          })}
        </p>
      {/snippet}
    </ConfirmDialog>
  {/if}
</Panel>
