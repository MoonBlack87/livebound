<script lang="ts">
  import { onDestroy } from 'svelte'
  import { api } from '../api/client'
  import { coded, t } from '../i18n/index.svelte'
  import { modelUrl } from '../lib/civitai'
  import { app, inspectImage, invalidate, toast } from '../state/store.svelte'
  import type { ImageDetail, ImageEditState, ImageResource, ModelSuggestion } from '../types/api'
  import DuplicateNotice from './drawer/DuplicateNotice.svelte'
  import ResourceSearchModal from './editor/ResourceSearchModal.svelte'
  import CopyButton from './ui/CopyButton.svelte'
  import Spinner from './ui/Spinner.svelte'
  import { formatBytes } from './ui/format.svelte'

  let { imageId }: { imageId: number } = $props()
  let detail = $state<ImageDetail | null>(null)
  let editState = $state<ImageEditState | null>(null)
  let busy = $state(false)
  let showRaw = $state(false)
  let newField = $state('')
  let newValue = $state('')
  let resourceName = $state('')
  let resourceType = $state<'checkpoint' | 'lora' | 'embedding'>('lora')
  let suggestions = $state<ModelSuggestion[]>([])
  let suggestionTimer: ReturnType<typeof setTimeout> | undefined
  let suggestionRequest = 0
  let searching = $state<ImageResource | null>(null)
  let metadataEditImageId: number | null = null

  onDestroy(() => clearTimeout(suggestionTimer))

  // metadata/resources.py derives these known types; only LoRAs normally have a weight.
  const KNOWN_RESOURCE_TYPES = new Set(['checkpoint', 'lora', 'embedding', 'upscaler'])
  const WEIGHTABLE_RESOURCE_TYPES = new Set(['lora'])

  async function reload() {
    try {
      const [nextDetail, nextState] = await Promise.all([api.image(imageId), api.imageEdit(imageId)])
      detail = nextDetail
      editState = nextState
    } catch (error) {
      toast('error', (error as Error).message)
      inspectImage(null)
    }
  }

  $effect(() => {
    void imageId
    detail = null
    editState = null
    reload()
  })

  function payload() {
    const edit = editState?.edit
    return {
      draft: {
        prompt: edit?.draft.prompt ?? null,
        negative_prompt: edit?.draft.negative_prompt ?? null,
        fields: { ...(edit?.draft.fields ?? {}) },
      },
      touched: [...(edit?.touched ?? [])],
      deleted: [...(edit?.deleted ?? [])],
    }
  }

  function beginMetadataEdit() {
    metadataEditImageId = imageId
  }

  async function setValue(key: string, value: unknown, id: number | null = imageId) {
    if (!editState) return
    const next = payload()
    if (key === 'prompt' || key === 'negative_prompt') next.draft[key] = String(value)
    else next.draft.fields[key] = value
    next.touched = [...new Set([...next.touched, key])]
    next.deleted = next.deleted.filter((item) => item !== key)
    await save(next, id)
  }

  async function deleteValue(key: string) {
    const next = payload()
    next.touched = next.touched.filter((item) => item !== key)
    next.deleted = [...new Set([...next.deleted, key])]
    await save(next)
  }

  async function restoreValue(key: string) {
    const next = payload()
    next.touched = next.touched.filter((item) => item !== key)
    next.deleted = next.deleted.filter((item) => item !== key)
    delete next.draft.fields[key]
    await save(next)
  }

  async function save(next: Record<string, unknown>, id: number | null = imageId) {
    if (id == null) return
    busy = true
    try {
      editState = await api.saveImageEdit(id, next)
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = false
    }
  }

  async function resetAll() {
    await save({ draft: {}, touched: [], deleted: [] })
  }

  // The list is asked for as the name is typed, because it comes from two
  // places at once: files in the model folders and identities already in the
  // hash map. The second is why a model that was only ever assigned through
  // Find model, and never downloaded, can still be found by its name.
  function loadSuggestions() {
    clearTimeout(suggestionTimer)
    suggestions = []
    const request = ++suggestionRequest
    const query = resourceName.trim()
    if (query.length < 2) {
      return
    }
    suggestionTimer = setTimeout(async () => {
      try {
        const loaded = (await api.modelSuggestions(query)).items
        if (request === suggestionRequest) suggestions = loaded
      } catch {
        // A failed lookup leaves the field a plain text input, which still works.
        if (request === suggestionRequest) suggestions = []
      }
    }, 200)
  }

  function asciiFold(value: string) {
    return value.replace(/[A-Z]/g, (character) => character.toLowerCase())
  }

  // A suggestion is only taken as chosen when the typed name matches exactly one
  // of them. Two files with the same stem are two different models, and guessing
  // which one was meant would attribute a picture to the wrong one.
  const chosenSuggestion = $derived.by(() => {
    const typed = asciiFold(resourceName.trim())
    if (!typed) return null
    const hits = suggestions.filter((item) => asciiFold(item.name) === typed)
    return hits.length === 1 ? hits[0] : null
  })

  async function addResource() {
    if (!resourceName.trim()) return
    const picked = chosenSuggestion
    await resourceAction(() =>
      api.addImageResource(imageId, {
        resource_type: resourceType,
        name: resourceName.trim(),
        hash: picked?.hash ?? null,
      }),
    )
    resourceName = ''
    suggestions = []
    ++suggestionRequest
    clearTimeout(suggestionTimer)
  }

  async function resourceAction(action: () => Promise<unknown>) {
    busy = true
    try {
      await action()
      await reload()
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = false
    }
  }

  function inputValue(value: unknown): string {
    if (value == null) return ''
    return typeof value === 'string' ? value : JSON.stringify(value)
  }

  function parsedValue(value: string, current: unknown): unknown {
    if (typeof current === 'number' && value.trim() !== '' && Number.isFinite(Number(value))) {
      return Number(value)
    }
    if (typeof current === 'boolean' && /^(true|false)$/i.test(value.trim())) {
      return value.trim().toLowerCase() === 'true'
    }
    if (typeof current === 'object' && current !== null) {
      try {
        return JSON.parse(value)
      } catch {
        return value
      }
    }
    return value
  }

  function fieldIsWide(item: { value: unknown; derived: boolean }): boolean {
    return item.derived || inputValue(item.value).length > 60
  }

  function hiddenByMetadataSettings(item: { key: string; original: unknown; value: unknown; changed: boolean }): boolean {
    if (item.changed || item.original == null || item.value != null) return false
    return (app.settings?.metadata_excluded_fields ?? []).some((pattern) =>
      pattern.endsWith('*') ? item.key.startsWith(pattern.slice(0, -1)) : item.key === pattern,
    )
  }

  function originalResource(resource: ImageResource) {
    if (resource.added_by_user) return undefined
    const originals =
      editState?.original_resources.filter(
        (item) => item.resource_type === resource.resource_type,
      ) ?? []
    return originals.find(
      (item) =>
        item.name_in_prompt === resource.name_in_prompt ||
        (Boolean(item.hash) && item.hash === resource.hash) ||
        (Boolean(item.model_version_id) && item.model_version_id === resource.model_version_id),
    ) ?? (originals.length === 1 ? originals[0] : undefined)
  }

  function showsWeight(resource: ImageResource): boolean {
    return (
      resource.weight !== null ||
      !KNOWN_RESOURCE_TYPES.has(resource.resource_type) ||
      WEIGHTABLE_RESOURCE_TYPES.has(resource.resource_type)
    )
  }

  function onKey(event: KeyboardEvent) {
    if (event.key === 'Escape' && !searching) inspectImage(null)
  }
