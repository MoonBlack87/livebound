<script lang="ts">
  import { t } from '../../i18n/index.svelte'

  let { text, label }: { text: string; label?: string } = $props()

  let copied = $state(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      copied = true
      setTimeout(() => (copied = false), 1400)
    } catch {
      /* clipboard blocked; nothing useful to do */
    }
  }
</script>

<button type="button" class="btn btn-ghost btn-sm" onclick={copy}>
  {copied ? t('ui.copied') : (label ?? t('ui.copy'))}
</button>
