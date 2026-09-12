<script lang="ts">
  let { class: className = '' }: { class?: string } = $props()
</script>

<span
  class="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-ink-600 border-t-accent-400 {className}"
  aria-hidden="true"
></span>
