<script lang="ts">
  import type { Snippet } from 'svelte'
  import { t } from '../../i18n/index.svelte'

  let {
    title,
    onClose,
    children,
    wide = false,
    footer,
  }: {
    title: string
    onClose: () => void
    children: Snippet
    wide?: boolean
    footer?: Snippet
  } = $props()

  let dialog = $state<HTMLDivElement | null>(null)

  $effect(() => {
    dialog?.focus()
  })

  function onKey(event: KeyboardEvent) {
    if (event.key === 'Escape') onClose()
  }

  function onBackdrop(event: MouseEvent) {
    if (event.target === event.currentTarget) onClose()
  }
</script>

<svelte:window onkeydown={onKey} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
  class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
  onmousedown={onBackdrop}
>
  <div
    bind:this={dialog}
    tabindex="-1"
    role="dialog"
    aria-modal="true"
    class="panel-raised animate-in flex max-h-[90vh] w-full flex-col outline-none {wide
      ? 'max-w-5xl'
      : 'max-w-lg'}"
  >
    <header class="flex items-center justify-between gap-3 border-b border-ink-700 px-5 py-3.5">
      <h2 class="text-sm font-semibold">{title}</h2>
      <button class="btn btn-ghost btn-sm" onclick={onClose} aria-label={t('ui.close')}>✕</button>
    </header>
    <div class="min-h-0 flex-1 overflow-y-auto px-5 py-4">{@render children()}</div>
    {#if footer}
      <footer class="flex items-center justify-end gap-2 border-t border-ink-700 px-5 py-3">
        {@render footer()}
      </footer>
    {/if}
  </div>
</div>
