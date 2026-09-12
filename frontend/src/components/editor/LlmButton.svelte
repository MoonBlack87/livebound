<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { app, invalidate, setActiveJob, toast } from '../../state/store.svelte'
  import type { LlmProfile, LlmStatus, SuggestMaterial } from '../../types/api'
  import Spinner from '../ui/Spinner.svelte'

  /**
   * Asking the local model for suggestions, with a say in how.
   *
   * One run answers title, description and tags together - that is one
   * generation, not three, so there was never anything to ask for field by
   * field. What is worth choosing is how many alternatives to produce: each is
   * another generation against the model that is already loaded, and loading it
   * is what costs the minute.
   *
   * The profile carries the tone (it is a stored system prompt); the hint is for
   * this one run - "darker", "mention the rain". Both are optional, so the plain
   * case stays one click.
   */

  let { postId }: { postId: number } = $props()

  let open = $state(false)
  let profiles = $state<LlmProfile[]>([])
  let profileId = $state<number | undefined>(undefined)
  let hint = $state('')
  let seed = $state<number | null>(null)
  /**
   * What the model is built from. `auto` is the rule - a model that sees works
   * from the pictures, one that cannot gets the generation prompts - and the
   * other three override it for this one run.
   */
  let material = $state<SuggestMaterial>('auto')
  let status = $state<LlmStatus | null>(null)
  let running = $state(false)
  let stage = $state('')

  const chosen = $derived(profiles.find((profile) => profile.id === profileId))
  const variants = $derived(app.settings?.llm_variants ?? 1)

  async function setVariants(value: number) {
    if (value === variants) return
    try {
      await api.saveSettings({ llm_variants: value })
      invalidate()
    } catch (error) {
      // The count is drawn from the stored setting, so a silent failure would
      // leave the panel showing a number the next run will not use.
      toast('error', (error as Error).message)
    }
  }

  $effect(() => {
    if (!open) return
    api
      .llmProfiles()
      .then((data) => {
        profiles = data.items
        // `Number(undefined)` is NaN, and `??` does not catch NaN - without a
        // stored default that sent `profile_id: NaN` and the request came back
        // as a raw validation error instead of a suggestion.
        const preferred = data.default_id ? Number(data.default_id) : undefined
        profileId = profileId ?? preferred ?? data.items[0]?.id
      })
      .catch(() => undefined)
    api.llmStatus().then((value) => (status = value)).catch(() => undefined)
  })

  async function generate() {
    running = true
    stage = ''
    try {
      // Svelte binds a number input as number-or-null, never as a string.
      const chosenSeed = seed ?? undefined
      if (chosenSeed !== undefined && (!Number.isInteger(chosenSeed)
          || chosenSeed < 0 || chosenSeed > 2147483647)) {
        throw new Error(t('llm.seedInvalid'))
      }
      let job = await api.llmSuggest([postId], profileId, hint, chosenSeed, material)
      // The job bar sits inside <main>; this editor is a fixed overlay on top of
      // it, so its spinner is never seen from here. Same 900 ms poll, own state -
      // the pattern BulkEditDialog::shorten already uses inside this overlay.
      setActiveJob(job)
      open = false
      while (job.status === 'starting' || job.status === 'running') {
        stage = stageLabel(job)
        await new Promise((resolve) => setTimeout(resolve, 900))
        job = await api.job(job.id)
      }
      if (job.status !== 'done') throw new Error(job.error || t('llm.failed'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      running = false
      stage = ''
    }
  }

  /**
   * The backend speaks English and carries a stable `stage_code` beside its
   * English `stage`; this resolves the code and falls back to that sentence,
   * the same contract `translateIssue` uses. No percentage on purpose: loading
   * the model dominates and has no measurable progress, and for one post
   * `total` is 1, so a bar would sit at 0 and jump to 100.
   */
  function stageLabel(job: { stage: string; stage_code?: string }): string {
    if (!job.stage_code) return job.stage
    const key = `llm.stage.${job.stage_code}`
    const text = t(key)
    return text === key ? job.stage : text
  }
</script>

<div class="relative">
  <button
    class="btn btn-ghost btn-sm"
    title={t('llm.buttonHint')}
    disabled={running}
    onclick={() => (open = !open)}
  >
    {#if running}
      <Spinner />
      <span class="ml-1.5">{stage || t('llm.stage.loading')}</span>
    {:else}
      ✦ {t('llm.suggest')}
      {open ? '▾' : '▸'}
    {/if}
  </button>

  {#if open}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <div class="fixed inset-0 z-30" onclick={() => (open = false)}></div>
    <div class="panel-raised absolute right-0 z-40 mt-1 w-80 space-y-2.5 p-3">
      <div>
        <span class="label">{t('llm.tone')}</span>
        <select
          class="input"
          value={profileId ?? ''}
          onchange={(event) =>
            (profileId = Number((event.currentTarget as HTMLSelectElement).value))}
        >
          {#each profiles as profile (profile.id)}
            <option value={profile.id}>{profile.name}</option>
          {/each}
        </select>
        {#if chosen?.description}
          <p class="mt-1 text-[11px] text-ink-500">{chosen.description}</p>
        {/if}
      </div>

      <div>
        <span class="label">{t('llm.count')}</span>
        <div class="mt-1 flex gap-1.5">
          {#each [1, 2, 3] as count (count)}
            <button
              class="btn btn-sm flex-1 justify-center {variants === count ? 'btn-primary' : ''}"
              onclick={() => setVariants(count)}>{count}</button
            >
          {/each}
        </div>
        <p class="mt-1 text-[11px] leading-relaxed text-ink-500">{t('llm.countExplain')}</p>
      </div>

      <div>
        <span class="label">{t('llm.material')}</span>
        <select class="input" bind:value={material}>
          <option value="auto">{t('llm.material.auto')}</option>
          <option value="images">{t('llm.material.images')}</option>
          <option value="prompt">{t('llm.material.prompt')}</option>
          <option value="both">{t('llm.material.both')}</option>
        </select>
        <p class="mt-1 text-[11px] leading-relaxed text-ink-500">
          {t(`llm.materialExplain.${material}`)}
        </p>
      </div>

      <div>
        <span class="label">{t('llm.seed')}</span>
        <input
          class="input"
          type="number"
          min="0"
          max="2147483647"
          step="1"
          placeholder={t('llm.seedPlaceholder')}
          bind:value={seed}
        />
        <p class="mt-1 text-[11px] leading-relaxed text-ink-500">{t('llm.seedExplain')}</p>
      </div>

      <div>
        <span class="label">{t('llm.hint')}</span>
        <textarea
          class="input min-h-16 resize-y"
          placeholder={t('llm.hintPlaceholder')}
          bind:value={hint}
          maxlength="1000"
        ></textarea>
        <p class="mt-1 text-[11px] leading-relaxed text-ink-500">{t('llm.hintExplain')}</p>
      </div>

      <!-- Only when we know it is off. Before the model has run once the answer
           is unknown, which is not the same as "no". -->
      {#if status?.vision === false}
        <p class="text-[11px] leading-relaxed" style="color: var(--color-ready);">
          {t('llm.noVision')}
        </p>
      {/if}

      <button
        class="btn btn-primary btn-sm w-full justify-center"
        disabled={running}
        onclick={generate}
      >
        {variants > 1 ? t('llm.generatePlural') : t('llm.generate')}
      </button>
    </div>
  {/if}
</div>
