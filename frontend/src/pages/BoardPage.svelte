<script lang="ts">
  import { api, translateCode } from '../api/client'
  import DiscoverDialog from '../components/DiscoverDialog.svelte'
  import PostCard from '../components/PostCard.svelte'
  import Empty from '../components/ui/Empty.svelte'
  import { plural, t } from '../i18n/index.svelte'
  import {
    app,
    clearPostSelection,
    invalidate,
    openPost,
    setActiveJob,
    setSelectedPosts,
    setView,
    toast,
    togglePost,
  } from '../state/store.svelte'
  import type { Post, PostState } from '../types/api'

  /**
   * Board columns. Several states share a column where the distinction only
   * matters inside the editor - "on CivitAI but not yet live" is one situation
   * to the user, whatever produced it.
   */
  const COLUMNS: { key: string; label: string; states: PostState[]; accent: string }[] = [
    { key: 'draft', label: 'board.column.draft', states: ['draft'], accent: 'var(--color-draft)' },
    { key: 'ready', label: 'board.column.ready', states: ['ready'], accent: 'var(--color-ready)' },
    {
      key: 'scheduled',
      label: 'board.column.scheduled',
      states: ['scheduled', 'remote_draft', 'pushing'],
      accent: 'var(--color-scheduled)',
    },
    {
      key: 'published',
      label: 'board.column.published',
      states: ['published'],
      accent: 'var(--color-published)',
    },
    {
      key: 'attention',
      label: 'board.column.attention',
      states: ['failed', 'needs_reconcile', 'remote_missing'],
      accent: 'var(--color-failed)',
    },
  ]

  let posts = $state<Post[]>([])
  let loading = $state(true)
  let discovering = $state(false)
  let matching = $state(false)
  let archiving = $state(false)
  //: The cutoffs the archive move offers. It belongs here, on the board, because
  //: this is what it takes posts off - the archive view is where they arrive.
  const CUTOFFS = [30, 90, 365]
  let archiveDays = $state(90)

  const selectedSet = $derived(new Set(app.selectedPosts))

  /**
   * What the archive accepts (`ARC-01`): a post that was verified as published.
   * That is `published`, and also a post CivitAI has lost which demonstrably
   * went out — the publication date separates that from a remote draft deleted
   * before it was ever live. Without the second case a published post that
   * vanishes from CivitAI could never leave the board.
   */
  function archivable(post: Post): boolean {
    if (post.state === 'published') return true
    if (post.state !== 'remote_missing' || !post.remote_published_at) return false
    // In the past, not merely set: a post scheduled on CivitAI and deleted there
    // before it went live keeps its future date, and was never published.
    return new Date(post.remote_published_at).getTime() <= Date.now()
  }

  const archivableSelection = $derived(
    posts.filter((post) => selectedSet.has(post.id) && archivable(post)),
  )

  /** Nothing to look for means nothing to press. */
  const unmatchedImages = $derived(
    posts.reduce((sum, post) => sum + (post.unmatched_images ?? 0), 0),
  )

  $effect(() => {
    void app.revision
    void app.postRevision
    loading = true
    api
      .posts()
      .then((data) => (posts = data.items.filter((post) => post.state !== 'archived')))
      .catch((error) => toast('error', (error as Error).message))
      .finally(() => (loading = false))
  })

  async function move(post: Post, column: string) {
    try {
      if (column === 'ready' && post.state === 'draft') await api.markReady(post.id)
      else if (column === 'draft' && post.state === 'ready') await api.markDraft(post.id)
      else return
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  function onDrop(event: DragEvent, column: string) {
    const id = Number(event.dataTransfer?.getData('text/post-id'))
    const post = posts.find((item) => item.id === id)
    if (post) move(post, column)
  }

  async function archiveOne(post: Post) {
    try {
      await api.archivePost(post.id)
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function archivePublishedPosts() {
    try {
      const result = await api.archivePublished(archiveDays)
      toast('success', plural('archive.moved', result.archived))
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function findLocalFiles() {
    if (!unmatchedImages || matching) return
    matching = true
    try {
      setActiveJob(await api.matchLocalAll())
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      matching = false
    }
  }

  async function archiveSelected() {
    if (!archivableSelection.length || archiving) return
    archiving = true
    try {
      // State only. The files stay where they are until an archive run is
      // started explicitly in the settings (`ARC-04`).
      const result = await api.archiveSelectedPosts(
        archivableSelection.map((post) => post.id),
      )
      toast('success', plural('board.archivedSelected', result.archived))
      for (const refusal of result.refused) {
        toast('error', translateCode(refusal.code, refusal.message, refusal.params))
      }
      clearPostSelection()
      invalidate()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      archiving = false
    }
  }

  async function sync() {
    try {
      setActiveJob(await api.sync())
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }
</script>

<div class="flex h-full flex-col">
  <header class="flex flex-wrap items-center gap-2 border-b border-ink-800 px-5 py-3">
    <h1 class="text-sm font-semibold">{t('nav.board')}</h1>
    <span class="mono text-xs text-ink-400">{t('board.count', { count: posts.length })}</span>
    <div class="flex-1"></div>
    <button class="btn btn-sm" onclick={sync}>{t('board.sync')}</button>
    <button
      class="btn btn-sm"
      disabled={!unmatchedImages || matching}
      title={unmatchedImages
        ? t('board.findLocalHint', { count: unmatchedImages })
        : t('board.findLocalNothing')}
      onclick={findLocalFiles}
    >
      {t('board.findLocal', { count: unmatchedImages })}
    </button>
    <select
      class="input w-auto"
      value={archiveDays}
      aria-label={t('board.archiveOlderLabel')}
      onchange={(event) => (archiveDays = Number((event.currentTarget as HTMLSelectElement).value))}
    >
      {#each CUTOFFS as cutoff (cutoff)}
        <option value={cutoff}>{t('archive.olderThan', { days: cutoff })}</option>
      {/each}
    </select>
    <button class="btn btn-sm" onclick={archivePublishedPosts}>{t('board.archivePublished')}</button>
    <button class="btn btn-sm" onclick={() => (discovering = true)}>{t('board.discover')}</button>
    <button class="btn btn-primary btn-sm" onclick={() => setView('library')}>
      {t('board.newPost')}
    </button>
  </header>

  {#if discovering}
    <DiscoverDialog onClose={() => (discovering = false)} />
  {/if}

  {#if app.selectedPosts.length}
    <div class="animate-in flex items-center gap-2 border-b border-ink-800 bg-ink-850 px-5 py-2.5">
      <span class="text-xs font-medium text-ink-100">
        {t('board.selected', { count: app.selectedPosts.length })}
      </span>
      <button
        class="btn btn-ghost btn-sm"
        onclick={() =>
          setSelectedPosts(posts.filter(archivable).map((post) => post.id))}
      >
        {t('board.selectPublished')}
      </button>
      <button class="btn btn-ghost btn-sm" onclick={clearPostSelection}>
        {t('board.deselect')}
      </button>
      <div class="flex-1"></div>
      <!-- `PRI-04`: the count and what happens are readable before the press. -->
      <span class="text-[11px] text-ink-400">{t('board.archiveExplain')}</span>
      <button
        class="btn btn-primary btn-sm"
        disabled={!archivableSelection.length || archiving}
        title={t('board.archiveHint')}
        onclick={archiveSelected}
      >
        {t('board.archiveSelected', { count: archivableSelection.length })}
      </button>
    </div>
  {/if}

  {#if loading}
    <div class="p-5"><div class="skeleton h-40 w-full"></div></div>
  {:else if !posts.length}
    <Empty icon="board" title={t('board.empty.title')} hint={t('board.empty.hint')} />
  {:else}
    <div class="min-h-0 flex-1 overflow-x-auto p-5">
      <div class="flex h-full min-w-[1100px] gap-3">
        {#each COLUMNS as column (column.key)}
          {@const items = posts.filter((post) => column.states.includes(post.state))}
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div
            class="flex min-w-56 flex-1 flex-col rounded-xl border border-ink-800 bg-ink-900"
            ondragover={(event) => event.preventDefault()}
            ondrop={(event) => onDrop(event, column.key)}
          >
            <header class="flex items-center gap-2 border-b border-ink-800 px-3 py-2.5">
              <span
                class="h-2 w-2 rounded-full"
                style="background: {column.accent};"
                aria-hidden="true"
              ></span>
              <h2 class="flex-1 text-xs font-semibold text-ink-200">{t(column.label)}</h2>
              <span class="mono text-[11px] text-ink-500">{items.length}</span>
            </header>
            <div class="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
              {#each items as post (post.id)}
                <PostCard
                  {post}
                  selected={selectedSet.has(post.id)}
                  onToggle={() => togglePost(post.id)}
                  onOpen={() => openPost(post.id)}
                  onArchive={archivable(post) ? () => archiveOne(post) : undefined}
                />
              {/each}
              {#if !items.length}
                <p class="py-6 text-center text-[11px] text-ink-600">{t('board.columnEmpty')}</p>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    </div>
  {/if}
</div>
