<script lang="ts">
  import { api } from '../../api/client'
  import { plural, t } from '../../i18n/index.svelte'
  import { app, invalidate, setActiveJob, toast } from '../../state/store.svelte'
  import ConfirmDialog from '../ui/ConfirmDialog.svelte'
  import Empty from '../ui/Empty.svelte'
  import Panel from '../ui/Panel.svelte'
  import { formatDateTime } from '../ui/format.svelte'
  import DirBrowser from './DirBrowser.svelte'
  import type { SourceDeletionImpact, SourceRoot } from '../../types/api'

  let sources = $state<SourceRoot[]>([])
  let browsing = $state(false)
  let deletion = $state<{ source: SourceRoot; impact: SourceDeletionImpact } | null>(null)

  $effect(() => {
    void app.revision
    api.sources().then((data) => (sources = data.items)).catch(() => undefined)
  })

  async function scanAll() {
    try {
      setActiveJob(await api.scan())
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function requestDeletion(source: SourceRoot) {
    try {
      deletion = { source, impact: await api.sourceDeletionImpact(source.id) }
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function deleteSource() {
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

<Panel title={t('settings.sources.title')}>
  {#snippet actions()}
    <button class="btn btn-sm" onclick={() => (browsing = true)}>{t('settings.sources.add')}</button>
    <button class="btn btn-sm" onclick={scanAll}>{t('settings.sources.scanAll')}</button>
  {/snippet}

  {#if !sources.length}
    <Empty icon="board" title={t('settings.sources.empty')} hint={t('settings.sources.emptyHint')} />
  {:else}
    <div class="space-y-1.5">
      {#each sources as source (source.id)}
        <div
          class="flex items-center gap-2.5 rounded-lg border border-ink-750 bg-ink-850 px-3 py-2"
        >
          <span
            class="h-1.5 w-1.5 shrink-0 rounded-full"
            style="background: {source.exists
              ? 'var(--color-published)'
              : 'var(--color-failed)'};"
            title={source.exists ? t('settings.sources.present') : t('settings.sources.gone')}
          ></span>
          <div class="min-w-0 flex-1">
            <p class="flex items-center gap-1.5 truncate text-xs text-ink-100">
              {source.label || source.path.split('/').filter(Boolean).pop()}
              {#if source.is_archive}
                <span class="chip text-[10px]">{t('settings.sources.archive')}</span>
              {/if}
            </p>
            <p class="mono truncate text-[10.5px] text-ink-400">
              {source.path}{source.last_scanned_at
                ? ` · ${t('settings.sources.lastScan', { when: formatDateTime(source.last_scanned_at) })}`
                : ` · ${t('settings.sources.neverScanned')}`}
            </p>
          </div>
          <label
            class="flex shrink-0 items-center gap-1.5 text-[11px] text-ink-400"
            title={t('settings.watch.sourceHint')}
          >
            <input
              type="checkbox"
              class="h-3.5 w-3.5 accent-[var(--color-accent-500)]"
              checked={source.watch_enabled}
              onchange={async (event) => {
                const on = (event.currentTarget as HTMLInputElement).checked
                try {
                  await api.setSourceWatch(source.id, on)
                  source.watch_enabled = on
                } catch (error) {
                  toast('error', (error as Error).message)
                }
              }}
            />
            {t('settings.watch.label')}
          </label>
          <button class="btn btn-sm" onclick={async () => setActiveJob(await api.scan(source.id))}>
            {t('library.scan')}
          </button>
          {#if !source.is_archive}
            <button
              class="btn btn-danger btn-sm"
              onclick={() => requestDeletion(source)}
            >
              ✕
            </button>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  {#if browsing}
    <DirBrowser onClose={() => (browsing = false)} />
  {/if}

  {#if deletion}
    {@const target = deletion}
    <ConfirmDialog
      title={t('settings.sources.deleteConfirmTitle')}
      danger
      requireKey="ui.confirm.word.delete"
      confirmLabel={t('settings.sources.deleteConfirmAction')}
      onCancel={() => (deletion = null)}
      onConfirm={deleteSource}
    >
      {#snippet body()}
        <p class="mono break-all text-xs text-ink-100">{target.source.path}</p>
        <p class="text-ink-400">{t('settings.sources.deleteConfirmBody')}</p>
        <ul class="list-disc space-y-1 pl-5">
          <li>{plural('settings.sources.deleteImages', target.impact.images)}</li>
          <li>{plural('settings.sources.deleteEdits', target.impact.edits)}</li>
          <li>
            {plural('settings.sources.deleteOverrides', target.impact.resource_overrides)}
          </li>
        </ul>
        <p class="text-ink-400">{t('settings.sources.deleteConfirmFiles')}</p>
      {/snippet}
    </ConfirmDialog>
  {/if}
</Panel>
