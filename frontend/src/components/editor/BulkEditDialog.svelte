<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { toast } from '../../state/store.svelte'
  import type { BulkEditPlanEntry } from '../../types/api'
  import Spinner from '../ui/Spinner.svelte'

  let {
    imageIds = [],
    postId,
    count,
    readOnly = false,
    onClose,
    onApplied,
  }: {
    imageIds?: number[]
    postId?: number
    count: number
    readOnly?: boolean
    onClose: () => void
    onApplied: () => void
  } = $props()

  let field = $state('')
  let value = $state('')
  let deleteField = $state('')
  let find = $state('')
  let replace = $state('')
  let target = $state<'prompt' | 'negative' | 'both'>('prompt')
  let regex = $state(false)
  let caseSensitive = $state(false)
  let prepend = $state('')
  let append = $state('')
  let negativeAppend = $state('')
  let promptValues = $state<Record<number, string>>({})
  let plan = $state<BulkEditPlanEntry[]>([])
  let busy = $state(false)
  let shortening = $state(false)
  let knownFields = $state<string[]>([])
  let unmatched = $state<string[]>([])

  // The fields these very images carry, through the same rule the edit uses -
  // so the list offers what can actually be changed rather than everything the
  // library has ever seen (`MET-09`).
  $effect(() => {
    api
      .metadataFields({ postId, imageIds })
      .then((data) => (knownFields = data.items.map((item) => item.field_name)))
      .catch(() => undefined)
  })

  function requestBody(apply: boolean) {
    const operations: Record<string, unknown> = {
      fields: field.trim() ? { [field.trim()]: value } : {},
      delete_fields: deleteField.trim() ? [deleteField.trim()] : [],
      prompt_prepend: prepend.trim() || null,
      prompt_append: append.trim() || null,
      negative_append: negativeAppend.trim() || null,
      prompt_values: promptValues,
    }
    if (find) {
      operations.find_replace = {
        find,
        replace,
        target,
        regex,
        case_sensitive: caseSensitive,
      }
    }
    return { image_ids: imageIds, post_id: postId, operations, apply }
  }

  async function preview() {
    busy = true
    try {
      const result = await api.bulkImageEdit(requestBody(false))
      plan = result.plan
      unmatched = result.unmatched_delete_fields ?? []
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = false
    }
  }

  async function apply() {
    busy = true
    try {
      const result = await api.bulkImageEdit(requestBody(true))
      toast('success', t('metadata.bulkApplied', { count: result.changed }))
      onApplied()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = false
    }
  }

  async function shorten() {
    shortening = true
    try {
      let job = await api.llmShorten(imageIds)
      while (job.status === 'starting' || job.status === 'running') {
        await new Promise((resolve) => setTimeout(resolve, 700))
        job = await api.job(job.id)
      }
      if (job.status !== 'done') throw new Error(job.error || t('metadata.shortenFailed'))
      const items = (job.result.items || []) as {
        image_id: number
        suggestion: string
      }[]
      promptValues = Object.fromEntries(items.map((item) => [item.image_id, item.suggestion]))
      await preview()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      shortening = false
    }
  }

  function shown(value: unknown): string {
    if (value == null) return '∅'
    const text = typeof value === 'string' ? value : JSON.stringify(value)
    return text.length > 180 ? `${text.slice(0, 180)}…` : text
  }
</script>

