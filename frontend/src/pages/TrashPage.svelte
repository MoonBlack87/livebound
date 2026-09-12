<script lang="ts">
  import { api } from '../api/client'
  import Empty from '../components/ui/Empty.svelte'
  import Spinner from '../components/ui/Spinner.svelte'
  import { formatBytes, formatDateTime } from '../components/ui/format.svelte'
  import { t } from '../i18n/index.svelte'
  import { app, invalidate, toast } from '../state/store.svelte'
  import type { TrashedImage } from '../types/api'

  let items = $state<TrashedImage[]>([])
  let loading = $state(true)

  $effect(() => {
    void app.revision
    loading = true
    api.trash()
      .then((value) => (items = value.items))
      .catch((error) => toast('error', (error as Error).message))
      .finally(() => (loading = false))
  })

  async function restore(image: TrashedImage) {
    try {
      const result = await api.restoreImages([image.id])
      if (result.failed.length) {
        toast('error', `${image.absolute_path}: ${result.failed[0].reason}`)
        return
      }
      invalidate()
      toast('success', t('trash.restored', { path: image.trash_original_path }))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }
</script>

<div class="flex h-full flex-col">
  <header class="flex items-center gap-2 border-b border-ink-800 px-5 py-3">
    <h1 class="text-sm font-semibold">{t('nav.trash')}</h1>
  </header>

  <div class="min-h-0 flex-1 overflow-y-auto p-5">
    {#if loading}
      <Spinner />
    {:else if !items.length}
      <Empty icon="trash" title={t('trash.empty.title')} hint={t('trash.empty.hint')} />
    {:else}
      <div class="space-y-2">
        {#each items as image (image.id)}
          <article class="panel flex flex-wrap items-center gap-3 p-3">
            <img
              src={api.thumbnailUrl(image.id, false, image.thumbnail_path)}
              alt=""
              loading="lazy"
              class="h-24 w-24 shrink-0 rounded bg-ink-950 object-contain"
            />
            <div class="min-w-64 flex-1">
              <p class="text-[10px] text-ink-500">{t('trash.originalPath')}</p>
              <p class="mono break-all text-[11px] text-ink-200">{image.trash_original_path}</p>
              <p class="mt-1 text-[10px] text-ink-500">{t('trash.currentPath')}</p>
              <p class="mono break-all text-[10px] text-ink-500">{image.absolute_path}</p>
              <p class="mt-1 text-[10.5px] text-ink-500">
                {image.width}×{image.height} · {formatBytes(image.file_size)} ·
                {t('trash.movedAt', { when: formatDateTime(image.trashed_at) })}
              </p>
            </div>
            <button class="btn btn-primary btn-sm" onclick={() => restore(image)}>
              {t('trash.restore')}
            </button>
          </article>
        {/each}
      </div>
    {/if}
  </div>
</div>
