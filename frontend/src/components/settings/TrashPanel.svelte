<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { app, setSettings, toast } from '../../state/store.svelte'
  import DirBrowser from './DirBrowser.svelte'
  import Panel from '../ui/Panel.svelte'

  let browsing = $state(false)
  let saving = $state(false)
  let filenamePattern = $state('')
  const patterns = $derived(app.settings?.generation_time_patterns ?? [])

  async function saveFolder(path: string) {
    saving = true
    try {
      setSettings(await api.saveSettings({ trash_folder: path }))
      toast('success', t('settings.trash.folderSaved'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      saving = false
    }
  }

  async function addPattern() {
    const pattern = filenamePattern.trim()
    if (!pattern) return
    saving = true
    try {
      setSettings(
        await api.saveSettings({
          generation_time_patterns: [...new Set([...patterns, pattern])],
        }),
      )
      filenamePattern = ''
      toast('success', t('settings.trash.patternsSaved'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      saving = false
    }
  }

  async function removePattern(pattern: string) {
    saving = true
    try {
      setSettings(
        await api.saveSettings({
          generation_time_patterns: patterns.filter((value) => value !== pattern),
        }),
      )
      toast('success', t('settings.trash.patternsSaved'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      saving = false
    }
  }
</script>

<Panel title={t('settings.trash.title')}>
  {#snippet actions()}
    <button class="btn btn-sm" disabled={saving} onclick={() => (browsing = true)}>
      {app.settings?.trash_folder ? t('settings.trash.changeFolder') : t('settings.trash.chooseFolder')}
    </button>
  {/snippet}

  <div class="space-y-4">
    <p class="text-xs leading-relaxed text-ink-300">{t('settings.trash.explain')}</p>
    {#if app.settings?.trash_folder}
      <p class="mono break-all rounded-lg border border-ink-750 bg-ink-850 px-3 py-2 text-xs text-ink-100">
        {app.settings.trash_folder}
      </p>
    {:else}
      <p class="text-xs text-ink-400">{t('settings.trash.notConfigured')}</p>
    {/if}

    <div class="border-t border-ink-750 pt-4">
      <p class="label">{t('settings.trash.patternsTitle')}</p>
      <p class="mb-2 text-[11px] leading-relaxed text-ink-500">
        {t('settings.trash.patternsExplain')}
      </p>
      <form
        class="flex gap-2"
        onsubmit={(event) => {
          event.preventDefault()
          void addPattern()
        }}
      >
        <input
          class="input mono"
          placeholder={t('settings.trash.patternPlaceholder')}
          aria-label={t('settings.trash.patternLabel')}
          bind:value={filenamePattern}
          disabled={saving}
        />
        <button class="btn btn-primary btn-sm" disabled={saving || !filenamePattern.trim()}>
          {t('settings.trash.addPattern')}
        </button>
      </form>

      {#if patterns.length}
        <div class="mt-2 space-y-1">
          {#each patterns as pattern (pattern)}
            <div class="flex items-center gap-2.5 rounded-lg border border-ink-750 bg-ink-850 px-3 py-2">
              <span class="mono min-w-0 flex-1 break-all text-xs text-ink-100">{pattern}</span>
              <button
                class="btn btn-danger btn-sm"
                disabled={saving}
                aria-label={t('settings.trash.removePattern', { pattern })}
                title={t('settings.trash.removePattern', { pattern })}
                onclick={() => removePattern(pattern)}
              >✕</button>
            </div>
          {/each}
        </div>
      {:else}
        <p class="mt-2 text-[11px] text-ink-500">{t('settings.trash.noPatterns')}</p>
      {/if}
    </div>
  </div>

  {#if browsing}
    <DirBrowser
      title={t('settings.trash.pickTitle')}
      onPick={(path) => void saveFolder(path)}
      onClose={() => (browsing = false)}
    />
  {/if}
</Panel>
