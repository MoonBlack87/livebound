<script lang="ts">
  import { api } from '../../api/client'
  import { plural, t } from '../../i18n/index.svelte'
  import { app, setSettings, toast } from '../../state/store.svelte'
  import type { MetadataFieldInventory } from '../../types/api'
  import Panel from '../ui/Panel.svelte'
  import { formatDateTime } from '../ui/format.svelte'

  let inventory = $state<MetadataFieldInventory[]>([])
  let saving = $state(false)
  let fieldFilter = $state('')
  let promptExpression = $state('')
  const configured = $derived(app.settings?.metadata_excluded_fields ?? [])
  const promptExclusions = $derived(app.settings?.prompt_exclusions ?? [])
  const inventoryNames = $derived(new Set(inventory.map((item) => item.field_name)))
  const visibleInventory = $derived(
    inventory.filter((item) =>
      item.field_name.toLocaleLowerCase().includes(fieldFilter.trim().toLocaleLowerCase()),
    ),
  )
  const legacyEntries = $derived(
    configured.filter((entry) => entry.endsWith('*') || !inventoryNames.has(entry)),
  )

  $effect(() => {
    void app.revision
    api
      .metadataFields()
      .then((data) => (inventory = data.items))
      .catch((error) => toast('error', (error as Error).message))
  })

  function matchingWildcard(fieldName: string): string | undefined {
    return configured.find(
      (entry) => entry.endsWith('*') && fieldName.startsWith(entry.slice(0, -1)),
    )
  }

  function isDisabled(fieldName: string): boolean {
    return configured.includes(fieldName) || matchingWildcard(fieldName) !== undefined
  }

  async function setFieldDisabled(fieldName: string, disabled: boolean) {
    saving = true
    const fields = disabled
      ? [...configured, fieldName]
      : configured.filter((entry) => entry !== fieldName)
    try {
      setSettings(await api.saveSettings({ metadata_excluded_fields: [...new Set(fields)] }))
      toast('success', t('settings.metadata.saved'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      saving = false
    }
  }

  async function removeLegacyEntry(entry: string) {
    saving = true
    try {
      setSettings(
        await api.saveSettings({
          metadata_excluded_fields: configured.filter((value) => value !== entry),
        }),
      )
      toast('success', t('settings.metadata.saved'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      saving = false
    }
  }

  async function addPromptExclusion() {
    const expression = promptExpression.trim()
    if (!expression) return
    saving = true
    try {
      setSettings(
        await api.saveSettings({
          prompt_exclusions: [...new Set([...promptExclusions, expression])],
        }),
      )
      promptExpression = ''
      toast('success', t('settings.metadata.promptExclusions.saved'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      saving = false
    }
  }

  async function removePromptExclusion(expression: string) {
    saving = true
    try {
      setSettings(
        await api.saveSettings({
          prompt_exclusions: promptExclusions.filter((value) => value !== expression),
        }),
      )
      toast('success', t('settings.metadata.promptExclusions.saved'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      saving = false
    }
  }
</script>

<Panel title={t('settings.metadata.title')}>
  <div class="space-y-2">
    <p class="text-xs leading-relaxed text-ink-300">{t('settings.metadata.explain')}</p>
    {#if inventory.length}
      <input
        class="input"
        placeholder={t('settings.metadata.filterPlaceholder')}
        aria-label={t('settings.metadata.filter')}
        bind:value={fieldFilter}
      />
      <div class="max-h-80 space-y-1 overflow-y-auto pr-1">
        {#each visibleInventory as field (field.field_name)}
          {@const wildcard = matchingWildcard(field.field_name)}
          <label
            class="flex items-start gap-2.5 rounded-lg border border-ink-750 bg-ink-850 px-3 py-2"
          >
            <input
              type="checkbox"
              class="mt-0.5 accent-teal-500"
              checked={isDisabled(field.field_name)}
              disabled={saving || wildcard !== undefined}
              aria-label={t('settings.metadata.toggle', { field: field.field_name })}
              onchange={(event) =>
                setFieldDisabled(
                  field.field_name,
                  (event.currentTarget as HTMLInputElement).checked,
                )}
            />
            <span class="min-w-0 flex-1">
              <span class="mono block text-xs text-ink-100">{field.field_name}</span>
              <span class="block text-[10.5px] text-ink-500">
                {plural('settings.metadata.images', field.image_count)} ·
                {t('settings.metadata.lastSeen', { when: formatDateTime(field.last_seen_at) })}
              </span>
              {#if field.field_name === 'Model' || field.field_name === 'Version'}
                <span class="mt-1 block text-[11px] leading-relaxed text-amber-400">
                  {t('settings.metadata.modelVersionWarning')}
                </span>
              {/if}
              {#if wildcard}
                <span class="mt-1 block text-[11px] leading-relaxed text-ink-400">
                  {t('settings.metadata.patternLocked', { pattern: wildcard })}
                </span>
              {/if}
            </span>
          </label>
        {/each}
        {#if !visibleInventory.length}
          <p class="rounded-lg border border-ink-750 bg-ink-850 px-3 py-4 text-xs text-ink-400">
            {t('settings.metadata.filterEmpty')}
          </p>
        {/if}
      </div>
    {:else}
      <p class="rounded-lg border border-ink-750 bg-ink-850 px-3 py-4 text-xs text-ink-400">
        {t('settings.metadata.empty')}
      </p>
    {/if}

    {#if legacyEntries.length}
      <div class="pt-2">
        <p class="label">{t('settings.metadata.savedPatterns')}</p>
        <p class="mb-1.5 text-[11px] leading-relaxed text-ink-500">
          {t('settings.metadata.savedPatternsHint')}
        </p>
        <div class="space-y-1">
          {#each legacyEntries as entry (entry)}
            <label
              class="flex items-center gap-2.5 rounded-lg border border-ink-750 bg-ink-850 px-3 py-2"
            >
              <input
                type="checkbox"
                class="accent-teal-500"
                checked
                disabled={saving}
                aria-label={t('settings.metadata.toggle', { field: entry })}
                onchange={(event) => {
                  if (!(event.currentTarget as HTMLInputElement).checked) {
                    void removeLegacyEntry(entry)
                  }
                }}
              />
              <span class="mono text-xs text-ink-100">{entry}</span>
            </label>
          {/each}
        </div>
      </div>
    {/if}

    <div class="border-t border-ink-750 pt-4">
      <p class="label">{t('settings.metadata.promptExclusions.title')}</p>
      <p class="mb-2 text-[11px] leading-relaxed text-ink-500">
        {t('settings.metadata.promptExclusions.explain')}
      </p>
      <form
        class="flex gap-2"
        onsubmit={(event) => {
          event.preventDefault()
          void addPromptExclusion()
        }}
      >
        <input
          class="input mono"
          placeholder={t('settings.metadata.promptExclusions.placeholder')}
          aria-label={t('settings.metadata.promptExclusions.expression')}
          bind:value={promptExpression}
          disabled={saving}
        />
        <button class="btn btn-primary btn-sm" disabled={saving || !promptExpression.trim()}>
          {t('settings.metadata.promptExclusions.add')}
        </button>
      </form>

      {#if promptExclusions.length}
        <div class="mt-2 space-y-1">
          {#each promptExclusions as expression (expression)}
            <div
              class="flex items-center gap-2.5 rounded-lg border border-ink-750 bg-ink-850 px-3 py-2"
            >
              <span class="mono min-w-0 flex-1 break-all text-xs text-ink-100">{expression}</span>
              <button
                class="btn btn-danger btn-sm"
                disabled={saving}
                aria-label={t('settings.metadata.promptExclusions.remove', { expression })}
                title={t('settings.metadata.promptExclusions.remove', { expression })}
                onclick={() => removePromptExclusion(expression)}
              >✕</button>
            </div>
          {/each}
        </div>
      {:else}
        <p class="mt-2 text-[11px] text-ink-500">
          {t('settings.metadata.promptExclusions.empty')}
        </p>
      {/if}
    </div>
  </div>
</Panel>
