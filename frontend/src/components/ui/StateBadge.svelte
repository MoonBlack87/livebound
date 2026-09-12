<script lang="ts">
  import { t } from '../../i18n/index.svelte'
  import type { PostState } from '../../types/api'
  import Icon from './Icon.svelte'
  import { STATE_STYLE } from './states'

  let { state, label }: { state: PostState; label?: string } = $props()

  const style = $derived(STATE_STYLE[state] ?? STATE_STYLE.draft)
</script>

<span
  class="chip"
  style="color: {style.color};
         border-color: color-mix(in srgb, {style.color} 40%, transparent);
         background: color-mix(in srgb, {style.color} 12%, transparent);"
>
  <Icon name={style.icon} size={16} />
  {label ?? t(style.label)}
</span>
