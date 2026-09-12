<script lang="ts">
  import type { Snippet } from 'svelte'
  import { t } from '../../i18n/index.svelte'
  import Modal from './Modal.svelte'

  let {
    title,
    body,
    confirmLabel,
    danger = false,
    busy = false,
    requireKey,
    onConfirm,
    onCancel,
  }: {
    title: string
    body: string | Snippet
    confirmLabel?: string
    danger?: boolean
    busy?: boolean
    /**
     * Translation key of the word the user has to type out. For irreversible
     * actions. It is a key rather than the word itself, because the word is
     * shown *and* compared - a German prompt with an English answer would be
     * unusable.
     */
    requireKey?: string
    onConfirm: () => void
    onCancel: () => void
  } = $props()

  let typed = $state('')

  const word = $derived(requireKey ? t(requireKey) : '')
  const ready = $derived(!requireKey || typed.trim().toLowerCase() === word.toLowerCase())
</script>

<Modal {title} onClose={onCancel}>
  <div class="space-y-3 text-sm text-ink-200">
    {#if typeof body === 'string'}{body}{:else}{@render body()}{/if}
  </div>
  {#if requireKey}
    <div class="mt-4">
      <label class="label" for="confirm-word">{t('ui.confirm.type', { word })}</label>
      <!-- svelte-ignore a11y_autofocus -->
      <input id="confirm-word" class="input" bind:value={typed} autofocus />
    </div>
  {/if}
  {#snippet footer()}
    <button class="btn" onclick={onCancel}>{t('ui.cancel')}</button>
    <button
      class={danger ? 'btn btn-danger' : 'btn btn-primary'}
      disabled={!ready || busy}
      onclick={onConfirm}
    >
      {confirmLabel ?? t('ui.confirm')}
    </button>
  {/snippet}
</Modal>
