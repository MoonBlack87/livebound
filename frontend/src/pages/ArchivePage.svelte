<script lang="ts">
  import { api } from '../api/client'
  import ConfirmDialog from '../components/ui/ConfirmDialog.svelte'
  import Empty from '../components/ui/Empty.svelte'
  import Spinner from '../components/ui/Spinner.svelte'
  import { formatDateTime } from '../components/ui/format.svelte'
  import ArchiveMoveNow from '../components/ArchiveMoveNow.svelte'
  import { plural, t } from '../i18n/index.svelte'
  import { app, invalidate, openPost, toast } from '../state/store.svelte'
  import type { ArchivePost, Post } from '../types/api'

  /**
   * What has been published and filed away.
   *
   * Its own view rather than a column on the board: the board answers "what is
   * going on", this answers "what did I publish", and the two grow at very
   * different rates.
   */

  const PAGE_SIZE = 50

  let items = $state<ArchivePost[]>([])
  let total = $state(0)
  let offset = $state(0)
  let loading = $state(true)
  let loadError = $state('')
  let fromDate = $state('')
  let toDate = $state('')
  let renamePost = $state<Post | null>(null)
  let renaming = $state(false)
  let loadSequence = 0

  $effect(() => {
    void app.revision
    void app.postRevision
    const at = offset
    const from = fromDate
    const to = toDate
    const sequence = ++loadSequence
    loading = true
    loadError = ''
    api
      .archivedPosts(PAGE_SIZE, at, from || undefined, to || undefined)
      .then((data) => {
        if (sequence !== loadSequence) return
        items = data.items
        total = data.total
      })
      .catch((error) => {
        if (sequence !== loadSequence) return
        loadError = (error as Error).message
        toast('error', loadError)
      })
      .finally(() => {
        if (sequence === loadSequence) loading = false
      })
  })

  function changeFromDate(value: string) {
    fromDate = value
    offset = 0
  }

  function changeToDate(value: string) {
    toDate = value
    offset = 0
  }

  function archiveTitle(post: ArchivePost): string {
    if (post.title) return post.title
    if (post.remote_post_id != null) {
      return post.archive_date
        ? t('archive.fallback.dated', { date: post.archive_date, id: post.remote_post_id })
        : t('archive.fallback.remote', { id: post.remote_post_id })
    }
    return t('archive.fallback.local', { id: post.id })
  }

  async function renameArchiveFolder(post: Post) {
    renaming = true
    try {
      await api.renameArchiveFolder(post.id)
      renamePost = null
      toast('success', t('archive.rename.done'))
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      renaming = false
    }
  }
</script>

