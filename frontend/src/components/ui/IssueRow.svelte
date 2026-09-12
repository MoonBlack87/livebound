<script lang="ts">
  import type { Snippet } from 'svelte'
  import type { IssueLevel } from '../../types/api'
  import Icon from './Icon.svelte'
  import { LEVEL_STYLE } from './states'

  let { level, children }: { level: IssueLevel; children: Snippet } = $props()

  const style = $derived(LEVEL_STYLE[level])
</script>

<div class="flex items-start gap-2 text-xs leading-relaxed">
  <span
    class="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[10px] font-bold"
    style="color: {style.color}; background: color-mix(in srgb, {style.color} 16%, transparent);"
    aria-hidden="true"
  >
    <Icon name={style.icon} size={16} />
  </span>
  <span class={level === 'info' ? 'text-ink-400' : 'text-ink-200'}>{@render children()}</span>
</div>
