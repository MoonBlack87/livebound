<script lang="ts">
  import { t } from '../../i18n/index.svelte'
  import { app } from '../../state/store.svelte'
  import { formatDateTime } from '../ui/format.svelte'
  import type { ImageDetail } from '../../types/api'

  let { detail }: { detail: ImageDetail } = $props()

  const danger = $derived(detail.duplicates.exact.length > 0)
  const rows = $derived([...detail.duplicates.exact, ...detail.duplicates.similar].slice(0, 4))
  // The configured domain, not a hardcoded one: .com filters to SFW in some
  // regions, which is exactly why the default is .red.
  const site = $derived(app.settings?.site_base ?? 'https://civitai.red')
</script>

<div
  class="rounded-lg border px-3 py-2.5"
  style="border-color: color-mix(in srgb, {danger
    ? 'var(--color-failed)'
    : 'var(--color-ready)'} 45%, transparent);
         background: color-mix(in srgb, {danger
    ? 'var(--color-failed)'
    : 'var(--color-ready)'} 10%, transparent);"
>
  <p class="text-xs font-medium text-ink-100">
    {danger ? t('drawer.dupe.exact') : t('drawer.dupe.similar')}
  </p>
  <ul class="mt-1.5 space-y-1">
    {#each rows as row (row.id)}
      <li class="text-[11px] text-ink-300">
        {row.post_title || t('drawer.dupe.post', { id: row.remote_post_id ?? row.post_id ?? '?' })}
        <span class="text-ink-500">
          · {formatDateTime(row.used_at)}
          {row.distance != null ? ` · ${t('drawer.dupe.distance', { distance: row.distance })}` : ''}
          {row.status === 'withdrawn' ? ` · ${t('drawer.dupe.withdrawn')}` : ''}
        </span>
        {#if row.remote_post_id}
          <a
            class="ml-1.5 underline decoration-dotted"
            href="{site}/posts/{row.remote_post_id}"
            target="_blank"
            rel="noreferrer"
          >
            {t('drawer.dupe.view')}
          </a>
        {/if}
      </li>
    {/each}
  </ul>
</div>
