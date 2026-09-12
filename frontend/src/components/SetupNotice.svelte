<script lang="ts">
  import { t } from '../i18n/index.svelte'
  import { app, setView } from '../state/store.svelte'

  type Area = 'images' | 'models' | 'civitai' | 'archive'
  type MissingSetting = {
    key: 'connection' | 'source' | 'archive' | 'adopted' | 'trash' | 'models'
    area: Area
    optional?: boolean
  }

  const missing = $derived.by(() => {
    const settings = app.settings
    if (!settings) return []
    const items: MissingSetting[] = []
    if (settings.civitai_auth_mode === 'none') {
      items.push({ key: 'connection', area: 'civitai' })
    }
    // Required and optional here mean what they mean in the first-start wizard
    // (`SetupPage.svelte`). The two disagreeing is how somebody ends up being
    // nagged for something they deliberately skipped - or not warned about
    // something the wizard insisted on.
    if (!settings.setup.source_folder) items.push({ key: 'source', area: 'images' })
    if (!settings.setup.archive_folder) items.push({ key: 'archive', area: 'archive' })
    if (!settings.setup.adopted_folder) {
      items.push({ key: 'adopted', area: 'images', optional: true })
    }
    if (!settings.trash_folder) {
      items.push({ key: 'trash', area: 'images', optional: true })
    }
    if (!settings.setup.model_root) {
      items.push({ key: 'models', area: 'models', optional: true })
    }
    return items
  })

  function openSettings(area: Area): void {
    const url = new URL(window.location.href)
    url.searchParams.set('settings', area)
    // Replace, never push: this application has no popstate listener, so a new
    // entry would let Back strip the query while the view stays where it is.
    // SettingsPage navigates the same way.
    window.history.replaceState(window.history.state, '', url)
    setView('settings')
  }
</script>

{#if missing.length && app.view !== 'settings'}
  <aside
    class="panel-raised animate-in fixed bottom-4 left-60 right-4 z-40 max-w-2xl overflow-hidden"
    aria-labelledby="setup-notice-title"
  >
    <header class="border-b border-ink-750 px-4 py-3">
      <h2 id="setup-notice-title" class="text-sm font-semibold text-ink-100">
        {t('setup.title')}
      </h2>
      <p class="mt-0.5 text-[11px] text-ink-400">{t('setup.explain')}</p>
    </header>
    <ul class="grid gap-2 p-3 sm:grid-cols-2">
      {#each missing as item (item.key)}
        <li class="flex items-center gap-3 rounded-lg border border-ink-750 bg-ink-850 px-3 py-2">
          <div class="min-w-0 flex-1">
            <p class="flex items-center gap-1.5 text-xs font-medium text-ink-100">
              {t(`setup.${item.key}.title`)}
              {#if item.optional}
                <span class="chip text-[9px]">{t('setup.optional')}</span>
              {/if}
            </p>
            <p class="mt-0.5 text-[10.5px] leading-relaxed text-ink-400">
              {t(`setup.${item.key}.consequence`)}
            </p>
          </div>
          <button class="btn btn-sm shrink-0" onclick={() => openSettings(item.area)}>
            {t('setup.configure')}
          </button>
        </li>
      {/each}
    </ul>
  </aside>
{/if}
