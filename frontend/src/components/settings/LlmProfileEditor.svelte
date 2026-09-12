<script lang="ts">
  import { untrack } from 'svelte'

  import { t } from '../../i18n/index.svelte'
  import type { LlmProfileInput } from '../../types/api'
  import Modal from '../ui/Modal.svelte'

  let {
    draft,
    saving,
    creating,
    canReset,
    onReset,
    onSave,
    onClose,
  }: {
    draft: LlmProfileInput
    saving: boolean
    creating: boolean
    canReset: boolean
    onReset: () => Promise<string | null>
    onSave: (draft: LlmProfileInput) => Promise<void>
    onClose: () => void
  } = $props()

  let form = $state(untrack(() => ({ ...draft })))
  let resetting = $state(false)

  function submit(event: SubmitEvent) {
    event.preventDefault()
    void onSave({ ...form })
  }

  async function resetSystemPrompt() {
    resetting = true
    try {
      const seed = await onReset()
      if (seed !== null) form.system_prompt = seed
    } finally {
      resetting = false
    }
  }
</script>

<Modal
  title={creating ? t('settings.llm.createProfile') : t('settings.llm.editProfileTitle')}
  wide
  {onClose}
>
  <form id="llm-profile-form" class="space-y-4" onsubmit={submit}>
    <div class="grid gap-3 md:grid-cols-2">
      <div>
        <label class="label" for="llm-profile-name">{t('settings.llm.name')}</label>
        <input
          id="llm-profile-name"
          class="input"
          bind:value={form.name}
          maxlength="80"
          required
        />
      </div>
      <div>
        <label class="label" for="llm-profile-description">
          {t('settings.llm.description')}
        </label>
        <input
          id="llm-profile-description"
          class="input"
          bind:value={form.description}
          maxlength="500"
        />
      </div>
    </div>

    <div>
      <div class="mb-1 flex items-center justify-between gap-3">
        <label class="label mb-0" for="llm-profile-prompt">
          {t('settings.llm.systemPrompt')}
        </label>
        {#if canReset}
          <button
            class="btn btn-sm"
            type="button"
            disabled={saving || resetting}
            onclick={resetSystemPrompt}
          >
            {t('settings.llm.resetSystemPrompt')}
          </button>
        {/if}
      </div>
      <textarea
        id="llm-profile-prompt"
        class="input min-h-36 resize-y"
        bind:value={form.system_prompt}
        maxlength="8000"
        required
      ></textarea>
    </div>

    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div>
        <label class="label" for="llm-profile-title-words">
          {t('settings.llm.titleMaxWords')}
        </label>
        <input
          id="llm-profile-title-words"
          class="input"
          type="number"
          min="1"
          max="30"
          bind:value={form.title_max_words}
          required
        />
      </div>
      <div>
        <label class="label" for="llm-profile-tag-count">{t('settings.llm.tagCount')}</label>
        <input
          id="llm-profile-tag-count"
          class="input"
          type="number"
          min="0"
          max="30"
          bind:value={form.tag_count}
          required
        />
      </div>
      <div>
        <label class="label" for="llm-profile-max-tokens">
          {t('settings.llm.maxNewTokens')}
        </label>
        <input
          id="llm-profile-max-tokens"
          class="input"
          type="number"
          min="32"
          max="4096"
          bind:value={form.max_new_tokens}
          required
        />
      </div>
      <div>
        <label class="label" for="llm-profile-temperature">
          {t('settings.llm.temperature')}
        </label>
        <input
          id="llm-profile-temperature"
          class="input"
          type="number"
          min="0"
          max="2"
          step="any"
          bind:value={form.temperature}
          required
        />
      </div>
      <div>
        <label class="label" for="llm-profile-top-p">{t('settings.llm.topP')}</label>
        <input
          id="llm-profile-top-p"
          class="input"
          type="number"
          min="0"
          max="1"
          step="any"
          bind:value={form.top_p}
          required
        />
      </div>
      <div>
        <label class="label" for="llm-profile-top-k">{t('settings.llm.topK')}</label>
        <input
          id="llm-profile-top-k"
          class="input"
          type="number"
          min="0"
          max="200"
          bind:value={form.top_k}
          required
        />
      </div>
    </div>

    <div class="rounded-lg border border-ink-750 bg-ink-850 p-3">
      <label class="flex cursor-pointer items-center gap-2 text-xs text-ink-200">
        <input type="checkbox" class="accent-teal-500" bind:checked={form.include_images} />
        {t('settings.llm.includeImages')}
      </label>
      <p class="mt-1 text-[11px] leading-relaxed text-ink-500">
        {t('settings.llm.includeImagesHint')}
      </p>
      <div class="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <label class="label" for="llm-profile-max-images">
            {t('settings.llm.maxImages')}
          </label>
          <input
            id="llm-profile-max-images"
            class="input"
            type="number"
            min="0"
            max="8"
            bind:value={form.max_images}
            disabled={!form.include_images}
            required
          />
        </div>
        <div>
          <label class="label" for="llm-profile-vision-side">
            {t('settings.llm.visionMaxSide')}
          </label>
          <input
            id="llm-profile-vision-side"
            class="input"
            type="number"
            min="128"
            max="2048"
            bind:value={form.vision_max_side}
            disabled={!form.include_images}
            required
          />
          <p class="mt-1 text-[11px] leading-relaxed text-ink-500">
            {t('settings.llm.visionMaxSideHint')}
          </p>
        </div>
      </div>
    </div>
  </form>

  {#snippet footer()}
    <button class="btn" disabled={saving} onclick={onClose}>
      {t('ui.cancel')}
    </button>
    <button
      class="btn btn-primary"
      type="submit"
      form="llm-profile-form"
      disabled={saving}
    >
      {creating ? t('settings.llm.createProfile') : t('settings.llm.saveProfile')}
    </button>
  {/snippet}
</Modal>
