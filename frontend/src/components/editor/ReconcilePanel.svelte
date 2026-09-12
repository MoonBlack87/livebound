<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { toast } from '../../state/store.svelte'
  import Spinner from '../ui/Spinner.svelte'

  let { postId, onDone }: { postId: number; onDone: () => void } = $props()

  let candidates = $state<Record<string, unknown>[] | null>(null)
  let reason = $state('')
  let manualUrl = $state('')
  let manualId = $state('')
  let busy = $state(false)

  $effect(() => {
    const id = postId
    api
      .reconcileCandidates(id)
      .then((data) => {
        candidates = data.candidates
        reason = data.reason
        manualUrl = (data.manual_url as string) ?? ''
      })
      .catch((error) => {
        candidates = []
        reason = (error as Error).message
      })
  })

  async function adopt(remoteId: number) {
    busy = true
    try {
      await api.reconcileAdopt(postId, remoteId)
      toast('success', t('reconcile.matched'))
      onDone()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = false
    }
  }
</script>

<div
  class="rounded-lg border p-3"
  style="border-color: color-mix(in srgb, var(--color-attention) 40%, transparent);
         background: color-mix(in srgb, var(--color-attention) 8%, transparent);"
>
  <p class="text-xs font-medium text-ink-100">{t('reconcile.title')}</p>
  <p class="mt-1 text-[11px] leading-relaxed text-ink-300">{t('reconcile.explain')}</p>

  {#if candidates === null}
    <p class="mt-2 flex items-center gap-2 text-xs text-ink-400">
      <Spinner />
      {t('discover.searching')}
    </p>
  {:else if candidates.length}
    <div class="mt-2 space-y-1.5">
      {#each candidates as candidate (candidate.remote_post_id)}
        <div class="flex items-center gap-2 rounded-md bg-ink-850 px-2.5 py-1.5">
          <div class="min-w-0 flex-1">
            <p class="truncate text-xs text-ink-200">
              {(candidate.title as string) ||
                t('drawer.dupe.post', { id: candidate.remote_post_id as number })}
            </p>
            <p class="text-[10px] text-ink-500">
              {(candidate.reasons as string[]).join(' · ')}
            </p>
          </div>
          <button
            class="btn btn-sm"
            disabled={busy}
            onclick={() => adopt(candidate.remote_post_id as number)}
          >
            {t('reconcile.match')}
          </button>
        </div>
      {/each}
    </div>
  {:else}
    {#if reason}
      <p class="mt-2 text-[11px] leading-relaxed text-ink-400">{reason}</p>
    {/if}
    <div class="mt-2 flex gap-2">
      <input
        class="input"
        placeholder={t('reconcile.postIdPlaceholder')}
        value={manualId}
        inputmode="numeric"
        oninput={(event) => {
          manualId = (event.currentTarget as HTMLInputElement).value.replace(/[^0-9]/g, '')
        }}
        onkeydown={(event) => {
          if (event.key === 'Enter' && manualId) adopt(Number(manualId))
        }}
      />
      <button class="btn btn-sm" disabled={!manualId || busy} onclick={() => adopt(Number(manualId))}>
        {t('reconcile.match')}
      </button>
    </div>
    {#if manualUrl}
      <a
        class="mt-1.5 inline-block text-[11px] underline decoration-dotted"
        href={manualUrl}
        target="_blank"
        rel="noreferrer"
        style="color: var(--color-accent-300);"
      >
        {t('discover.openDrafts')}
      </a>
    {/if}
    <p class="mt-1.5 text-[11px] leading-relaxed text-ink-500">{t('reconcile.notFound')}</p>
  {/if}
</div>
