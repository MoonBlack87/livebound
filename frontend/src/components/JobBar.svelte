<script lang="ts">
  import { api } from '../api/client'
  import { plural, t } from '../i18n/index.svelte'
  import { app, invalidate, invalidatePosts, setActiveJob, toast } from '../state/store.svelte'
  import Spinner from './ui/Spinner.svelte'
  import type { Job } from '../types/api'

  /**
   * The single progress strip for whatever long-running thing is happening.
   * Polls rather than streaming: one job at a time, one request per second.
   */

  const running = $derived(app.activeJob?.status === 'running' || app.activeJob?.status === 'starting')
  const percent = $derived(
    app.activeJob?.total ? Math.round((app.activeJob.processed / app.activeJob.total) * 100) : 0,
  )

  let jobCursor: number | undefined = undefined
  const visibleJobIds = new Set<number>()

  function reportFinished(job: Job) {
    const result = (job.result ?? {}) as Record<string, number>
    const quietWatch =
      job.kind === 'watch-posts' &&
      job.status === 'done' &&
      job.failed === 0 &&
      (result.changed ?? 0) === 0 &&
      (result.divergences ?? 0) === 0 &&
      (result.ratings_updated ?? 0) === 0
    if (quietWatch) return

    if (job.kind === 'watch-posts') invalidatePosts()
    else invalidate()
    if (job.status === 'error') toast('error', job.error ?? t('job.failed'))
    else if (job.status === 'cancelled') {
      toast('info', job.kind === 'trash' ? summariseCancelledTrash(job) : t('job.cancelled'))
    } else {
      const kind =
        (job.kind === 'trash' || job.kind === 'watch-posts') && job.failed
          ? 'error'
          : 'success'
      toast(kind, summarise(job))
    }
  }

  // Jobs started by a click are already visible. Remember them without jumping
  // the cursor past an earlier, short background run that still needs reporting.
  $effect(() => {
    const job = app.activeJob
    if (job) visibleJobIds.add(job.id)
  })

  // A check started by the scheduler has no click to hang on. The cursor makes
  // completed jobs visible too, so even a sub-five-second run refreshes the UI.
  $effect(() => {
    let cancelled = false

    const poll = async () => {
      try {
        const updates = await api.jobUpdates(jobCursor)
        if (cancelled) return
        let firstDeferred: number | undefined
        for (const job of updates.items) {
          if (visibleJobIds.has(job.id)) continue
          if (job.status === 'starting' || job.status === 'running') {
            if (!app.activeJob) {
              visibleJobIds.add(job.id)
              setActiveJob(job)
            } else {
              firstDeferred = Math.min(firstDeferred ?? job.id, job.id)
            }
          } else {
            visibleJobIds.add(job.id)
            reportFinished(job)
          }
        }
        // A second simultaneous running job cannot fit in the one job bar. Keep
        // the cursor before it; already reported ids are suppressed by the set.
        jobCursor = firstDeferred === undefined ? updates.cursor : firstDeferred - 1
        for (const id of visibleJobIds) {
          if (id <= jobCursor) visibleJobIds.delete(id)
        }
      } catch {
        /* the bar is not worth a toast */
      }
    }

    void poll()
    const timer = setInterval(poll, 5000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  })

  $effect(() => {
    const job = app.activeJob
    if (!job || (job.status !== 'running' && job.status !== 'starting')) return

    let cancelled = false
    const timer = setInterval(async () => {
      try {
        const next = await api.job(job.id)
        if (cancelled) return
        setActiveJob(next)
        if (next.status !== 'running' && next.status !== 'starting') {
          reportFinished(next)
          setTimeout(() => setActiveJob(null), 2500)
        }
      } catch {
        if (!cancelled) clearInterval(timer)
      }
    }, 900)

    return () => {
      cancelled = true
      clearInterval(timer)
    }
  })

  function summarise(job: Job): string {
    const result = (job.result ?? {}) as Record<string, number>
    if (job.kind === 'scan') {
      return t('job.done.scan', {
        images: result.scanned ?? 0,
        failed: result.failed ?? 0,
        reapplied: result.reapplied ?? 0,
      })
    }
    if (job.kind === 'push') {
      const pushed = plural('job.done.push', job.succeeded)
      return job.skipped ? `${pushed} ${t('job.done.push.skipped', { count: job.skipped })}` : pushed
    }
    if (job.kind === 'llm') return plural('job.done.llm', job.succeeded)
    if (job.kind === 'sync') return t('job.done.sync')
    if (job.kind === 'watch-posts') {
      return t('job.done.watchPosts', {
        changed: result.changed ?? 0,
        divergences: result.divergences ?? 0,
        ratings: result.ratings_updated ?? 0,
        incomplete: job.failed,
      })
    }
    if (job.kind === 'adopt') {
      return t('job.done.adopt', {
        created: job.succeeded,
        skipped: job.skipped,
        failed: job.failed,
      })
    }
    if (job.kind === 'model-hash') {
      return t('job.done.modelHash', {
        recognized: result.recognized ?? 0,
        unrecognized: result.unrecognized ?? 0,
        unqueried: result.unqueried ?? 0,
        reapplied: result.reapplied ?? 0,
      })
    }
    if (job.kind === 'trash') {
      return t('job.done.trash', { moved: job.succeeded, failed: job.failed })
    }
    if (job.kind === 'archive') {
      return t('job.done.archive', {
        moved: result.moved ?? 0,
        failed: result.failed ?? 0,
        folders: result.folders_removed ?? 0,
      })
    }
    return t('job.done')
  }

  function summariseCancelledTrash(job: Job): string {
    return t('job.cancelled.trash', {
      moved: job.succeeded,
      failed: job.failed,
      remaining: Math.max(job.total - job.processed, 0),
    })
  }

  function stage(job: Job): string {
    if (job.kind === 'adopt') {
      return job.stage ? t('job.stage.adoptPost', { id: job.stage }) : t('job.stage.adopt')
    }
    if (job.kind === 'model-hash') return t('job.stage.modelHash')
    if (job.kind === 'watch-posts') return t('job.stage.watchPosts')
    if (job.kind === 'trash') {
      return job.stage ? t('job.stage.trashFile', { file: job.stage }) : t('job.stage.trash')
    }
    return job.stage || job.kind
  }
</script>

{#if app.activeJob}
  {@const job = app.activeJob}
  <div class="border-b border-ink-750 bg-ink-900 px-5 py-2">
    <div class="flex items-center gap-3">
      {#if running}
        <Spinner />
      {:else}
        <span aria-hidden="true">{job.status === 'error' ? '✕' : '✓'}</span>
      {/if}
      <span class="text-xs font-medium text-ink-200">{stage(job)}</span>
      {#if job.total > 0}
        <span class="mono text-xs text-ink-400">{job.processed}/{job.total}</span>
      {/if}
      {#if job.failed > 0}
        <span class="text-xs" style="color: var(--color-failed);">
          {t('job.failedCount', { count: job.failed })}
        </span>
      {/if}
      <div class="flex-1"></div>
      {#if running}
        <button class="btn btn-ghost btn-sm" onclick={() => api.cancelJob(job.id)}>
          {t('ui.cancel')}
        </button>
      {/if}
    </div>
    {#if job.total > 0}
      <div class="mt-1.5 h-1 overflow-hidden rounded-full bg-ink-800">
        <div
          class="h-full rounded-full transition-[width] duration-300"
          style="width: {percent}%; background: {job.failed
            ? 'var(--color-ready)'
            : 'var(--color-accent-500)'};"
        ></div>
      </div>
    {/if}
  </div>
{/if}
