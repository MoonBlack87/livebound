<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { app, invalidate, setActiveJob, toast } from '../../state/store.svelte'
  import type { ModelRoot } from '../../types/api'
  import Empty from '../ui/Empty.svelte'
  import Panel from '../ui/Panel.svelte'
  import { formatDateTime } from '../ui/format.svelte'
  import DirBrowser from './DirBrowser.svelte'

  let roots = $state<ModelRoot[]>([])
  let recognized = $state(0)
  let unrecognized = $state(0)
  let unqueried = $state(0)
  let browsing = $state(false)

  $effect(() => {
    void app.revision
    api
      .modelRoots()
      .then((data) => {
        roots = data.items
        recognized = data.recognized
        unrecognized = data.unrecognized
        unqueried = data.unqueried
      })
      .catch(() => undefined)
  })

  async function addRoot(path: string) {
    try {
      await api.addModelRoot({ path })
      invalidate()
      toast('success', t('dirs.modelAdded'))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function hash(rootId?: number) {
    try {
      setActiveJob(await api.hashModelRoots(rootId))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function remove(rootId: number) {
    try {
      await api.deleteModelRoot(rootId)
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }
</script>

<Panel title={t('settings.modelRoots.title')}>
  {#snippet actions()}
    <button class="btn btn-sm" onclick={() => (browsing = true)}>
      {t('settings.modelRoots.add')}
    </button>
    <button class="btn btn-sm" onclick={() => hash()}>{t('settings.modelRoots.hashAll')}</button>
  {/snippet}

  <p class="mb-2 text-[11px] text-ink-400">
    {t('settings.modelRoots.identity', { recognized, unrecognized, unqueried })}
  </p>

  {#if !roots.length}
    <Empty
      icon="models"
      title={t('settings.modelRoots.empty')}
      hint={t('settings.modelRoots.emptyHint')}
    />
  {:else}
    <div class="space-y-1.5">
      {#each roots as root (root.id)}
        <div
          class="flex items-center gap-2.5 rounded-lg border border-ink-750 bg-ink-850 px-3 py-2"
        >
          <span
            class="h-1.5 w-1.5 shrink-0 rounded-full"
            style="background: {root.exists
              ? 'var(--color-published)'
              : 'var(--color-failed)'};"
            title={root.exists
              ? t('settings.modelRoots.present')
              : t('settings.modelRoots.gone')}
          ></span>
          <div class="min-w-0 flex-1">
            <p class="truncate text-xs text-ink-100">
              {root.label || root.path.split('/').filter(Boolean).pop()}
            </p>
            <p class="mono truncate text-[10.5px] text-ink-400">
              {root.path}{root.last_hashed_at
                ? ` · ${t('settings.modelRoots.lastHash', { when: formatDateTime(root.last_hashed_at) })}`
                : ` · ${t('settings.modelRoots.neverHashed')}`}
            </p>
          </div>
          <label
            class="flex shrink-0 items-center gap-1.5 text-[11px] text-ink-400"
            title={t('settings.watch.modelHint')}
          >
            <input
              type="checkbox"
              class="h-3.5 w-3.5 accent-[var(--color-accent-500)]"
              checked={root.watch_enabled}
              onchange={async (event) => {
                const on = (event.currentTarget as HTMLInputElement).checked
                try {
                  await api.setModelRootWatch(root.id, on)
                  root.watch_enabled = on
                } catch (error) {
                  toast('error', (error as Error).message)
                }
              }}
            />
            {t('settings.watch.label')}
          </label>
          <button class="btn btn-sm" onclick={() => hash(root.id)}>
            {t('settings.modelRoots.hash')}
          </button>
          <button class="btn btn-danger btn-sm" onclick={() => remove(root.id)}>✕</button>
        </div>
      {/each}
    </div>
  {/if}

  {#if browsing}
    <DirBrowser
      title={t('dirs.titleModel')}
      onPick={(path) => void addRoot(path)}
      onClose={() => (browsing = false)}
    />
  {/if}
</Panel>
