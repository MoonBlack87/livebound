<script lang="ts">
  import type { Snippet } from 'svelte'

  let {
    title,
    actions,
    children,
    class: className = '',
  }: {
    title?: string
    actions?: Snippet
    children: Snippet
    class?: string
  } = $props()
</script>

<section class="panel {className}">
  {#if title || actions}
    <header class="flex items-center justify-between gap-3 border-b border-ink-750 px-4 py-3">
      <h2 class="text-sm font-semibold text-ink-100">{title ?? ''}</h2>
      {#if actions}
        <div class="flex items-center gap-2">{@render actions()}</div>
      {/if}
    </header>
  {/if}
  <div class="p-4">{@render children()}</div>
</section>
