<script lang="ts">
  import { api } from '../api/client'
  import { t } from '../i18n/index.svelte'
  import Spinner from './ui/Spinner.svelte'
  import type { BindingSuggestion, ModelSearchResult, Post } from '../types/api'

  /**
   * Which model page the post appears on.
   *
   * A post binds to exactly one model version - a LoRA *or* a checkpoint, never
   * both, because CivitAI stores a single `modelVersionId`. And it can only be
   * set when the post is created: `post.update` has no such field, so a later
   * change is accepted and silently ignored. Once bound, this control locks and
   * offers the rebuild path instead of pretending an edit would work.
   */

  let {
    post,
    disabled = false,
    onChange,
    onRebuild,
  }: {
    post: Post
    disabled?: boolean
    onChange: (patch: Record<string, unknown>) => void
    onRebuild: () => void
  } = $props()

  let query = $state('')
  let results = $state<ModelSearchResult[]>([])
  let searching = $state(false)
  let open = $state(false)

  const locked = $derived(post.bound_model_version_id != null)
  const suggestions = $derived<BindingSuggestion[]>(post.suggested_bindings ?? [])

  $effect(() => {
    if (query.trim().length < 2) {
      results = []
      return
    }
    let cancelled = false
    searching = true
    const timer = setTimeout(() => {
      api
        .searchModels(query)
        .then((data) => !cancelled && (results = data.items))
        .catch(() => !cancelled && (results = []))
        .finally(() => !cancelled && (searching = false))
    }, 380)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  })

  function select(versionId: number | null, modelName?: string, versionName?: string) {
    if (disabled) return
    if (versionId === null) onChange({ clear_binding: true })
    else onChange({ model_version_id: versionId, model_name: modelName, version_name: versionName })
    open = false
    query = ''
  }
</script>

<div>
  <span class="label">{t('binding.label')}</span>

  {#if post.model_version_id}
    <div class="flex items-center gap-2 rounded-lg border border-ink-700 bg-ink-850 px-3 py-2">
      <div class="min-w-0 flex-1">
        <p class="truncate text-xs text-ink-100">
          {post.model_name ?? t('binding.version', { id: post.model_version_id })}
        </p>
        <p class="mono truncate text-[10px] text-ink-400">
          {post.version_name ? `${post.version_name} · ` : ''}mv {post.model_version_id}
        </p>
      </div>
      {#if locked}
        <span class="chip text-[10px]" title={t('binding.lockedHint')}>🔒 {t('binding.locked')}</span>
      {:else}
        <button class="btn btn-ghost btn-sm" {disabled} onclick={() => select(null)}>
          {t('binding.clear')}
        </button>
      {/if}
    </div>
  {:else}
    <button class="btn w-full justify-center" disabled={locked || disabled} onclick={() => (open = !open)}>
      {open ? t('binding.closePicker') : t('binding.choose')}
    </button>
  {/if}

  {#if locked}
    <p class="mt-1.5 text-xs leading-relaxed text-ink-400">
      {t('binding.lockedExplain')}
      <button class="underline decoration-dotted" {disabled} onclick={onRebuild}>
        {t('binding.rebuild')}
      </button>
      {t('binding.rebuildExplain')}
    </p>
  {/if}

  {#if open && !locked}
    <div class="mt-2 space-y-2 rounded-lg border border-ink-700 bg-ink-950 p-2.5">
      {#if suggestions.length}
        <div>
          <p class="label mb-1.5">{t('binding.detected')}</p>
          <div class="space-y-1">
            {#each suggestions as item (item.model_version_id)}
              <button
                class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-ink-850"
                {disabled}
                onclick={() =>
                  select(
                    item.model_version_id,
                    item.model_name ?? undefined,
                    item.version_name ?? undefined,
                  )}
              >
                <span class="chip shrink-0 text-[10px]">
                  {item.resource_type === 'checkpoint' ? 'CKPT' : 'LoRA'}
                </span>
                <span class="min-w-0 flex-1 truncate text-xs text-ink-200">{item.model_name}</span>
                <span class="mono shrink-0 text-[10px] text-ink-500">{item.image_count}×</span>
              </button>
            {/each}
          </div>
        </div>
      {/if}

      <div>
        <p class="label mb-1.5">{t('binding.search')}</p>
        <input class="input" {disabled} placeholder={t('binding.searchPlaceholder')} bind:value={query} />
        {#if searching}
          <p class="mt-2 flex items-center gap-2 text-xs text-ink-400">
            <Spinner />
            {t('binding.searching')}
          </p>
        {/if}
        <div class="mt-1.5 max-h-56 space-y-1 overflow-y-auto">
          {#each results as model (model.id)}
            <div>
              <p class="px-2 pt-1.5 text-[11px] font-medium text-ink-300">
                {model.name}
                <span class="text-ink-500">· {model.type} · {model.creator}</span>
              </p>
              {#each (model.versions ?? []).slice(0, 4) as version (version.id)}
                <button
                  class="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left hover:bg-ink-850"
                  {disabled}
                  onclick={() => select(version.id, model.name ?? undefined, version.name ?? undefined)}
                >
                  <span class="min-w-0 flex-1 truncate text-xs text-ink-200">{version.name}</span>
                  <span class="mono shrink-0 text-[10px] text-ink-500">{version.base_model}</span>
                </button>
              {/each}
            </div>
          {/each}
        </div>
      </div>
    </div>
  {/if}
</div>
