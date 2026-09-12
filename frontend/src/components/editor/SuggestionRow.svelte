<script lang="ts">
  import { t } from '../../i18n/index.svelte'
  import type { Suggestion } from '../../types/api'

  let {
    suggestions,
    disabled = false,
    onAccept,
    onDismiss,
  }: {
    suggestions: Suggestion[]
    disabled?: boolean
    onAccept: (id: number) => void
    onDismiss: (id: number) => void
  } = $props()
</script>

{#if suggestions.length}
  <div class="mt-1.5 space-y-1.5">
    <!-- All the unused ones, newest first: the backend reads back at most 30
         (`ORDER BY id DESC LIMIT 30`), and hiding a suggestion the user waited
         half a minute for is the complaint this comes from. One already written
         into the post is not hidden but spent - offering it again is noise. -->
    {#each suggestions.filter((item) => !item.accepted) as item (item.id)}
      <div
        class="flex items-start gap-2 rounded-lg border px-2.5 py-1.5"
        style="border-color: color-mix(in srgb, var(--color-accent-500) 35%, transparent);
               background: color-mix(in srgb, var(--color-accent-500) 8%, transparent);"
      >
        <span class="mt-0.5 shrink-0 text-[10px]" style="color: var(--color-accent-400);">✦</span>
        <div class="min-w-0 flex-1">
          <p class="break-words text-xs text-ink-200">{item.text}</p>
          <!-- The number that produced this. Worth showing: a discarded
               suggestion is otherwise gone for good. -->
          {#if item.seed != null}
            <p class="mt-0.5 font-mono text-[10px] text-ink-500" title={t('llm.seedExplain')}>
              {t('llm.seed')} {item.seed}
            </p>
          {/if}
        </div>
        <button class="btn btn-sm shrink-0" {disabled} onclick={() => onAccept(item.id)}>
          {t('llm.accept')}
        </button>
        <!-- No confirmation: this removes only a regeneratable machine proposal,
             never content the maintainer wrote. -->
        <button
          class="btn btn-sm shrink-0"
          aria-label={t('llm.dismiss')}
          onclick={() => onDismiss(item.id)}>✕</button
        >
      </div>
    {/each}
  </div>
{/if}