<div class="flex h-full flex-col">
  <header class="flex flex-wrap items-center gap-2 border-b border-ink-800 px-5 py-3">
    <h1 class="text-sm font-semibold">{t('nav.archive')}</h1>
    <span class="mono text-xs text-ink-400">{plural('archive.count', total)}</span>
    <div class="flex-1"></div>
    <div class="flex items-center gap-2">
      <label class="flex items-center gap-1.5 text-xs text-ink-400">
        <span>{t('archive.fromDate')}</span>
        <input
          class="input w-auto"
          type="date"
          value={fromDate}
          onchange={(event) =>
            changeFromDate((event.currentTarget as HTMLInputElement).value)}
        />
      </label>
      <label class="flex items-center gap-1.5 text-xs text-ink-400">
        <span>{t('archive.toDate')}</span>
        <input
          class="input w-auto"
          type="date"
          value={toDate}
          onchange={(event) => changeToDate((event.currentTarget as HTMLInputElement).value)}
        />
      </label>
    </div>
    <ArchiveMoveNow />
  </header>

  <div class="min-h-0 flex-1 overflow-y-auto p-5">
    {#if loadError}
      <p
        class="mb-3 rounded-lg border px-3 py-2 text-xs"
        style="border-color: var(--color-failed); color: var(--color-failed);"
        role="alert"
      >
        {loadError}
      </p>
    {/if}
    {#if loading}
      <Spinner />
    {:else if !items.length}
      <Empty
        icon="archive"
        title={fromDate || toDate ? t('archive.empty.filteredTitle') : t('archive.empty.title')}
        hint={fromDate || toDate ? t('archive.empty.filteredHint') : t('archive.empty.hint')}
      />
    {:else}
      <div>
        {#each items as post, index (post.id)}
          {#if index === 0 || items[index - 1].archive_date !== post.archive_date}
            <h2 class="mono mb-1 mt-4 text-xs font-semibold text-ink-300 first:mt-0">
              {post.archive_date ?? t('archive.unknownDate')}
            </h2>
          {/if}
          <article
            class="mb-1 flex flex-col gap-3 rounded-lg border border-ink-750 bg-ink-850 p-2.5 sm:flex-row sm:items-center"
          >
            <div class="flex h-16 w-full shrink-0 gap-1 overflow-hidden sm:w-[21rem]">
              {#each post.previews as preview (preview.position)}
                {#if preview.image_id != null}
                  <img
                    src={api.thumbnailUrl(preview.image_id, false, preview.thumbnail_path)}
                    alt=""
                    loading="lazy"
                    class="h-16 min-w-0 flex-1 rounded bg-ink-800 object-cover sm:w-16 sm:flex-none"
                  />
                {:else if preview.remote_url}
                  <img
                    src={preview.remote_url}
                    alt=""
                    loading="lazy"
                    class="h-16 min-w-0 flex-1 rounded bg-ink-800 object-cover sm:w-16 sm:flex-none"
                  />
                {:else}
                  <div
                    class="flex h-16 min-w-0 flex-1 items-center justify-center rounded bg-ink-800 text-lg text-ink-500 sm:w-16 sm:flex-none"
                    role="img"
                    aria-label={t('archive.previewUnavailable')}
                    title={t('archive.previewUnavailable')}
                  >
                    ◇
                  </div>
                {/if}
              {/each}
            </div>

            <div class="flex min-w-0 flex-1 flex-col gap-2 sm:flex-row sm:items-center">
              <div class="min-w-0 flex-1">
                <button
                  class="block max-w-full truncate text-left text-xs font-medium text-ink-100 hover:underline"
                  title={archiveTitle(post)}
                  onclick={() => openPost(post.id)}
                >
                  {archiveTitle(post)}
                </button>
                <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                  <span class="mono text-[11px] text-ink-400">
                    {formatDateTime(post.archive_published_at)}
                  </span>
                  <span class="text-[11px] text-ink-500">
                    {plural('post.images', post.image_count ?? 0)}
                  </span>
                  {#if post.archive_folder}
                    <span class="mono max-w-full truncate text-[11px] text-ink-500" title={post.archive_folder}>
                      {t('archive.folder')}: {post.archive_folder}
                    </span>
                  {/if}
                </div>
              </div>
              <div class="flex shrink-0 flex-wrap gap-1.5">
                {#if post.archive_folder_rename}
                  <button class="btn btn-sm" onclick={() => (renamePost = post)}>
                    {t('archive.rename.action')}
                  </button>
                {/if}
                {#if post.remote_url}
                  <a
                    class="btn btn-ghost btn-sm"
                    href={post.remote_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {t('archive.onCivitai')}
                  </a>
                {/if}
              </div>
            </div>
          </article>
        {/each}
      </div>

      {#if total > PAGE_SIZE}
        <div class="mt-5 flex items-center justify-center gap-2">
          <button
            class="btn btn-sm"
            disabled={offset === 0}
            onclick={() => (offset = Math.max(0, offset - PAGE_SIZE))}
          >
            {t('paging.back')}
          </button>
          <span class="mono text-xs text-ink-400">
            {t('paging.range', {
              from: offset + 1,
              to: Math.min(offset + PAGE_SIZE, total),
              total,
            })}
          </span>
          <button
            class="btn btn-sm"
            disabled={offset + PAGE_SIZE >= total}
            onclick={() => (offset += PAGE_SIZE)}
          >
            {t('paging.next')}
          </button>
        </div>
      {/if}
    {/if}
  </div>
</div>

{#if renamePost?.archive_folder_rename}
  {@const plan = renamePost.archive_folder_rename}
  <ConfirmDialog
    title={t('archive.rename.title')}
    confirmLabel={renaming ? t('archive.rename.working') : t('archive.rename.confirm')}
    onCancel={() => (renamePost = null)}
    onConfirm={() => renameArchiveFolder(renamePost!)}
  >
    {#snippet body()}
      <p>{t('archive.rename.body')}</p>
      <dl class="mono space-y-2 rounded-lg border border-ink-750 bg-ink-850 p-3 text-xs">
        <div>
          <dt class="text-ink-500">{t('archive.rename.from')}</dt>
          <dd class="break-all text-ink-200">{plan.from}</dd>
        </div>
        <div>
          <dt class="text-ink-500">{t('archive.rename.to')}</dt>
          <dd class="break-all text-ink-200">{plan.to}</dd>
        </div>
      </dl>
      <p class="text-ink-400">{t('archive.rename.reversible')}</p>
    {/snippet}
  </ConfirmDialog>
{/if}
