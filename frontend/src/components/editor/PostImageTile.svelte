<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { inspectImage } from '../../state/store.svelte'
  import type { PostImage } from '../../types/api'

  let {
    image,
    index,
    readOnly,
    isPlaceholder,
    onDragStart,
    onDragEnd,
    onAck,
    onHideMeta,
    onMatchManual,
    onRemove,
  }: {
    image: PostImage
    index: number
    readOnly: boolean
    isPlaceholder: boolean
    onDragStart: () => void
    onDragEnd: () => void
    onAck: () => void
    onHideMeta: () => void
    onMatchManual?: () => void
    onRemove: () => void
  } = $props()

  const isVideo = $derived(
    (image.media_type && image.media_type !== 'image') ||
      Boolean(
        image.content_type?.startsWith('video/') || image.content_type?.startsWith('audio/'),
      ),
  )

  /**
   * The original file rather than the downscaled view.
   *
   * The stored address ends in a transform (`width=450`); for a video that is a
   * still frame, not the file. Opening it needs the original.
   */
  function originalUrl(url: string): string {
    return url.replace(/\/[^/]*$/, '/original=true')
  }

  function startDrag(event: DragEvent) {
    if (readOnly) return
    // Firefox starts no drag at all unless the transfer carries something, and
    // 'move' is what makes the cursor say what this gesture does.
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move'
      event.dataTransfer.setData('text/plain', String(image.id))
    }
    onDragStart()
  }
</script>

<figure
  draggable={!readOnly}
  ondragstart={startDrag}
  ondragend={onDragEnd}
  class="group relative overflow-hidden rounded-xl border {image.duplicates?.has_exact &&
  !image.dedup_ack
    ? 'border-[color-mix(in_srgb,var(--color-failed)_55%,transparent)]'
    : 'border-ink-750'} {readOnly ? '' : 'cursor-grab active:cursor-grabbing'} {isPlaceholder
    ? 'placeholder'
    : ''}"
