<script lang="ts">
  import { t } from '../i18n/index.svelte'
  import { app, dismissToast } from '../state/store.svelte'
  import Icon from './ui/Icon.svelte'

  const STYLE = {
    success: { color: 'var(--color-published)', icon: 'success' },
    error: { color: 'var(--color-failed)', icon: 'failed' },
    info: { color: 'var(--color-accent-400)', icon: 'info' },
  } as const
</script>

{#if app.toasts.length}
  <div
    class="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-full max-w-sm flex-col gap-2"
  >
    {#each app.toasts as toast (toast.id)}
      {@const style = STYLE[toast.kind]}
      <div
        class="panel-raised animate-in pointer-events-auto flex items-start gap-2.5 px-3.5 py-3"
        style="border-color: color-mix(in srgb, {style.color} 40%, var(--color-ink-700));"
      >
        <span
          class="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full"
          style="color: {style.color}; background: color-mix(in srgb, {style.color} 16%, transparent);"
          aria-hidden="true"
        >
          <Icon name={style.icon} size={14} />
        </span>
        <p class="flex-1 whitespace-pre-line text-xs leading-relaxed text-ink-200">{toast.message}</p>
        <button
          class="btn btn-ghost btn-sm -my-1 -mr-1"
          onclick={() => dismissToast(toast.id)}
          aria-label={t('ui.close')}
        >
          ✕
        </button>
      </div>
    {/each}
  </div>
{/if}
