<script lang="ts">
  import type { Snippet } from 'svelte'

  let {
    label,
    hint,
    children,
  }: { label: string; hint?: string | Snippet; children: Snippet } = $props()
</script>

<div>
  <span class="label">{label}</span>
  {@render children()}
  {#if hint}
    <p class="mt-1 text-xs text-ink-400">
      {#if typeof hint === 'string'}{hint}{:else}{@render hint()}{/if}
    </p>
  {/if}
</div>
