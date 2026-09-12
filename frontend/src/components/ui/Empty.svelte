<script lang="ts">
  import Icon, { type IconName } from './Icon.svelte'

  let { icon, title, hint }: { icon: IconName; title: string; hint?: string } = $props()
</script>

<div class="flex flex-col items-center justify-center gap-2 py-16 text-center">
  <div class="opacity-40"><Icon name={icon} size={40} /></div>
  <p class="text-sm font-medium text-ink-200">{title}</p>
  {#if hint}
    <p class="max-w-md text-xs text-ink-400">{hint}</p>
  {/if}
</div>
