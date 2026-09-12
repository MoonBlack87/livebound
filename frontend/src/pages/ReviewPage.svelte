<script lang="ts">
  import { api, translateIssue } from '../api/client'
  import RunRow from '../components/RunRow.svelte'
  import Empty from '../components/ui/Empty.svelte'
  import IssueRow from '../components/ui/IssueRow.svelte'
  import Spinner from '../components/ui/Spinner.svelte'
  import StateBadge from '../components/ui/StateBadge.svelte'
  import { formatDateTime } from '../components/ui/format.svelte'
  import { plural, t } from '../i18n/index.svelte'
  import { app, openPost, setActiveJob, toast } from '../state/store.svelte'
  import type { Post, PushPreview, PushRun } from '../types/api'

  /**
   * The last stop before anything is sent.
   *
   * The preview is not decorative: it resolves the relative schedules, runs the
   * full validation and lists the exact API calls in order. Since a published
   * post cannot be unpublished, seeing the plan beforehand is the only real
   * safety net.
   */

  let posts = $state<Post[]>([])
  let previews = $state<Record<number, PushPreview>>({})
  let selected = $state<number[]>([])
  let expanded = $state<number | null>(null)
  let loading = $state(true)
  let runs = $state<PushRun[]>([])
  /** The log is a short list, not a history: the last day unless asked. */
  let allRuns = $state(false)

  const pushable = $derived(selected.filter((id) => previews[id]?.can_push))

  $effect(() => {
    void app.revision
    void app.postRevision
    loading = true
    api
      .posts('ready,failed,remote_draft,needs_reconcile')
      .then(async (data) => {
        posts = data.items
        selected = data.items.filter((post) => post.state === 'ready').map((post) => post.id)
        if (data.items.length) {
          const result = await api.pushPreview(data.items.map((post) => post.id))
          previews = Object.fromEntries(result.items.map((item) => [item.post_id, item]))
        } else {
          previews = {}
        }
      })
      .catch((error) => toast('error', (error as Error).message))
      .finally(() => (loading = false))
    api
      .pushRuns(allRuns ? undefined : 24)
      .then((data) => (runs = data.items))
      .catch(() => undefined)
  })

  async function push() {
    if (!pushable.length) return
    try {
      setActiveJob(await api.push(pushable))
      selected = []
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  function toggle(postId: number, checked: boolean) {
    selected = checked ? [...selected, postId] : selected.filter((id) => id !== postId)
  }

  function imageCount(post: Post, preview: PushPreview | undefined): number {
    if (post.image_count != null) return post.image_count
    return preview?.calls.filter((call) => call.call === 'upload_image').length ?? 0
  }

  function statusColor(preview: PushPreview): string {
    if (preview.can_push) return 'var(--color-published)'
    return preview.blocked ? 'var(--color-failed)' : 'var(--color-ready)'
  }

  function statusLabel(preview: PushPreview): string {
    if (preview.can_push) return `✓ ${t('review.ready')}`
    if (preview.blocked) return `✕ ${plural('review.errors', preview.error_count)}`
    return `! ${plural('review.open', preview.warning_count)}`
  }
</script>

<div class="flex h-full flex-col">
  <header class="flex items-center gap-2 border-b border-ink-800 px-5 py-3">
    <h1 class="text-sm font-semibold">{t('nav.review')}</h1>
    <span class="mono text-xs text-ink-400">{t('review.pending', { count: posts.length })}</span>
    <div class="flex-1"></div>
    {#if selected.length > pushable.length}
      <span class="text-xs" style="color: var(--color-ready);">
        {t('review.blocked', { count: selected.length - pushable.length })}
      </span>
    {/if}
    <button class="btn btn-primary" disabled={!pushable.length} onclick={push}>
      {plural('review.pushAction', pushable.length)}
    </button>
  </header>

  <div class="min-h-0 flex-1 overflow-y-auto p-5">
    {#if loading}
      <Spinner />
    {:else if !posts.length}
      <Empty icon="review" title={t('review.empty.title')} hint={t('review.empty.hint')} />
    {:else}
      <div class="space-y-2">
        {#each posts as post (post.id)}
          {@const preview = previews[post.id]}
          {@const open = expanded === post.id}
          <article class="panel overflow-hidden">
            <div class="flex items-center gap-3 px-4 py-3">
              <input
                type="checkbox"
                class="accent-teal-500"
                checked={selected.includes(post.id)}
                disabled={!preview?.can_push}
                onchange={(event) =>
                  toggle(post.id, (event.currentTarget as HTMLInputElement).checked)}
              />
              <StateBadge state={post.state} />
              <button
                class="min-w-0 flex-1 truncate text-left text-sm text-ink-100 hover:underline"
                onclick={() => openPost(post.id)}
              >
                {#if post.title}{post.title}{:else}<span class="text-ink-500">
                    {t('post.untitled')}
                  </span>{/if}
              </button>

              <span class="mono shrink-0 text-xs text-ink-400">
                {plural('post.images', imageCount(post, preview))}
              </span>
              <span class="mono shrink-0 text-xs text-ink-400">
                {formatDateTime(preview?.resolved_publish_at ?? post.resolved_publish_at)}
              </span>

              {#if preview}
                <span class="chip shrink-0 text-[11px]" style="color: {statusColor(preview)};">
                  {statusLabel(preview)}
                </span>
              {/if}

              <button
                class="btn btn-ghost btn-sm shrink-0"
                onclick={() => (expanded = open ? null : post.id)}
              >
                {open ? '▾' : '▸'}
              </button>
            </div>

            {#if open && preview}
              <div class="grid grid-cols-2 gap-5 border-t border-ink-800 bg-ink-850 px-4 py-3">
                <div>
                  <p class="label">{t('review.checklist')}</p>
                  {#if preview.comfyui_workflow_replaced_images.length}
                    <IssueRow level="warning">
                      {t('review.comfyuiWorkflowReplaced', { images: preview.comfyui_workflow_replaced_images.join(', ') })}
                    </IssueRow>
                  {/if}
                  {#if !preview.issues.length && !preview.comfyui_workflow_replaced_images.length}
                    <IssueRow level="info">{t('review.noIssues')}</IssueRow>
                  {:else}
                    <div class="space-y-1.5">
                      {#each preview.issues as issue, index (index)}
                        <IssueRow level={issue.level}>{translateIssue(issue)}</IssueRow>
                      {/each}
                    </div>
                  {/if}
                </div>
                <div>
                  <p class="label">{t('review.calls')}</p>
                  <ol class="space-y-1">
                    {#each preview.calls as call, index (index)}
                      <li class="flex items-baseline gap-2 text-[11px]">
                        <span
                          class="mono shrink-0 rounded px-1 py-0.5 text-[9.5px] uppercase"
                          style="background: color-mix(in srgb, {call.transport === 'mcp'
                            ? 'var(--color-accent-500)'
                            : 'var(--color-scheduled)'} 18%, transparent);
                                 color: {call.transport === 'mcp'
                            ? 'var(--color-accent-300)'
                            : 'var(--color-scheduled)'};"
                        >
                          {call.transport}
                        </span>
                        <span class="mono shrink-0 text-ink-200">{call.call}</span>
                        <span class="truncate text-ink-500">
                          {call.detail}{call.changed ? ` (${t('review.metadataChanged')})` : ''}
                        </span>
                      </li>
                    {/each}
                  </ol>
                </div>
              </div>
            {/if}
          </article>
        {/each}
      </div>
    {/if}

    {#if runs.length}
      <div class="mt-6">
        <div class="flex items-baseline justify-between">
          <p class="label">
            {allRuns ? t('review.allRuns') : t('review.recentRuns')}
          </p>
          <button class="btn btn-ghost btn-sm" onclick={() => (allRuns = !allRuns)}>
            {allRuns ? t('review.lastDay') : t('review.showOlder')}
          </button>
        </div>
        <div class="space-y-1.5">
          {#each runs.slice(0, allRuns ? 20 : 6) as run (run.id)}
            <RunRow {run} />
          {/each}
        </div>
      </div>
    {:else if allRuns}
      <div class="mt-6">
        <div class="flex items-baseline justify-between">
          <p class="label">{t('review.allRuns')}</p>
          <button class="btn btn-ghost btn-sm" onclick={() => (allRuns = false)}>
            {t('review.lastDay')}
          </button>
        </div>
        <p class="text-xs text-ink-500">{t('review.noRuns')}</p>
      </div>
    {/if}
  </div>
</div>
