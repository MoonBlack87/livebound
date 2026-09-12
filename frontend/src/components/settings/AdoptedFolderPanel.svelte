<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { app, setSettings, toast } from '../../state/store.svelte'
  import ConfirmDialog from '../ui/ConfirmDialog.svelte'
  import DirBrowser from './DirBrowser.svelte'
  import Panel from '../ui/Panel.svelte'

  let browsing = $state(false)
  let removing = $state(false)
  let saving = $state(false)

  async function saveFolder(path: string) {
    saving = true
    try {
      setSettings(await api.saveSettings({ adopted_folder: path }))
      toast('success', t('settings.adopted.folderSaved'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      saving = false
    }
  }

  async function removeFolder() {
    removing = false
    await saveFolder('')
  }
</script>

<Panel title={t('settings.adopted.title')}>
  {#snippet actions()}
    {#if app.settings?.adopted_folder}
      <button class="btn btn-danger btn-sm" disabled={saving} onclick={() => (removing = true)}>
        {t('settings.adopted.removeFolder')}
      </button>
    {/if}
    <button class="btn btn-sm" disabled={saving} onclick={() => (browsing = true)}>
      {app.settings?.adopted_folder
        ? t('settings.adopted.changeFolder')
        : t('settings.adopted.chooseFolder')}
    </button>
  {/snippet}

  <div class="space-y-3">
    <p class="text-xs leading-relaxed text-ink-300">{t('settings.adopted.explain')}</p>
    {#if app.settings?.adopted_folder}
      <p
        class="mono break-all rounded-lg border border-ink-750 bg-ink-850 px-3 py-2 text-xs text-ink-100"
      >
        {app.settings.adopted_folder}
      </p>
    {:else}
      <p class="text-xs text-ink-400">{t('settings.adopted.notConfigured')}</p>
    {/if}
  </div>

  {#if browsing}
    <DirBrowser
      title={t('settings.adopted.pickTitle')}
      onPick={(path) => void saveFolder(path)}
      onClose={() => (browsing = false)}
    />
  {/if}

  {#if removing}
    <ConfirmDialog
      title={t('settings.adopted.removeTitle')}
      danger
      requireKey="ui.confirm.word.delete"
      confirmLabel={t('settings.adopted.removeFolder')}
      onCancel={() => (removing = false)}
      onConfirm={removeFolder}
    >
      {#snippet body()}
        <p>{t('settings.adopted.removeBody')}</p>
      {/snippet}
    </ConfirmDialog>
  {/if}
</Panel>
