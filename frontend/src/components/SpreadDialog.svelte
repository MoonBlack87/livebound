<script lang="ts">
  import { api } from '../api/client'
  import { t } from '../i18n/index.svelte'
  import { invalidate, toast } from '../state/store.svelte'
  import Modal from './ui/Modal.svelte'
  import { formatTime } from './ui/format.svelte'
  import type { SpreadPlanEntry } from '../types/api'

  let { onClose }: { onClose: () => void } = $props()

  let posts = $state<{ id: number; title: string }[]>([])
  let selected = $state<number[]>([])
  let startOffset = $state('90')
  let interval = $state(120)
  let plan = $state<SpreadPlanEntry[] | null>(null)

  $effect(() => {
    api
      .posts('draft,ready,failed')
      .then((data) => {
        posts = data.items.map((post) => ({ id: post.id, title: post.title }))
        selected = data.items.map((post) => post.id)
      })
      .catch(() => undefined)
  })

  async function run(apply: boolean) {
    try {
      const result = await api.spread({
        post_ids: selected,
        start_offset_minutes: Number(startOffset) || 60,
        interval_minutes: interval,
        apply,
      })
      plan = result.plan
      if (apply) {
        invalidate()
        toast('success', t('spread.applied', { count: result.plan.length }))
        onClose()
      }
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  function toggle(id: number, checked: boolean) {
    selected = checked ? [...selected, id] : selected.filter((item) => item !== id)
  }
</script>

<Modal title={t('spread.title')} wide {onClose}>
  <div class="grid grid-cols-2 gap-5">
    <div class="space-y-3">
      <div>
        <span class="label">{t('spread.firstIn')}</span>
        <input class="input" bind:value={startOffset} placeholder={t('spread.minutes')} />
        <p class="mt-1 text-xs text-ink-500">{t('spread.relativeHint')}</p>
      </div>
      <div>
        <span class="label">{t('spread.interval')}</span>
        <input type="number" class="input" bind:value={interval} min="1" />
      </div>
      <div>
        <span class="label">{t('spread.posts', { count: selected.length })}</span>
        <div
          class="max-h-56 space-y-0.5 overflow-y-auto rounded-lg border border-ink-750 p-1.5"
        >
          {#each posts as post (post.id)}
            <label
              class="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-xs hover:bg-ink-850"
            >
              <input
                type="checkbox"
                class="accent-teal-500"
                checked={selected.includes(post.id)}
                onchange={(event) =>
                  toggle(post.id, (event.currentTarget as HTMLInputElement).checked)}
              />
              <span class="truncate">
                {post.title || t('drawer.dupe.post', { id: post.id })}
              </span>
            </label>
          {/each}
        </div>
      </div>
    </div>

    <div>
      <span class="label">{t('spread.preview')}</span>
      {#if plan}
        <div class="max-h-80 space-y-1 overflow-y-auto rounded-lg border border-ink-750 p-2">
          {#each plan as entry (entry.post_id)}
            <div class="flex items-center gap-2 text-xs">
              <span class="mono w-32 shrink-0 text-ink-400">{formatTime(entry.publish_at)}</span>
              <span class="truncate text-ink-200">{entry.title}</span>
              {#if entry.locked}
                <span class="chip text-[10px]">{t('spread.locked')}</span>
              {/if}
            </div>
          {/each}
        </div>
      {:else}
        <p
          class="rounded-lg border border-dashed border-ink-700 py-10 text-center text-xs text-ink-500"
        >
          {t('spread.previewHint')}
        </p>
      {/if}
    </div>
  </div>

  {#snippet footer()}
    <button class="btn" onclick={() => run(false)}>{t('spread.preview')}</button>
    <button class="btn btn-primary" disabled={!plan} onclick={() => run(true)}>
      {t('spread.apply')}
    </button>
  {/snippet}
</Modal>
