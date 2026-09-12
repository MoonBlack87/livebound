<script lang="ts">
  import { api, translateIssue } from '../api/client'
  import { plural, t } from '../i18n/index.svelte'
  import {
    app,
    invalidate,
    invalidatePosts,
    openPost,
    setActiveJob,
    toast,
  } from '../state/store.svelte'
  import ImagePicker from './ImagePicker.svelte'
  import ModelBinding from './ModelBinding.svelte'
  import ScheduleField from './ScheduleField.svelte'
  import TagInput from './TagInput.svelte'
  import ConfirmDialog from './ui/ConfirmDialog.svelte'
  import IssueRow from './ui/IssueRow.svelte'
  import Spinner from './ui/Spinner.svelte'
  import StateBadge from './ui/StateBadge.svelte'
  import { formatDateTime } from './ui/format.svelte'
  import LlmButton from './editor/LlmButton.svelte'
  import PostImageTile from './editor/PostImageTile.svelte'
  import BulkEditDialog from './editor/BulkEditDialog.svelte'
  import ReconcilePanel from './editor/ReconcilePanel.svelte'
  import SuggestionRow from './editor/SuggestionRow.svelte'
  import type { ManualMatchCandidate, Post, PostImage, Suggestion } from '../types/api'

  let { postId }: { postId: number } = $props()

  let post = $state<Post | null>(null)
  let suggestions = $state<Suggestion[]>([])
  let busy = $state(false)
  let confirm = $state<null | 'remote-delete' | 'rebuild' | 'delete'>(null)
  let picking = $state(false)
  let matchingImage = $state<PostImage | null>(null)
  let matchSuggestions = $state<ManualMatchCandidate[]>([])
  let matchSuggestionsLoading = $state(false)
  let matchSuggestionsUnavailable = $state(false)
  let editingMetadata = $state(false)
  //: The post_image being dragged, and the order the grid is showing while it
  //: is in the air. Identities, not positions: positions move under a reflow.
  let dragId = $state<number | null>(null)
  let dragOrder = $state<number[] | null>(null)
  let dragStartOrder = $state<number[] | null>(null)
  //: Set one frame after dragstart. The browser snapshots the dragged element
  //: to paint under the cursor, and an element changed inside the dragstart
  //: handler is either snapshotted already-dimmed or cancels the drag outright.
  let dragArmed = $state(false)
  let gridEl = $state<HTMLDivElement | undefined>(undefined)
  let textEditPostId: number | null = null

  type Invalidation = 'posts' | 'global'
  type RunOptions = {
    invalidation: Invalidation
    editor: 'response' | 'reload' | 'closed'
    message?: string
  }

  const images = $derived(post?.images ?? [])
  const previewImages = $derived.by(() => {
    const currentOrder = images.map((row) => row.id)
    if (!dragOrder || !dragStartOrder || !sameOrder(dragStartOrder, currentOrder)) return images
    const byId = new Map(images.map((row) => [row.id, row]))
    return dragOrder.map((id) => byId.get(id)).filter((row) => row !== undefined)
  })
  const checklist = $derived(post?.checklist)
  const readOnly = $derived(post?.state === 'pushing')
  // Without MediaDelete the remote-delete path always 403s; saying so up front
  // beats offering a button that cannot work.
  const canDelete = $derived(
    !app.account?.scopes?.known ||
      !app.account.scopes.problems.some((problem) => problem.scope === 'MediaDelete'),
  )

  async function reload() {
    try {
      const [next, suggested] = await Promise.all([
        api.post(postId),
        api.llmSuggestions(postId).catch(() => ({ items: [] as Suggestion[] })),
      ])
      post = next
      suggestions = suggested.items.filter((item) => !item.accepted)
    } catch (error) {
      toast('error', (error as Error).message)
      openPost(null)
    }
  }

  $effect(() => {
    // Global jobs can affect suggestions too; post-only jobs change the remote
    // state, divergence and ratings shown in this editor.
    void app.revision
    void app.postRevision
    void postId
    reload()
  })

  function beginTextEdit() {
    textEditPostId = postId
  }

  // The tag input reports its own blur 140 ms late, so that closing the editor
  // by clicking the backdrop lands first and this callback runs against a post
  // that is no longer open. The id is therefore taken when the edit starts, and
  // a save without one does not go out at all.
  function saveTags(id: number | null, tags: string[]) {
    if (id == null) return
    run(() => api.setPostTags(id, tags), { invalidation: 'posts', editor: 'response' })
  }

  async function patch(id: number | null, body: Record<string, unknown>) {
    if (id == null) return
    busy = true
    try {
      post = await api.patchPost(id, body)
      invalidatePosts()
    } catch (error) {
      toast('error', (error as Error).message)
      reload()
    } finally {
      busy = false
    }
  }

  async function dismissSuggestion(id: number) {
    busy = true
    try {
      await api.llmDismiss(id)
      suggestions = suggestions.filter((item) => item.id !== id)
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = false
    }
  }

  async function run(fn: () => Promise<unknown>, options: RunOptions) {
    busy = true
    try {
      const result = await fn()
      if (options.editor === 'response') post = result as Post
      else if (options.editor === 'reload') await reload()
      if (options.invalidation === 'posts') invalidatePosts()
      else invalidate()
      if (options.message) toast('success', options.message)
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = false
    }
  }

  function imageIds(): number[] {
    return images.map((image) => image.image_id).filter((id): id is number => id != null)
  }

  function sameOrder(left: number[], right: number[]): boolean {
    return left.length === right.length && left.every((id, index) => id === right[index])
  }

  function startDrag(imageId: number) {
    const order = images.map((row) => row.id)
    dragId = imageId
    dragOrder = order
    dragStartOrder = order
    dragArmed = false
    requestAnimationFrame(() => {
      if (dragId === imageId) dragArmed = true
    })
  }

  function endDrag() {
    dragId = null
    dragOrder = null
    dragStartOrder = null
    dragArmed = false
  }

  $effect(() => {
    const currentOrder = images.map((row) => row.id)
    if (dragStartOrder && !sameOrder(dragStartOrder, currentOrder)) endDrag()
  })

  /**
   * Move the dragged tile to whichever slot the pointer is nearest.
   *
   * Geometry rather than dragenter/dragleave, because the grid reflows under
   * the pointer and those events then arrive in storms - which is what made an
   * earlier attempt oscillate between two arrangements while the mouse stood
   * still. Distance answers the same question without an event per crossing.
   *
   * It also settles itself: once the tile has moved to the nearest slot, the
   * nearest tile *is* the tile in hand, so the next dragover changes nothing.
   */
  function dragOverGrid(event: DragEvent) {
    if (dragId === null || !dragOrder || !gridEl) return
    event.preventDefault()
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'

    const tiles = [...gridEl.children] as HTMLElement[]
    let nearest = -1
    let shortest = Number.POSITIVE_INFINITY
    tiles.forEach((tile, index) => {
      const box = tile.getBoundingClientRect()
      const dx = event.clientX - (box.left + box.width / 2)
      const dy = event.clientY - (box.top + box.height / 2)
      const distance = dx * dx + dy * dy
      if (distance < shortest) {
        shortest = distance
        nearest = index
      }
    })

    const current = dragOrder.indexOf(dragId)
    if (nearest < 0 || current < 0 || nearest === current) return
    const next = [...dragOrder]
    const [moved] = next.splice(current, 1)
    next.splice(nearest, 0, moved)
    dragOrder = next
  }

  function dropOnGrid(event: DragEvent) {
    if (dragId === null || !dragOrder) return
    event.preventDefault()
    commitOrder(dragOrder)
    endDrag()
  }

  function commitOrder(order: number[]) {
    const current = images.map((row) => row.id)
    if (sameOrder(order, current)) return
    run(() => api.setPostImageOrder(postId, order), {
      invalidation: 'posts',
      editor: 'response',
    })
  }

  async function pushThis() {
    try {
      setActiveJob(await api.push([postId]))
      openPost(null)
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function fetchMissing() {
    try {
      setActiveJob(await api.fetchImages(postId))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function openManualMatch(image: PostImage) {
    matchingImage = image
    matchSuggestions = []
    matchSuggestionsUnavailable = false
    matchSuggestionsLoading = true
    try {
      const result = await api.matchCandidates(postId, image.id)
      if (matchingImage?.id === image.id) matchSuggestions = result.items
    } catch {
      if (matchingImage?.id === image.id) matchSuggestionsUnavailable = true
    } finally {
      if (matchingImage?.id === image.id) matchSuggestionsLoading = false
    }
  }
</script>

{#if !post}
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/70"><Spinner /></div>
{:else}
  {@const current = post}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
    onmousedown={(event) => event.target === event.currentTarget && openPost(null)}
  >
    <div class="panel-raised animate-in flex h-full max-h-[94vh] w-full max-w-6xl flex-col">
      <header class="flex items-center gap-3 border-b border-ink-700 px-5 py-3">
        <StateBadge state={current.state} />
        <h2 class="min-w-0 flex-1 truncate text-sm font-semibold">
          {#if current.title}{current.title}{:else}<span class="text-ink-500">
              {t('post.untitled')}
            </span>{/if}
        </h2>
        {#if busy}<Spinner />{/if}
        {#if current.remote_url}
          <a class="btn btn-sm" href={current.remote_url} target="_blank" rel="noreferrer">
            {t('editor.onCivitai')}
          </a>
        {/if}
        <button class="btn btn-ghost btn-sm" onclick={() => openPost(null)}>✕</button>
      </header>

      {#if current.last_error}
        <div
          class="border-b px-5 py-2.5 text-xs"
          style="background: color-mix(in srgb, var(--color-failed) 12%, transparent);
                 border-color: color-mix(in srgb, var(--color-failed) 35%, transparent);
                 color: var(--color-ink-200);"
        >
          {current.last_error}
        </div>
      {/if}

      {#if current.remote_diverged}
        <div
          class="flex items-center gap-3 border-b px-5 py-2.5 text-xs"
          style="background: color-mix(in srgb, var(--color-attention) 12%, transparent);
                 border-color: color-mix(in srgb, var(--color-attention) 35%, transparent);"
        >
          <span class="flex-1 text-ink-200">{t('editor.diverged')}</span>
          <button
            class="btn btn-sm"
            disabled={readOnly}
            onclick={() =>
              run(() => api.adoptSnapshot(postId), {
                invalidation: 'posts',
                editor: 'response',
              })}
          >
            {t('editor.takeRemote')}
          </button>
          <button
            class="btn btn-sm"
            disabled={readOnly}
            onclick={() =>
              run(() => api.pushText(postId), {
                invalidation: 'global',
                editor: 'response',
              })}
          >
            {t('editor.sendLocal')}
          </button>
        </div>
      {/if}

      <div class="grid min-h-0 flex-1 grid-cols-[1fr_380px] overflow-hidden">
        <!-- --- images ------------------------------------------------- -->
        <div class="min-h-0 overflow-y-auto border-r border-ink-800 p-5">
          <div class="mb-3 flex items-center justify-between gap-2">
            <p class="label mb-0">{t('editor.imagesLabel', { count: images.length })}</p>
            <div class="flex items-center gap-2">
              {#if images.some((image) => !image.image_id && image.remote_url)}
                <button
                  class="btn btn-sm"
                  disabled={readOnly}
                  title={t('editor.matchLocalHint')}
                  onclick={() =>
                    run(() => api.matchLocal(postId), {
                      invalidation: 'global',
                      editor: 'response',
                      message: t('editor.matchLocalDone'),
                    })}
                >
                  {t('editor.matchLocal')}
                </button>
                <button class="btn btn-sm" title={t('editor.downloadHint')} onclick={fetchMissing}>
                  {t('editor.download')}
                </button>
              {/if}
              <button class="btn btn-sm" disabled={readOnly} onclick={() => (picking = true)}>
                {t('editor.addImages')}
              </button>
              {#if imageIds().length}
                <button
                  class="btn btn-sm"
                  disabled={readOnly}
                  onclick={() => (editingMetadata = true)}
                >
                  {t('metadata.bulkEdit')}
                </button>
              {/if}
            </div>
          </div>

          {#if images.some((image) => image.is_missing)}
            <div
              class="mb-3 rounded-lg border border-amber-500/40 bg-amber-950/30 px-3 py-2 text-xs text-amber-200"
            >
              {t('editor.sourceUnavailable')}
            </div>
          {/if}

          {#if !images.length}
            <button
              class="w-full rounded-lg border border-dashed border-ink-700 py-10 text-center text-xs text-ink-500 transition-colors hover:border-ink-600 hover:text-ink-300"
              onclick={() => (picking = true)}
            >
              {t('editor.noImages')}
            </button>
          {:else}
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div
              bind:this={gridEl}
              class="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3"
              ondragover={dragOverGrid}
              ondrop={dropOnGrid}
            >
              {#each previewImages as image, index (image.id)}
                <PostImageTile
                  {image}
                  {index}
                  readOnly={readOnly ?? false}
                  isPlaceholder={!readOnly && dragArmed && dragId === image.id}
                  onDragStart={() => startDrag(image.id)}
                  onDragEnd={endDrag}
                  onAck={() =>
                    run(
                      () => api.ackDuplicate(postId, image.id, !image.dedup_ack),
                      {
                        invalidation: 'posts',
                        editor: 'reload',
                        message: image.dedup_ack ? undefined : t('editor.dupeAcked'),
                      },
                    )}
                  onHideMeta={() =>
                    run(() => api.hideMeta(postId, image.id, !image.hide_meta), {
                      invalidation: 'posts',
                      editor: 'reload',
                    })}
                  onMatchManual={current.origin === 'imported' && image.remote_url && !readOnly
                    ? () => openManualMatch(image)
                    : undefined}
                  onRemove={() =>
                    run(
                      () =>
                        api.setPostImageOrder(
                          postId,
                          images.filter((row) => row.id !== image.id).map((row) => row.id),
                        ),
                      { invalidation: 'posts', editor: 'response' },
                    )}
                />
              {/each}
            </div>
          {/if}
        </div>

        <!-- --- form --------------------------------------------------- -->
        <div class="min-h-0 space-y-4 overflow-y-auto p-5">
          <!-- One button for the whole form: a single generation answers title,
               description and tags together, so a button per field asked the
               model three times and threw two thirds of every answer away. -->
          <div class="flex items-center justify-end">
            <LlmButton {postId} />
          </div>

          <div>
            <span class="label mb-0">{t('editor.title')}</span>
            <input
              class="input mt-1.5"
              value={current.title}
              disabled={readOnly}
              onfocus={beginTextEdit}
              onblur={(event) =>
                patch(textEditPostId, { title: (event.currentTarget as HTMLInputElement).value })}
            />
            <SuggestionRow
              suggestions={suggestions.filter((item) => item.field === 'title')}
              disabled={readOnly}
              onAccept={(id) =>
                run(() => api.llmAccept(id), { invalidation: 'posts', editor: 'reload' })}
              onDismiss={dismissSuggestion}
            />
          </div>

          <div>
            <span class="label mb-0">{t('editor.description')}</span>
            <textarea
              class="input mt-1.5 min-h-24 resize-y"
              value={current.detail}
              disabled={readOnly}
              onfocus={beginTextEdit}
              onblur={(event) =>
                patch(textEditPostId, { detail: (event.currentTarget as HTMLTextAreaElement).value })}
            ></textarea>
            <SuggestionRow
              suggestions={suggestions.filter((item) => item.field === 'description')}
              disabled={readOnly}
              onAccept={(id) =>
                run(() => api.llmAccept(id), { invalidation: 'posts', editor: 'reload' })}
              onDismiss={dismissSuggestion}
            />
          </div>

          <div>
            <span class="label mb-0">{t('editor.tags')}</span>
            <div class="mt-1.5">
              <TagInput
                tags={current.tags}
                disabled={readOnly}
                onBeginEdit={beginTextEdit}
                onChange={(tags) => saveTags(textEditPostId, tags)}
              />
            </div>
            <SuggestionRow
              suggestions={suggestions.filter((item) => item.field === 'tags')}
              disabled={readOnly}
              onAccept={(id) =>
                run(() => api.llmAccept(id), { invalidation: 'posts', editor: 'reload' })}
              onDismiss={dismissSuggestion}
            />
          </div>

          <ModelBinding
            post={current}
            disabled={readOnly}
            onChange={(body) => patch(postId, body)}
            onRebuild={() => (confirm = 'rebuild')}
          />

          <ScheduleField
            post={current}
            disabled={readOnly || !current.can_schedule}
            minLeadMinutes={app.settings?.limits.min_schedule_minutes ?? 60}
            onChange={(body) => patch(postId, body)}
          />

          {#if checklist}
            <div class="rounded-lg border border-ink-750 bg-ink-850 p-3">
              <p class="label mb-2">{t('review.checklist')}</p>
              {#if !checklist.issues.length}
                <IssueRow level="info">{t('editor.allGood')}</IssueRow>
              {:else}
                <div class="space-y-1.5">
                  {#each checklist.issues as issue, index (index)}
                    <IssueRow level={issue.level}>{translateIssue(issue)}</IssueRow>
                  {/each}
                </div>
              {/if}
            </div>
          {/if}

          {#if current.has_remote}
            <div class="space-y-1.5 rounded-lg border border-ink-750 bg-ink-850 p-3">
              <p class="label mb-1">{t('editor.remoteSection')}</p>
              <p class="mono text-[11px] text-ink-300">
                {t('drawer.dupe.post', { id: current.remote_post_id ?? '?' })} · {current.remote_state}
              </p>
              {#if current.remote_published_at}
                <p class="text-[11px] text-ink-400">
                  publishedAt {formatDateTime(current.remote_published_at)}
                </p>
              {/if}
              <div class="flex flex-wrap gap-1.5 pt-1">
                <button
                  class="btn btn-sm"
                  disabled={readOnly}
                  onclick={() =>
                    run(() => api.syncPost(postId), {
                      invalidation: 'global',
                      editor: 'response',
                    })}
                >
                  {t('editor.reconcile')}
                </button>
                {#if current.can_edit_remote}
                  <button
                    class="btn btn-sm"
                    disabled={readOnly}
                    onclick={() =>
                      run(() => api.pushText(postId), {
                        invalidation: 'global',
                        editor: 'response',
                      })}
                  >
                    {t('editor.sendText')}
                  </button>
                {/if}
                <button
                  class="btn btn-sm btn-danger"
                  disabled={readOnly || !canDelete}
                  title={canDelete ? '' : t('editor.noDeleteScope')}
                  onclick={() => (confirm = 'remote-delete')}
                >
                  {t('editor.remoteDelete')}
                </button>
              </div>
            </div>
          {/if}

          {#if current.state === 'needs_reconcile'}
            <ReconcilePanel {postId} onDone={invalidate} />
          {/if}
        </div>
      </div>

      <footer class="flex items-center gap-2 border-t border-ink-700 px-5 py-3">
        <button
          class="btn btn-danger btn-sm"
          disabled={readOnly}
          onclick={() => (confirm = 'delete')}
        >
          {t('editor.deletePost')}
        </button>
        <div class="flex-1"></div>
        {#if current.state === 'draft'}
          <button
            class="btn btn-sm"
            disabled={readOnly}
            onclick={() =>
              run(() => api.markReady(postId), {
                invalidation: 'posts',
                editor: 'response',
              })}
          >
            {t('editor.markReady')}
          </button>
        {/if}
        {#if current.state === 'ready'}
          <button
            class="btn btn-sm"
            onclick={() =>
              run(() => api.markDraft(postId), {
                invalidation: 'posts',
                editor: 'response',
              })}
          >
            {t('editor.markDraft')}
          </button>
        {/if}
        <button
          class="btn btn-primary"
          disabled={!checklist?.can_push || readOnly}
          title={checklist?.can_push ? '' : t('editor.checklistOpen')}
          onclick={pushThis}
        >
          {current.has_remote ? t('editor.pushAgain') : t('editor.push')}
        </button>
      </footer>
    </div>

    {#if picking}
      <ImagePicker
        exclude={imageIds()}
        onCancel={() => (picking = false)}
        onPick={(picked) => {
          picking = false
          // Appended, never reordered: the existing sequence is the author's.
          run(
            () => api.setPostImages(postId, [...imageIds(), ...picked]),
            {
              invalidation: 'posts',
              editor: 'response',
              message: t('editor.imagesAdded', { images: plural('post.images', picked.length) }),
            },
          )
        }}
      />
    {/if}

    {#if matchingImage}
      {@const remoteImage = matchingImage}
      <ImagePicker
        exclude={images
          .filter((image) => image.id !== remoteImage.id)
          .map((image) => image.image_id)
          .filter((id): id is number => id != null)}
        single
        title={t('picker.matchTitle')}
        confirmLabel={t('picker.matchAction')}
        referenceUrl={remoteImage.remote_url}
        suggestions={matchSuggestions}
        suggestionsLoading={matchSuggestionsLoading}
        suggestionsUnavailable={matchSuggestionsUnavailable}
        onCancel={() => (matchingImage = null)}
        onPick={(picked) => {
          const imageId = picked[0]
          if (imageId == null) return
          const postImageId = remoteImage.id
          matchingImage = null
          run(() => api.matchLocalManually(postId, postImageId, imageId), {
            invalidation: 'global',
            editor: 'response',
            message: t('editor.manualMatchDone'),
          })
        }}
      />
    {/if}

    {#if editingMetadata}
      <BulkEditDialog
        imageIds={imageIds()}
        {postId}
        count={imageIds().length}
        {readOnly}
        onClose={() => (editingMetadata = false)}
        onApplied={() => {
          editingMetadata = false
          invalidate()
        }}
      />
    {/if}

    {#if confirm === 'delete'}
      <ConfirmDialog
        title={t('editor.deletePost')}
        danger
        confirmLabel={t('editor.deleteLocal')}
        onCancel={() => (confirm = null)}
        onConfirm={() => {
          confirm = null
          run(
            async () => {
              await api.deletePost(postId)
              openPost(null)
            },
            { invalidation: 'global', editor: 'closed' },
          )
        }}
      >
        {#snippet body()}
          <p>{t('editor.deleteBody1')}</p>
          {#if current.has_remote}
            <p class="text-ink-400">{t('editor.deleteBody2')}</p>
          {/if}
          <p class="text-ink-400">{t('editor.deleteBody3')}</p>
        {/snippet}
      </ConfirmDialog>
    {/if}

    {#if confirm === 'remote-delete'}
      <ConfirmDialog
        title={t('editor.remoteDeleteTitle')}
        danger
        requireKey="ui.confirm.word.delete"
        confirmLabel={t('editor.remoteDeleteAction')}
        onCancel={() => (confirm = null)}
        onConfirm={() => {
          confirm = null
          run(() => api.remoteDelete(postId), {
            invalidation: 'global',
            editor: 'response',
            message: t('editor.remoteDeleted'),
          })
        }}
      >
        {#snippet body()}
          <p>{t('editor.remoteDeleteBody1', { id: current.remote_post_id ?? '?' })}</p>
          <p class="text-ink-400">{t('editor.remoteDeleteBody2')}</p>
        {/snippet}
      </ConfirmDialog>
    {/if}

    {#if confirm === 'rebuild'}
      <ConfirmDialog
        title={t('editor.rebuildTitle')}
        danger
        requireKey="ui.confirm.word.rebuild"
        confirmLabel={t('editor.rebuildAction')}
        onCancel={() => (confirm = null)}
        onConfirm={() => {
          confirm = null
          run(() => api.rebuild(postId), {
            invalidation: 'global',
            editor: 'response',
            message: t('editor.rebuilt'),
          })
        }}
      >
        {#snippet body()}
          <p>{t('editor.rebuildBody1')}</p>
          <p class="text-ink-400">{t('editor.rebuildBody2')}</p>
        {/snippet}
      </ConfirmDialog>
    {/if}
  </div>
{/if}