<div class="fixed inset-0 z-[65] flex items-center justify-center bg-black/70 p-4">
  <section class="panel-raised flex max-h-[92vh] w-full max-w-4xl flex-col">
    <header class="flex items-center gap-3 border-b border-ink-750 px-5 py-3">
      <h2 class="flex-1 text-sm font-semibold">{t('metadata.bulkTitle', { count })}</h2>
      {#if busy || shortening}<Spinner />{/if}
      <button class="btn btn-ghost btn-sm" onclick={onClose}>✕</button>
    </header>

    <div class="grid min-h-0 flex-1 grid-cols-2 overflow-hidden">
      <div class="space-y-4 overflow-y-auto border-r border-ink-800 p-5">
        <div>
          <p class="label">{t('metadata.findReplace')}</p>
          <div class="grid grid-cols-2 gap-2">
            <input class="input" bind:value={find} placeholder={t('metadata.find')} />
            <input class="input" bind:value={replace} placeholder={t('metadata.replace')} />
          </div>
          <div class="mt-2 flex flex-wrap gap-3 text-xs text-ink-300">
            <select class="input w-auto" bind:value={target}>
              <option value="prompt">{t('drawer.prompt')}</option>
              <option value="negative">{t('drawer.negativePrompt')}</option>
              <option value="both">{t('metadata.bothPrompts')}</option>
            </select>
            <label class="chip"><input type="checkbox" bind:checked={regex} /> Regex</label>
            <label class="chip"><input type="checkbox" bind:checked={caseSensitive} /> Aa</label>
          </div>
        </div>

        <div>
          <p class="label">{t('metadata.prependAppend')}</p>
          <input class="input mb-2" bind:value={prepend} placeholder={t('metadata.prepend')} />
          <input class="input mb-2" bind:value={append} placeholder={t('metadata.append')} />
          <input class="input" bind:value={negativeAppend} placeholder={t('metadata.negativeAppend')} />
        </div>

        <div>
          <p class="label">{t('metadata.parameter')}</p>
          <div class="grid grid-cols-2 gap-2">
            <input
              class="input"
              list="bulk-known-fields"
              bind:value={field}
              placeholder={t('metadata.fieldName')}
            />
            <input class="input" bind:value={value} placeholder={t('metadata.fieldValue')} />
          </div>
          <input
            class="input mt-2"
            list="bulk-known-fields"
            bind:value={deleteField}
            placeholder={t('metadata.deleteField')}
          />
          <!-- A list, not a closed set: a field the inventory has not seen yet
               must stay enterable (`MET-09`). -->
          <datalist id="bulk-known-fields">
            {#each knownFields as name (name)}
              <option value={name}></option>
            {/each}
          </datalist>
          {#if unmatched.length}
            <p class="mt-1.5 text-[11px] leading-relaxed" style="color: var(--color-ready);">
              {t('metadata.deleteFieldUnmatched', { fields: unmatched.join(', ') })}
            </p>
          {/if}
        </div>

        <button class="btn w-full justify-center" disabled={shortening || !imageIds.length} onclick={shorten}>
          ✨ {t('metadata.shortenSuggest')}
        </button>
        {#if postId && !imageIds.length}
          <p class="text-[11px] text-ink-500">{t('metadata.shortenSelectionOnly')}</p>
        {/if}
      </div>

      <div class="min-h-0 overflow-y-auto p-5">
        <p class="label">{t('metadata.preview')}</p>
        {#if !plan.length}
          <p class="text-xs text-ink-500">{t('metadata.previewHint')}</p>
        {:else}
          <div class="space-y-2">
            {#each plan as item (item.image_id)}
              <div class="rounded-lg border border-ink-750 bg-ink-850 p-3">
                <p class="truncate text-xs font-medium">{item.filename}</p>
                {#if item.changes.length}
                  {#each item.changes as change (change.field)}
                    <div class="mt-2 text-[11px]">
                      <span class="text-ink-400">{change.field}</span>
                      <p class="line-through opacity-60">{shown(change.before)}</p>
                      <p class="text-ink-100">{shown(change.after)}</p>
                    </div>
                  {/each}
                {:else}
                  <p class="mt-1 text-[11px] text-ink-500">{t('metadata.noChange')}</p>
                {/if}
              </div>
            {/each}
          </div>
        {/if}
      </div>
    </div>

    <footer class="flex justify-end gap-2 border-t border-ink-750 px-5 py-3">
      <button class="btn" disabled={busy} onclick={preview}>{t('metadata.preview')}</button>
      <button class="btn btn-primary" disabled={readOnly || busy || !plan.length} onclick={apply}>
        {t('metadata.apply')}
      </button>
    </footer>
  </section>
</div>