</script>

<svelte:window onkeydown={onKey} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="fixed inset-0 z-50 flex justify-end bg-black/60 backdrop-blur-sm"
  onmousedown={(event) => event.target === event.currentTarget && inspectImage(null)}
>
  <aside class="animate-in flex h-full w-full max-w-3xl flex-col border-l border-ink-700 bg-ink-900">
    <header class="flex items-center gap-3 border-b border-ink-750 px-5 py-3">
      <h2 class="min-w-0 flex-1 truncate text-sm font-semibold">
        {detail ? detail.relative_path.split('/').pop() : t('drawer.loading')}
      </h2>
      {#if editState?.diff.changed}<span class="chip">{t('metadata.edited')}</span>{/if}
      {#if busy}<Spinner />{/if}
      {#if editState?.diff.changed}
        <button class="btn btn-sm" onclick={resetAll}>{t('metadata.resetAll')}</button>
      {/if}
      <button class="btn btn-ghost btn-sm" onclick={() => inspectImage(null)}>✕</button>
    </header>

    {#if !detail || !editState}
      <div class="flex flex-1 items-center justify-center"><Spinner /></div>
    {:else}
      <div class="min-h-0 flex-1 overflow-y-auto">
        {#if detail.is_missing}
          <div
            class="flex min-h-48 w-full flex-col items-center justify-center gap-2 bg-ink-950 px-4 text-center"
          >
            <span class="text-3xl text-amber-300" aria-hidden="true">!</span>
            <p class="text-sm text-ink-300">{t('library.sourceNotFound')}</p>
            <p class="text-xs text-ink-500">{t('library.sourceUnavailable')}</p>
          </div>
        {:else}
          <img src={api.previewUrl(detail.id)} alt="" class="max-h-[38vh] w-full bg-ink-950 object-contain" />
        {/if}
        <div class="space-y-4 p-4">
          <div>
            <div class="flex items-center justify-between">
              <p class="label mb-0">{t('drawer.path')}</p>
              <CopyButton text={detail.absolute_path} />
            </div>
            <p class="mono mt-1 break-all text-[11px] text-ink-400">{detail.absolute_path}</p>
            <p class="mt-1 text-[11px] text-ink-500">
              {detail.source_label || t('drawer.unknownSource')} · {detail.width}×{detail.height} ·
              {formatBytes(detail.file_size)} · sha {detail.sha256?.slice(0, 12)}
            </p>
          </div>

          {#if detail.duplicates.has_exact || detail.duplicates.has_similar}
            <DuplicateNotice {detail} />
          {/if}

          <div>
            <div class="mb-1 flex items-center justify-between">
              <p class="label mb-0">{t('drawer.prompt')}</p>
              {#if editState.diff.prompts.prompt.changed}
                <button class="btn btn-ghost btn-sm" onclick={() => restoreValue('prompt')}>
                  {t('metadata.restore')}
                </button>
              {/if}
            </div>
            <textarea
              class="input min-h-28 resize-y"
              value={editState.effective.prompt}
              onfocus={beginMetadataEdit}
              onblur={(event) => setValue('prompt', event.currentTarget.value, metadataEditImageId)}
            ></textarea>
            {#if editState.diff.prompts.prompt.changed}
              <p class="mt-1 whitespace-pre-wrap text-[11px] text-ink-500">
                {t('metadata.before')}: {editState.original.prompt || '∅'}
              </p>
            {/if}
          </div>

          <div>
            <div class="mb-1 flex items-center justify-between">
              <p class="label mb-0">{t('drawer.negativePrompt')}</p>
              <div class="flex gap-1">
                {#if editState.diff.prompts.negative_prompt.changed}
                  <button class="btn btn-ghost btn-sm" onclick={() => restoreValue('negative_prompt')}>
                    {t('metadata.restore')}
                  </button>
                {/if}
                {#if editState.effective.negative_prompt != null}
                  <button class="btn btn-ghost btn-sm" onclick={() => deleteValue('negative_prompt')}>
                    {t('metadata.remove')}
                  </button>
                {/if}
              </div>
            </div>
            <textarea
              class="input min-h-20 resize-y"
              value={editState.effective.negative_prompt ?? ''}
              onfocus={beginMetadataEdit}
              onblur={(event) =>
                setValue('negative_prompt', event.currentTarget.value, metadataEditImageId)}
            ></textarea>
            {#if editState.diff.prompts.negative_prompt.changed}
              <p class="mt-1 whitespace-pre-wrap text-[11px] text-ink-500">
                {t('metadata.before')}: {editState.original.negative_prompt || '∅'}
              </p>
            {/if}
          </div>

          <div>
            <p class="label">{t('drawer.parameters')}</p>
            <div class="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {#each editState.diff.fields as item (item.key)}
                {#if !item.derived}
                  {@const hidden = hiddenByMetadataSettings(item)}
                  <div
                    class="{fieldIsWide(item) ? 'sm:col-span-2' : ''} {item.changed
                      ? 'rounded-lg border border-ready/40 bg-ready/5 p-2'
                      : 'py-1'}"
                  >
                    <div class="mb-1 flex items-center justify-between gap-2">
                      <label class="min-w-0 truncate text-xs font-medium" for={`meta-${item.key}`}>
                        {item.key}
                      </label>
                      {#if item.changed}
                        <button class="btn btn-ghost btn-sm" onclick={() => restoreValue(item.key)}>
                          {t('metadata.restore')}
                        </button>
                      {:else if !hidden}
                        <button class="btn btn-ghost btn-sm" onclick={() => deleteValue(item.key)}>
                          {t('metadata.remove')}
                        </button>
                      {/if}
                    </div>
                    {#if hidden}
                      <p class="text-xs text-ink-500">
                        {t('metadata.hiddenBySettings', { value: inputValue(item.original) || '∅' })}
                      </p>
                    {:else if item.deleted}
                      <p class="text-xs text-ink-500">{t('metadata.removed')}</p>
                    {:else}
                      <input
                        id={`meta-${item.key}`}
                        class="input"
                        value={inputValue(item.value)}
                        onfocus={beginMetadataEdit}
                        onblur={(event) =>
                          setValue(
                            item.key,
                            parsedValue(event.currentTarget.value, item.value),
                            metadataEditImageId,
                          )}
                      />
                    {/if}
                    {#if item.changed}
                      <p class="mt-1 break-all text-[11px] text-ink-500">
                        {t('metadata.before')}: {inputValue(item.original) || '∅'}
                      </p>
                    {/if}
                  </div>
                {/if}
              {/each}
              <div class="grid grid-cols-[1fr_2fr_auto] gap-2 sm:col-span-2">
                <input class="input" bind:value={newField} placeholder={t('metadata.fieldName')} />
                <input class="input" bind:value={newValue} placeholder={t('metadata.fieldValue')} />
                <button
                  class="btn"
                  disabled={!newField.trim()}
                  onclick={async () => {
                    await setValue(newField.trim(), newValue)
                    newField = ''
                    newValue = ''
                  }}>{t('metadata.add')}</button
                >
              </div>
            </div>
          </div>

          <div>
            <p class="label">{t('drawer.resources')}</p>
            <p class="mb-2 text-[11px] text-ink-500">{t('metadata.resourcesDerived')}</p>
            <div class="space-y-2">
              {#each editState.resources as resource (resource.id)}
                {@const before = originalResource(resource)}
                <div class="rounded-lg border border-ink-750 bg-ink-850 p-2 {resource.deleted_by_user ? 'opacity-55' : ''}">
                  <div class="flex gap-2">
                    <select class="input w-auto" disabled value={resource.resource_type}>
                      <option>{resource.resource_type}</option>
                    </select>
                    <input
                      class="input flex-1"
                      value={resource.name_in_prompt}
                      onblur={(event) => {
                        if (event.currentTarget.value !== resource.name_in_prompt)
                          resourceAction(() => api.patchImageResource(resource.id, { name: event.currentTarget.value }))
                      }}
                    />
                    <button class="btn btn-sm" onclick={() => (searching = resource)}>
                      {t('metadata.findModel')}
                    </button>
                  </div>
                  <div class="mt-2 grid grid-cols-2 items-end gap-2">
                    <div class="min-w-0 px-1 pb-2">
                      <p class="label mb-1">{t('metadata.hash')}</p>
                      {#if resource.hash}
                        <p class="mono truncate text-xs text-ink-300" title={resource.hash}>
                          {resource.hash}
                        </p>
                      {:else}
                        <!-- Not every generator writes one. A1111 records no hash for an
                             upscaler at all, so an empty field here is a fact about the
                             file rather than something missing from this row. -->
                        <p class="truncate text-xs text-ink-500">{t('metadata.hashAbsent')}</p>
                      {/if}
                    </div>
                    {#if showsWeight(resource)}
                      <input
                        class="input"
                        type="number"
                        step="0.05"
                        value={resource.weight ?? ''}
                        placeholder={t('metadata.weight')}
                        onblur={(event) =>
                          resourceAction(() =>
                            api.patchImageResource(resource.id, {
                              weight: event.currentTarget.value ? Number(event.currentTarget.value) : null,
                            }),
                          )}
                      />
                    {/if}
                  </div>
                  {#if resource.locked_by_user || resource.deleted_by_user || resource.added_by_user}
                    <p class="mt-1 text-[11px] text-ink-500">
                      {t('metadata.before')}: {before?.name_in_prompt ?? t('metadata.userAdded')} ·
                      {before?.hash ?? '∅'}
                    </p>
                  {/if}
                  <div class="mt-2 flex items-center gap-2">
                    {#if modelUrl(resource.model_id, resource.model_version_id)}
                      <a
                        class="text-[11px] text-teal-300 hover:underline"
                        href={modelUrl(resource.model_id, resource.model_version_id) ?? undefined}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {resource.model_name || t('metadata.unresolved')}
                        {resource.version_name ? ` · ${resource.version_name}` : ''}
                        {resource.resolved_from === 'local_name' ? ` · ${t('metadata.matchedByName')}` : ''}
                      </a>
                    {:else}
                      <span class="text-[11px] text-ink-400">
                        {resource.model_name || t('metadata.unresolved')}
                        {resource.version_name ? ` · ${resource.version_name}` : ''}
                        {resource.resolved_from === 'local_name' ? ` · ${t('metadata.matchedByName')}` : ''}
                      </span>
                    {/if}
                    <div class="flex-1"></div>
                    {#if resource.locked_by_user}
                      <button class="btn btn-ghost btn-sm" onclick={() => resourceAction(() => api.unlockImageResource(resource.id))}>
                        {t('metadata.unlock')}
                      </button>
                    {/if}
                    {#if resource.deleted_by_user && !resource.added_by_user}
                      <button class="btn btn-sm" onclick={() => resourceAction(() => api.restoreImageResource(resource.id))}>
                        {t('metadata.restore')}
                      </button>
                    {:else}
                      <button class="btn btn-ghost btn-sm" onclick={() => resourceAction(() => api.deleteImageResource(resource.id))}>
                        {resource.added_by_user ? t('metadata.removeAdded') : t('metadata.removeFromUpload')}
                      </button>
                    {/if}
                  </div>
                  {#if resource.model_version_id == null && !resource.deleted_by_user}
                    <p
                      class="mt-2 rounded px-2 py-1.5 text-[11px] leading-relaxed"
                      style="color: var(--color-ready); background: color-mix(in srgb, var(--color-ready) 10%, transparent);"
                    >
                      ! {t('metadata.unresolvedCivitai')}
                    </p>
                  {/if}
                </div>
              {/each}

              <div class="rounded-lg border border-dashed border-ink-700 p-2">
                <div class="grid grid-cols-[auto_1fr_auto] gap-2">
                  <select class="input w-auto" bind:value={resourceType}>
                    <option value="checkpoint">checkpoint</option>
                    <option value="lora">LoRA</option>
                    <option value="embedding">embedding</option>
                  </select>
                  <input
                    class="input"
                    list="resource-name-options"
                    bind:value={resourceName}
                    oninput={loadSuggestions}
                    placeholder={t('metadata.resourceName')}
                  />
                  <datalist id="resource-name-options">
                    {#each suggestions as suggestion (suggestion.hash)}
                      <option value={suggestion.name}>{suggestion.model_name ?? ''}</option>
                    {/each}
                  </datalist>
                  <button class="btn" disabled={!resourceName.trim()} onclick={addResource}>{t('metadata.add')}</button>
                </div>
                {#if chosenSuggestion}
                  <p class="muted mt-2 text-[11px]">
                    {chosenSuggestion.model_name
                      ? t('metadata.resourceBinds', { model: chosenSuggestion.model_name })
                      : t('metadata.resourceBindsHash')}
                  </p>
                {/if}
              </div>
            </div>
          </div>

          {#if editState.warnings.length}
            <div class="rounded-lg border border-ink-750 bg-ink-850 p-2">
              <p class="label mb-2">{t('metadata.warnings')}</p>
              {#each editState.warnings as warning (`${warning.code}-${warning.field}`)}
                <p class="mb-1 text-xs text-ink-300">
                  ⚠ {coded('metadata.warning', warning.code, warning.message, warning.params)}
                </p>
              {/each}
            </div>
          {/if}

          {#if editState.comfyui_workflow_replaced}
            <div
              class="rounded-lg border p-2 text-xs leading-relaxed"
              style="color: var(--color-ink-200); background: color-mix(in srgb, var(--color-ready) 12%, transparent); border-color: color-mix(in srgb, var(--color-ready) 35%, transparent);"
            >
              ⚠ {t('metadata.comfyuiWorkflowReplaced')}
            </div>
          {/if}

          <div>
            <button class="btn btn-ghost btn-sm -ml-2" onclick={() => (showRaw = !showRaw)}>
              {showRaw ? '▾' : '▸'} {t('metadata.effectiveRaw')}
            </button>
            {#if showRaw}
              <pre class="mono mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-ink-750 bg-ink-950 p-2 text-[11px] text-ink-300">{editState.effective_infotext}</pre>
            {/if}
          </div>
        </div>
      </div>
    {/if}
  </aside>
</div>

{#if searching}
  <ResourceSearchModal
    resource={searching}
    onClose={() => (searching = null)}
    onAttached={() => {
      searching = null
      reload()
      invalidate()
    }}
  />
{/if}
