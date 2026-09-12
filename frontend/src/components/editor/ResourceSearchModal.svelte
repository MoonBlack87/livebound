<script lang="ts">
  import { api } from '../../api/client'
  import { plural, t } from '../../i18n/index.svelte'
  import { toast } from '../../state/store.svelte'
  import type { ImageResource, ModelSearchResult, ModelVersionResult } from '../../types/api'
  import Spinner from '../ui/Spinner.svelte'

  let {
    resource,
    onClose,
    onAttached,
  }: { resource: ImageResource; onClose: () => void; onAttached: () => void } = $props()

  let query = $state('')
  let results = $state<ModelSearchResult[]>([])
  let busy = $state(false)
  let hashScope = $state<{ hash: string | null; images: number } | null>(null)

  $effect(() => {
    if (!query) query = resource.model_name || resource.name_in_prompt
  })

  $effect(() => {
    const resourceId = resource.id
    let cancelled = false
    hashScope = null
    api
      .imageResourceHashScope(resourceId)
      .then((scope) => !cancelled && (hashScope = scope))
      .catch((error) => !cancelled && toast('error', (error as Error).message))
    return () => {
      cancelled = true
    }
  })

  async function search() {
    if (!query.trim()) return
    busy = true
    try {
      results = (await api.searchModels(query.trim())).items
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = false
    }
  }

  async function attach(model: ModelSearchResult, version: ModelVersionResult) {
    busy = true
    try {
      const attached = await api.attachImageResource(
        resource.id,
        model as unknown as Record<string, unknown>,
        version as unknown as Record<string, unknown>,
      )
      toast('success', plural('metadata.resourceAttached', attached.reapplied))
      onAttached()
    } catch (error) {
      toast('error', (error as Error).message)
      busy = false
    }
  }
</script>

<div class="fixed inset-0 z-[70] flex items-center justify-center bg-black/70 p-4">
  <section class="panel-raised flex max-h-[80vh] w-full max-w-2xl flex-col">
    <header class="flex items-center gap-2 border-b border-ink-750 p-4">
      <h3 class="flex-1 text-sm font-semibold">{t('metadata.resourceSearch')}</h3>
      {#if busy}<Spinner />{/if}
      <button class="btn btn-ghost btn-sm" onclick={onClose}>✕</button>
    </header>
    <form class="flex gap-2 p-4" onsubmit={(event) => { event.preventDefault(); search() }}>
      <input class="input flex-1" bind:value={query} placeholder={t('metadata.resourceQuery')} />
      <button class="btn btn-primary" disabled={busy}>{t('common.search')}</button>
    </form>
    {#if hashScope}
      <p class="px-4 pb-3 text-xs text-ink-400">
        {#if hashScope.hash === null}
          {t('metadata.resourceScopeNoHash')}
        {:else if hashScope.images === 0}
          {t('metadata.resourceScopeOnlyThis')}
        {:else}
          {plural('metadata.resourceScope', hashScope.images, { hash: hashScope.hash })}
        {/if}
      </p>
    {/if}
    <div class="min-h-0 flex-1 space-y-2 overflow-y-auto px-4 pb-4">
      {#each results as model (model.id)}
        <div class="rounded-lg border border-ink-750 bg-ink-850 p-3">
          <p class="text-sm font-medium">{model.name}</p>
          <p class="text-[11px] text-ink-400">{model.type} · {model.creator ?? '—'}</p>
          <div class="mt-2 flex flex-wrap gap-1.5">
            {#each model.versions as version (version.id)}
              <button class="btn btn-sm" type="button" onclick={() => attach(model, version)}>
                {version.name || `#${version.id}`}
              </button>
            {/each}
          </div>
        </div>
      {/each}
    </div>
  </section>
</div>
