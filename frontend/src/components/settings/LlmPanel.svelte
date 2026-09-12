<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { app, invalidate, toast } from '../../state/store.svelte'
  import ConfirmDialog from '../ui/ConfirmDialog.svelte'
  import CopyButton from '../ui/CopyButton.svelte'
  import Panel from '../ui/Panel.svelte'
  import LlmProfileEditor from './LlmProfileEditor.svelte'
  import type { LlmProfile, LlmProfileInput, LlmStatus } from '../../types/api'

  let status = $state<LlmStatus | null>(null)
  let profiles = $state<LlmProfile[]>([])
  let defaultProfileId = $state<number | null>(null)
  let draft = $state<LlmProfileInput | null>(null)
  let deletingInFlight = $state(false)
  let editingProfileId = $state<number | null>(null)
  let deleting = $state<LlmProfile | null>(null)
  let restoring = $state(false)
  let restoreAsked = $state(false)
  let saving = $state(false)
  let editingHasSeed = $state(false)
  let endpointModels = $state<{ items: string[]; url: string; reachable: boolean } | null>(null)

  /**
   * Which of the two sources is active is a stored choice, not a guess. A model
   * name means the server answers; empty means the local folder does. Reading it
   * off the shape of a string would turn a mistyped path into a model name and
   * fail somewhere far away from the mistake.
   */
  const usesEndpoint = $derived(Boolean(app.settings?.llm_endpoint_model))

  async function loadEndpointModels() {
    try {
      endpointModels = await api.llmEndpointModels()
    } catch {
      endpointModels = { items: [], url: '', reachable: false }
    }
  }

  async function chooseSource(endpointChosen: boolean) {
    if (endpointChosen === usesEndpoint) return
    try {
      if (endpointChosen) {
        await loadEndpointModels()
        const first = endpointModels?.items[0]
        if (!first) {
          toast('error', t('settings.llm.endpointUnreachable'))
          return
        }
        await api.saveSettings({ llm_endpoint_model: first })
      } else {
        await api.saveSettings({ llm_endpoint_model: '' })
      }
      invalidate()
      status = await api.llmStatus()
    if (usesEndpoint) await loadEndpointModels()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  function newProfile(): LlmProfileInput {
    return {
      name: '',
      description: '',
      system_prompt: '',
      title_max_words: 7,
      tag_count: 7,
      include_images: true,
      max_images: 1,
      vision_max_side: 768,
      max_new_tokens: 700,
      temperature: 0.7,
      top_p: 0.8,
      top_k: 20,
    }
  }

  async function loadProfiles() {
    const data = await api.llmProfiles()
    profiles = data.items
    const parsed = data.default_id ? Number(data.default_id) : null
    defaultProfileId = parsed !== null && Number.isInteger(parsed) ? parsed : null
  }

  $effect(() => {
    void app.settings
    api.llmStatus().then((value) => (status = value)).catch(() => undefined)
    // Without this the picker shows the one stored name and nothing else, so a
    // second model can never be chosen and a stopped server is invisible.
    if (usesEndpoint) void loadEndpointModels()
    loadProfiles().catch(() => undefined)
  })

  async function unload() {
    await api.llmStop()
    toast('info', t('settings.llm.unloaded'))
    status = await api.llmStatus()
  }

  function createProfile() {
    editingProfileId = null
    editingHasSeed = false
    draft = newProfile()
  }

  function editProfile(profile: LlmProfile) {
    editingProfileId = profile.id
    editingHasSeed = profile.is_builtin
    draft = {
      name: profile.name,
      description: profile.description,
      system_prompt: profile.system_prompt,
      title_max_words: profile.title_max_words,
      tag_count: profile.tag_count,
      include_images: profile.include_images,
      max_images: profile.max_images,
      vision_max_side: profile.vision_max_side,
      max_new_tokens: profile.max_new_tokens,
      temperature: profile.temperature,
      top_p: profile.top_p,
      top_k: profile.top_k,
    }
  }

  async function resetProfilePrompt(): Promise<string | null> {
    if (editingProfileId === null || !editingHasSeed) return null
    try {
      return (await api.llmProfileSeed(editingProfileId)).system_prompt
    } catch (error) {
      toast('error', (error as Error).message)
      return null
    }
  }

  async function restoreProfiles() {
    if (restoring) return
    restoring = true
    try {
      const { restored } = await api.restoreLlmProfiles()
      await loadProfiles()
      toast('success', t('settings.llm.profilesRestored', { names: restored.join(', ') }))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      restoreAsked = false
      restoring = false
    }
  }

  async function saveProfile(profile: LlmProfileInput) {
    saving = true
    try {
      if (editingProfileId === null) {
        await api.createLlmProfile(profile)
        toast('success', t('settings.llm.profileCreated'))
      } else {
        await api.updateLlmProfile(editingProfileId, profile)
        toast('success', t('settings.llm.profileSaved'))
      }
      draft = null
      await loadProfiles()
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      saving = false
    }
  }

  async function makeDefault(profile: LlmProfile) {
    try {
      await api.setDefaultLlmProfile(profile.id)
      defaultProfileId = profile.id
      toast('success', t('settings.llm.profileSetDefault', { name: profile.name }))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function deleteProfile() {
    // Without the in-flight guard a second click sent a second DELETE, and
    // clearing `deleting` only on success left the dialog open with no way to
    // tell it apart from one that had not been pressed yet. The create/edit
    // path has `saving` and does it this way.
    if (!deleting || deletingInFlight || profiles.length <= 1) return
    const profile = deleting
    deletingInFlight = true
    try {
      await api.deleteLlmProfile(profile.id)
      await loadProfiles()
      toast('success', t('settings.llm.profileDeleted', { name: profile.name }))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      deleting = null
      deletingInFlight = false
    }
  }
</script>

<Panel title={t('settings.llm.title')}>
  {#snippet actions()}
    {#if status?.worker_running}
      <button class="btn btn-sm" onclick={unload}>{t('settings.llm.unload')}</button>
    {/if}
  {/snippet}

  <div class="space-y-3">
    <div class="flex flex-wrap items-center gap-2">
      <span
        class="h-1.5 w-1.5 rounded-full"
        style="background: {status?.ready ? 'var(--color-published)' : 'var(--color-ready)'};"
      ></span>
      <span class="text-xs text-ink-200">
        {status?.ready ? t('settings.llm.ready') : t('settings.llm.notReady')}
        {status?.worker_running ? ` · ${t('settings.llm.loaded')}` : ''}
        {status?.resolved_device ? ` · ${status.resolved_device}` : ''}
      </span>
      {#if status?.vision === true}
        <span class="chip text-[10px]">✦ {t('settings.llm.sees')}</span>
      {:else if status?.vision === false}
        <span class="chip text-[10px]" style="color: var(--color-ready);">
          {t('settings.llm.textOnly')}
        </span>
      {/if}
    </div>

    <!-- Vision failing is easy to miss: the model answers and simply never looks
         at the pictures. Locally that is usually a missing torchvision/Pillow;
         for a hosted model the reason comes from the server, and installing
         anything here would not help - so only the reason is shown. -->
    {#if status?.vision === false && status.vision_error && status.runtime === 'endpoint'}
      <p class="text-[11px] leading-relaxed" style="color: var(--color-ready);">
        {status.vision_error}
      </p>
    {:else if status?.vision === false && status.vision_error}
      <div
        class="rounded-lg border px-3 py-2.5"
        style="border-color: color-mix(in srgb, var(--color-ready) 45%, transparent);
               background: color-mix(in srgb, var(--color-ready) 10%, transparent);"
      >
        <p class="text-xs font-medium text-ink-100">{t('settings.llm.visionOffTitle')}</p>
        <p class="mt-1 text-[11px] leading-relaxed text-ink-300">{status.vision_error}</p>
        <div class="mt-1.5 flex items-center gap-2">
          <code
            class="mono min-w-0 flex-1 truncate rounded bg-ink-950 px-2 py-1 text-[11px] text-ink-200"
          >
            {status.vision_fix_command || 'uv pip install --python <interpreter> pillow torchvision'}
          </code>
          {#if status.vision_fix_command}
            <CopyButton text={status.vision_fix_command} />
          {/if}
        </div>
        <p class="mt-1 text-[11px] leading-relaxed text-ink-500">{t('settings.llm.visionFixHint')}</p>
      </div>
    {/if}

    <!-- The job line says this while the model works; after it was released the
         remembered answer is the only thing left that explains a slow run. -->
    {#if status?.offloaded}
      <p class="text-[11px] leading-relaxed" style="color: var(--color-ready);">
        {t('settings.llm.offloaded')}
      </p>
    {/if}

    <!-- Running the checkpoint's own Python is the one thing here that cannot be
         taken back once it has happened, so it says so where the model status is. -->
    {#if app.settings?.llm_trust_remote_code}
      <div
        class="rounded-lg border px-3 py-2.5"
        style="border-color: color-mix(in srgb, var(--color-failed) 45%, transparent);
               background: color-mix(in srgb, var(--color-failed) 10%, transparent);"
      >
        <p class="text-xs font-medium text-ink-100">{t('settings.llm.trustRemoteCodeOnTitle')}</p>
        <p class="mt-1 text-[11px] leading-relaxed text-ink-300">
          {t('settings.llm.trustRemoteCodeOnBody')}
        </p>
      </div>
    {/if}

    <div>
      <span class="label">{t('settings.llm.source')}</span>
      <div class="mt-1 flex gap-1.5">
        <button
          class="btn btn-sm flex-1 justify-center {usesEndpoint ? '' : 'btn-primary'}"
          onclick={() => chooseSource(false)}>{t('settings.llm.sourceLocal')}</button
        >
        <button
          class="btn btn-sm flex-1 justify-center {usesEndpoint ? 'btn-primary' : ''}"
          onclick={() => chooseSource(true)}>{t('settings.llm.sourceEndpoint')}</button
        >
      </div>
      <p class="mt-1 text-[11px] leading-relaxed text-ink-500">{t('settings.llm.ggufPolicy')}</p>
      <p class="mt-1 text-[11px] leading-relaxed text-ink-400">
        {usesEndpoint
          ? t('settings.llm.endpointActive')
          : t('settings.llm.localActive')}
        · {usesEndpoint
          ? t('settings.llm.localNotSelected')
          : t('settings.llm.endpointNotSelected')}
      </p>
    </div>

    {#if usesEndpoint}
      <div class="grid grid-cols-2 gap-3">
        <div>
          <span class="label">{t('settings.llm.endpointUrl')}</span>
          <input
            class="input"
            value={app.settings?.llm_endpoint || status?.endpoint || ''}
            onblur={async (event) => {
              await api.saveSettings({
                llm_endpoint: (event.currentTarget as HTMLInputElement).value,
              })
              invalidate()
              await loadEndpointModels()
            }}
          />
        </div>
        <div>
          <span class="label">{t('settings.llm.endpointModel')}</span>
          <!-- Picked from what the server reports, never typed: a name that only
               looks right fails at the first suggestion, minutes later. -->
          <select
            class="input"
            value={app.settings?.llm_endpoint_model || ''}
            onchange={async (event) => {
              await api.saveSettings({
                llm_endpoint_model: (event.currentTarget as HTMLSelectElement).value,
              })
              invalidate()
              status = await api.llmStatus()
            }}
          >
            {#each endpointModels?.items ?? [app.settings?.llm_endpoint_model ?? ''] as name (name)}
              <option value={name}>{name}</option>
            {/each}
          </select>
          {#if endpointModels && !endpointModels.reachable}
            <p class="mt-1 text-[11px]" style="color: var(--color-ready);">
              {t('settings.llm.endpointUnreachable')}
            </p>
          {:else if endpointModels && !endpointModels.items.length}
            <p class="mt-1 text-[11px]" style="color: var(--color-ready);">
              {t('settings.llm.endpointEmpty')}
            </p>
          {/if}
        </div>
      </div>
    {:else}
    <div class="grid grid-cols-2 gap-3">
      <div>
        <span class="label">{t('settings.llm.python')}</span>
        <input
          class="input"
          value={app.settings?.llm_python || status?.python || ''}
          placeholder={t('settings.llm.autodetected')}
          onblur={async (event) => {
            await api.saveSettings({ llm_python: (event.currentTarget as HTMLInputElement).value })
            invalidate()
          }}
        />
        {#if !status?.python_exists}
          <p class="mt-1 text-[11px]" style="color: var(--color-ready);">
            {t('settings.llm.noInterpreter')}
          </p>
        {/if}
      </div>
      <div>
        <span class="label">{t('settings.llm.modelDir')}</span>
        <input
          class="input"
          value={app.settings?.llm_model_dir || status?.model_dir || ''}
          onblur={async (event) => {
            await api.saveSettings({
              llm_model_dir: (event.currentTarget as HTMLInputElement).value,
            })
            invalidate()
          }}
        />
        {#if !status?.model_dir_exists}
          <p class="mt-1 text-[11px]" style="color: var(--color-ready);">
            {t('settings.llm.noModelDir')}
          </p>
        {/if}
      </div>
    </div>

    <p class="text-[11px] leading-relaxed text-ink-500">{t('settings.llm.processHint')}</p>
    {/if}

    <!-- Running the checkpoint's own Python only applies to a local model; a
         hosted one runs in someone else's process. -->
    {#if !usesEndpoint}
    <div>
      <label class="flex cursor-pointer items-center gap-2 text-xs text-ink-200">
        <input
          type="checkbox"
          class="accent-teal-500"
          checked={app.settings?.llm_trust_remote_code ?? false}
          onchange={async (event) => {
            await api.saveSettings({
              llm_trust_remote_code: (event.currentTarget as HTMLInputElement).checked,
            })
            invalidate()
          }}
        />
        {t('settings.llm.trustRemoteCode')}
      </label>
      <p class="mt-1 text-[11px] leading-relaxed text-ink-500">
        {t('settings.llm.trustRemoteCodeHint')}
      </p>
    </div>
    {/if}

    <div>
      <div class="mb-2 flex items-center justify-between gap-3">
        <span class="label mb-0">{t('settings.llm.profiles')}</span>
        <div class="flex flex-wrap justify-end gap-1.5">
          <button class="btn btn-sm" disabled={restoring} onclick={() => (restoreAsked = true)}>
            {t('settings.llm.restoreProfiles')}
          </button>
          <button class="btn btn-sm" onclick={createProfile}>
            {t('settings.llm.addProfile')}
          </button>
        </div>
      </div>
      <div class="space-y-2">
        {#each profiles as profile (profile.id)}
          <div class="rounded-lg border border-ink-750 bg-ink-850 px-3 py-2.5">
            <div class="flex items-start gap-3">
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-1.5">
                  <span class="text-xs font-medium text-ink-100">{profile.name}</span>
                  {#if profile.id === defaultProfileId}
                    <span class="chip text-[10px]">{t('settings.llm.default')}</span>
                  {/if}
                </div>
                <p class="mt-0.5 font-mono text-[10px] text-ink-600 select-all">
                  {t('settings.llm.profileUuid')} {profile.uuid}
                </p>
                {#if profile.description}
                  <p class="mt-0.5 text-[11px] leading-relaxed text-ink-400">
                    {profile.description}
                  </p>
                {/if}
              </div>
              <div class="flex flex-wrap justify-end gap-1.5">
                {#if profile.id !== defaultProfileId}
                  <button class="btn btn-sm" onclick={() => makeDefault(profile)}>
                    {t('settings.llm.makeDefault')}
                  </button>
                {/if}
                <button class="btn btn-sm" onclick={() => editProfile(profile)}>
                  {t('settings.llm.editProfile')}
                </button>
                <button
                  class="btn btn-danger btn-sm"
                  disabled={profiles.length <= 1}
                  onclick={() => (deleting = profile)}
                >
                  {t('settings.llm.deleteProfile')}
                </button>
              </div>
            </div>
          </div>
        {/each}
      </div>
      {#if profiles.length === 1}
        <p class="mt-1.5 text-[11px] text-ink-500">{t('settings.llm.lastProfileHint')}</p>
      {/if}
    </div>
  </div>

  {#if draft}
    <LlmProfileEditor
      {draft}
      {saving}
      creating={editingProfileId === null}
      canReset={editingHasSeed}
      onReset={resetProfilePrompt}
      onSave={saveProfile}
      onClose={() => (draft = null)}
    />
  {/if}

  {#if restoreAsked}
    <ConfirmDialog
      title={t('settings.llm.restoreProfiles')}
      body={t('settings.llm.restoreProfilesBody')}
      danger
      confirmLabel={t('settings.llm.restoreProfiles')}
      onCancel={() => (restoreAsked = false)}
      onConfirm={restoreProfiles}
    />
  {/if}

  {#if deleting}
    <ConfirmDialog
      title={t('settings.llm.deleteProfile')}
      body={t('settings.llm.deleteProfileBody', { name: deleting.name })}
      danger
      confirmLabel={t('settings.llm.deleteProfile')}
      onCancel={() => (deleting = null)}
      onConfirm={deleteProfile}
    />
  {/if}
</Panel>
