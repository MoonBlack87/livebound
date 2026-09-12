<script lang="ts">
  import { api } from '../api/client'
  import { t } from '../i18n/index.svelte'
  import { setActiveJob, toast } from '../state/store.svelte'
  import { formatDateTime } from './ui/format.svelte'
  import type { PushRun } from '../types/api'

  let { run }: { run: PushRun } = $props()

  let open = $state(false)
  let detail = $state<PushRun | null>(null)

  const resumable = $derived(run.status === 'interrupted' || run.failed > 0)

  const DOT: Record<string, string> = {
    done: 'var(--color-published)',
    interrupted: 'var(--color-ready)',
    error: 'var(--color-failed)',
  }
  const ITEM_COLOR: Record<string, string> = {
    done: 'var(--color-published)',
    error: 'var(--color-failed)',
  }

  async function resume() {
    try {
      setActiveJob(await api.resumeRun(run.id))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function expand() {
    if (!open && !detail) detail = await api.pushRun(run.id)
    open = !open
  }
</script>

<div class="panel overflow-hidden">
  <div class="flex items-center gap-3 px-3 py-2">
    <span
      class="h-2 w-2 shrink-0 rounded-full"
      style="background: {DOT[run.status] ?? 'var(--color-ink-500)'};"
      aria-hidden="true"
    ></span>
    <span class="mono text-[11px] text-ink-400">#{run.id}</span>
    <span class="text-xs text-ink-200">{run.kind}</span>
    <span class="text-[11px] text-ink-400">
      {t('run.ok', { count: run.succeeded })}{run.failed
        ? `, ${t('run.failed', { count: run.failed })}`
        : ''}{run.skipped ? `, ${t('run.skipped', { count: run.skipped })}` : ''}
    </span>
    <span class="mono text-[11px] text-ink-500">{formatDateTime(run.created_at)}</span>
    <div class="flex-1"></div>
    {#if resumable}
      <button class="btn btn-sm" onclick={resume}>{t('run.resume')}</button>
    {/if}
    <button class="btn btn-ghost btn-sm" onclick={expand}>{open ? '▾' : '▸'}</button>
  </div>
  {#if open && detail}
    <div class="space-y-1 border-t border-ink-800 bg-ink-850 px-3 py-2">
      {#each detail.items as item (item.id)}
        <div class="flex items-center gap-2 text-[11px]">
          <span
            class="w-16 shrink-0"
            style="color: {ITEM_COLOR[item.status as string] ?? 'var(--color-ink-400)'};"
          >
            {item.status}
          </span>
          <span class="w-44 shrink-0 truncate text-ink-300">{item.title || '—'}</span>
          <span class="text-ink-400">{item.step_label}</span>
          {#if item.error}
            <span class="truncate" style="color: var(--color-failed);">{item.error}</span>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>