>
  {#if isPlaceholder}
    {#if image.image_id}
      <img
        src={api.thumbnailUrl(image.image_id, false, image.thumbnail_path)}
        alt=""
        class="placeholder-thumbnail aspect-[3/4] w-full bg-ink-850 object-cover"
      />
    {:else if image.remote_url}
      <img
        src={image.remote_url}
        alt=""
        class="placeholder-thumbnail aspect-[3/4] w-full bg-ink-850 object-cover"
      />
    {:else}
      <div class="aspect-[3/4] w-full bg-ink-850"></div>
    {/if}
  <!-- Videos are not fetched by this app - it shows that they exist and links
       them. Everything else the user does outside. -->
  {:else if image.is_missing}
    <div
      class="flex aspect-[3/4] w-full flex-col items-center justify-center gap-2 bg-ink-850 px-3 text-center"
    >
      <span class="text-2xl text-amber-300" aria-hidden="true">!</span>
      <p class="text-xs text-ink-300">{t('editor.sourceNotFound')}</p>
      <p class="text-[10px] leading-tight text-ink-500">{t('editor.sourceUnavailable')}</p>
    </div>
  {:else if isVideo && !image.image_id}
    <div
      class="flex aspect-[3/4] w-full flex-col items-center justify-center gap-2 bg-ink-850 px-2 text-center"
    >
      <span class="text-2xl opacity-50" aria-hidden="true">▶</span>
      <p class="text-[11px] text-ink-300">{t('editor.video')}</p>
      <p class="text-[10px] leading-tight text-ink-500">{t('editor.videoNotFetched')}</p>
      {#if image.remote_url}
        <a
          class="btn btn-sm"
          href={originalUrl(image.remote_url)}
          target="_blank"
          rel="noreferrer"
        >
          {t('editor.open')}
        </a>
      {/if}
    </div>
  {:else if image.image_id}
    <img
      src={api.thumbnailUrl(image.image_id, false, image.thumbnail_path)}
      alt=""
      class="aspect-[3/4] w-full bg-ink-850 object-cover"
    />
  {:else if image.remote_url}
    <img
      src={image.remote_url}
      alt=""
      loading="lazy"
      class="aspect-[3/4] w-full bg-ink-850 object-cover"
    />
  {:else}
    <div class="flex aspect-[3/4] w-full items-center justify-center bg-ink-850 text-xs text-ink-500">
      {t('editor.fileMissing')}
    </div>
  {/if}

  {#if !isPlaceholder && ((!image.image_id && image.remote_url) || image.remote_on_site === true)}
    <div class="absolute inset-x-1.5 top-8 flex flex-col gap-1">
      {#if !image.image_id && image.remote_url}
        <span
          class="rounded px-1.5 py-0.5 text-center text-[10px]"
          style="background: rgb(0 0 0 / 0.75); color: var(--color-ink-300);"
          title={t('editor.remoteOnlyHint')}
        >
          {t('editor.remoteOnly')}
        </span>
      {/if}
      {#if image.remote_on_site === true}
        <span
          class="rounded px-1.5 py-0.5 text-center text-[10px]"
          style="background: color-mix(in srgb, var(--color-accent-500) 75%, black); color: #d7fffa;"
          title={t('editor.generatedOnCivitaiHint')}
        >
          {t('editor.generatedOnCivitai')}
        </span>
      {/if}
    </div>
  {/if}

  <!-- The number stays on the placeholder as well. During a drag the grid shows
       the order that will apply, and the position the moved image is about to
       take is the answer somebody is dragging for - not least whether it
       becomes the cover. A gap with no number would also read as a broken
       tile rather than a reserved slot. -->
  <span
    class="absolute left-1.5 top-1.5 flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold"
    style="background: {index === 0 ? 'var(--color-accent-500)' : 'rgb(0 0 0 / 0.7)'};
           color: {index === 0 ? '#04211f' : 'var(--color-ink-200)'};"
    title={index === 0 ? t('editor.coverImage') : ''}
  >
    {index + 1}
  </span>

  {#if !isPlaceholder && image.hide_meta}
    <span
      class="absolute right-1.5 top-1.5 rounded px-1.5 py-0.5 text-[10px]"
      style="background: rgb(0 0 0 / 0.75); color: var(--color-ink-300);"
      title={t('editor.hideMetaHint')}
    >
      🚫 {t('editor.meta')}
    </span>
  {/if}

  <!-- Only for an image that exists on CivitAI. Unknown until CivitAI has
       looked at it, which can take minutes - and unknown is not PG. -->
  {#if !isPlaceholder && image.remote_image_id}
    <span
      class="absolute bottom-1.5 right-1.5 rounded px-1.5 py-0.5 text-[10px] font-semibold"
      style="background: rgb(0 0 0 / 0.75); color: var(--color-ink-300);"
      title={image.nsfw_label
        ? t('editor.ratingHint', { rating: image.nsfw_label })
        : t('editor.ratingUnknownHint')}
    >
      {image.nsfw_label ?? t('editor.ratingUnknown')}
    </span>
  {/if}

  {#if !isPlaceholder && image.edited}
    <span
      class="absolute left-8 top-1.5 rounded px-1.5 py-0.5 text-[10px]"
      style="background: color-mix(in srgb, var(--color-accent-500) 85%, black); color: #04211f;"
      title={t('metadata.edited')}
    >
      ✎ {t('metadata.edited')}
    </span>
  {/if}

  {#if !isPlaceholder && !readOnly && image.duplicates?.has_exact}
    <div class="absolute inset-x-0 top-0 bg-black/85 px-2 py-1.5">
      <p class="text-[10px] leading-tight" style="color: #fca5a5;">{t('editor.wasInAPost')}</p>
      <button class="btn btn-sm mt-1 w-full justify-center" onclick={onAck}>
        {image.dedup_ack ? `✓ ${t('editor.acknowledged')}` : t('editor.useAnyway')}
      </button>
    </div>
  {/if}

  <!-- With all four controls in one row the match label is squeezed to a few
       truncated letters - it is the only one with a sentence in it. So it takes
       the whole first line and the three short ones share the second. -->
  {#if !isPlaceholder && !readOnly}
    <div
      class="absolute inset-x-1.5 bottom-1.5 gap-1 opacity-0 transition-opacity group-hover:opacity-100 {onMatchManual && image.image_id ? 'grid grid-cols-3' : 'flex flex-wrap'}"
    >
    {#if onMatchManual}
      <button
        class="btn btn-sm min-w-0 max-w-full flex-1 justify-center truncate {image.image_id ? 'col-span-3' : ''}"
        onclick={onMatchManual}
      >
        {image.image_id ? t('editor.changeManualMatch') : t('editor.matchManually')}
      </button>
    {/if}
    {#if image.image_id}
      <button
        class="btn btn-sm {onMatchManual ? '' : 'flex-1'} justify-center"
        onclick={() => inspectImage(image.image_id as number)}
      >
        {t('editor.meta')}
      </button>
    {/if}
    <button class="btn btn-sm justify-center" title={t('editor.hideMetaHint')} onclick={onHideMeta}>
      {image.hide_meta ? '👁' : '🚫'}
    </button>
    <button class="btn btn-sm btn-danger justify-center" onclick={onRemove}>✕</button>
    </div>
  {/if}
</figure>

<style>
  .placeholder {
    border-color: transparent;
    background: var(--color-ink-850);
    box-shadow:
      inset 0 0 0 1px var(--color-ink-700),
      inset 0 0.5rem 1.5rem rgb(0 0 0 / 0.55);
  }

  .placeholder-thumbnail {
    opacity: 0.18;
    pointer-events: none;
  }
</style>
