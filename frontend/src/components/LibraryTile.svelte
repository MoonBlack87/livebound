<script lang="ts">
  import { api } from '../api/client'
  import { plural, t } from '../i18n/index.svelte'
  import { formatBytes } from './ui/format.svelte'
  import type { ImageRow } from '../types/api'

  let {
    image,
    selected,
    large = false,
    onclick,
    onInspect,
    onOpenGroup,
  }: {
    image: ImageRow
    selected: boolean
    /** The two bigger grid steps show the whole picture, not a 3:4 crop. */
    large?: boolean
    onclick: (event: MouseEvent) => void
    onInspect: () => void
    onOpenGroup: () => void
  } = $props()
</script>

<figure
  class="group relative overflow-hidden rounded-xl border transition-all {selected
    ? 'border-accent-400 ring-2 ring-accent-500/40'
    : image.is_missing
      ? 'border-amber-500/60'
      : 'border-ink-750 hover:border-ink-600'}"
>
  <figcaption
    class="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 via-black/60 to-transparent px-2 pb-1.5 pt-6"
  >
    <p class="truncate text-[11px] text-ink-100" title={image.relative_path}>
      {image.relative_path.split('/').pop()}
    </p>
    <p class="mono truncate text-[10px] text-ink-400">
      {image.width}×{image.height} · {formatBytes(image.file_size)}{image.quick.seed
        ? ` · seed ${image.quick.seed}`
        : ''}
    </p>
  </figcaption>

  <button type="button" {onclick} class="block w-full cursor-pointer" aria-pressed={selected}>
    {#if image.is_missing}
      <span
        class="flex w-full flex-col items-center justify-center gap-2 bg-ink-850 px-3 text-center text-ink-400 {large
          ? 'min-h-56'
          : 'aspect-[3/4]'}"
      >
        <span class="text-2xl text-amber-300" aria-hidden="true">!</span>
        <span class="text-xs">{t('library.sourceNotFound')}</span>
      </span>
    {:else}
      <img
        src={api.thumbnailUrl(image.id, large, image.thumbnail_path)}
        alt={image.relative_path}
        loading="lazy"
        class="w-full bg-ink-850 {large ? 'object-contain' : 'aspect-[3/4] object-cover'}"
      />
    {/if}
  </button>

  <!-- Publishing history, exact copies in the library and metadata are three
       separate facts. None stands in for another. -->
  <div class="pointer-events-none absolute left-1.5 top-1.5 flex flex-col gap-1">
    {#if image.is_missing}
      <span
        class="rounded px-1.5 py-0.5 text-[10px] font-semibold backdrop-blur-sm"
        style="background: rgb(180 83 9 / 0.9); color: #fff;"
        title={t('library.sourceUnavailable')}
      >
        ! {t('library.sourceNotFound')}
      </span>
    {/if}
    {#if image.duplicate_count > 1}
      {@const confidence = t(`duplicates.confidence.${image.duplicate_confidence}`)}
      <button
        type="button"
        class="pointer-events-auto rounded px-1.5 py-0.5 text-left text-[10px] font-semibold backdrop-blur-sm"
        style="background: {image.duplicate_confidence === 'probable'
          ? 'rgb(217 119 6 / 0.9)'
          : 'rgb(8 145 178 / 0.9)'}; color: #fff;"
        onclick={onOpenGroup}
        title={t('library.badge.stack', {
          confidence,
          count: image.duplicate_count,
        })}
      >
        ▱ {confidence} · {plural('library.badge.files', image.duplicate_count)}
      </button>
    {/if}
    {#if image.used}
      <span
        class="rounded px-1.5 py-0.5 text-[10px] font-semibold backdrop-blur-sm"
        style="background: rgb(220 38 38 / 0.85); color: #fff;"
      >
        ✕ {t('library.badge.posted')}
      </span>
    {:else if image.planned}
      <!-- Deliberate order: posted beats planned beats similar. The first is
           final, the second is intent that can still be undone, the third is
           only a hint - so the strongest fact wins the one slot. -->
      <span
        class="rounded px-1.5 py-0.5 text-[10px] font-semibold backdrop-blur-sm"
        style="background: rgb(37 99 235 / 0.85); color: #fff;"
        title={t('library.badge.plannedHint')}
      >
        ▤ {t('library.badge.planned')}
      </span>
    {:else if image.similar}
      <span
        class="rounded px-1.5 py-0.5 text-[10px] font-semibold backdrop-blur-sm"
        style="background: rgb(217 119 6 / 0.85); color: #fff;"
      >
        ≈ {t('library.badge.similar')}
      </span>
    {/if}
    {#if !image.quick.has_metadata}
      <span
        class="rounded px-1.5 py-0.5 text-[10px] font-semibold backdrop-blur-sm"
        style="background: rgb(0 0 0 / 0.7); color: var(--color-ink-300);"
      >
        {t('library.badge.noMetadata')}
      </span>
    {/if}
  </div>

  {#if selected}
    <span
      class="pointer-events-none absolute right-1.5 top-1.5 flex h-5 w-5 items-center justify-center rounded-full text-[11px] font-bold"
      style="background: var(--color-accent-500); color: #04211f;"
      aria-hidden="true"
    >
      ✓
    </span>
  {/if}

  <button
    type="button"
    onclick={onInspect}
    class="btn btn-sm absolute right-1.5 bottom-1.5 opacity-0 transition-opacity group-hover:opacity-100"
    title={t('library.detailsHint')}
  >
    {t('library.details')}
  </button>

</figure>
