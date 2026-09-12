<script lang="ts">
  import AboutPanel from '../components/settings/AboutPanel.svelte'
  import AdoptedFolderPanel from '../components/settings/AdoptedFolderPanel.svelte'
  import ArchivePanel from '../components/settings/ArchivePanel.svelte'
  import BackupPanel from '../components/settings/BackupPanel.svelte'
  import BehaviourPanel from '../components/settings/BehaviourPanel.svelte'
  import ConnectionPanel from '../components/settings/ConnectionPanel.svelte'
  import DataDirPanel from '../components/settings/DataDirPanel.svelte'
  import LlmPanel from '../components/settings/LlmPanel.svelte'
  import MetadataPanel from '../components/settings/MetadataPanel.svelte'
  import ModelRootsPanel from '../components/settings/ModelRootsPanel.svelte'
  import SourcesPanel from '../components/settings/SourcesPanel.svelte'
  import TrashPanel from '../components/settings/TrashPanel.svelte'
  import { t } from '../i18n/index.svelte'

  const AREAS = [
    { value: 'general', label: 'settings.area.general' },
    { value: 'images', label: 'settings.area.images' },
    { value: 'metadata', label: 'settings.area.metadata' },
    { value: 'models', label: 'settings.area.models' },
    { value: 'civitai', label: 'settings.area.civitai' },
    { value: 'local-llm', label: 'settings.area.localLlm' },
    { value: 'about', label: 'settings.area.about' },
  ] as const

  type Area = (typeof AREAS)[number]['value']

  function areaFromUrl(): Area {
    const value = new URL(window.location.href).searchParams.get('settings')
    return AREAS.some((area) => area.value === value) ? (value as Area) : 'general'
  }

  function hrefFor(area: Area): string {
    const url = new URL(window.location.href)
    url.searchParams.set('settings', area)
    return `${url.pathname}${url.search}${url.hash}`
  }

  let activeArea = $state<Area>(areaFromUrl())

  $effect(() => {
    const url = new URL(window.location.href)
    if (url.searchParams.get('settings') !== activeArea) {
      url.searchParams.set('settings', activeArea)
      window.history.replaceState(window.history.state, '', url)
    }
  })

  function selectArea(event: MouseEvent, area: Area): void {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return
    }
    event.preventDefault()
    activeArea = area
  }
</script>

<div class="mx-auto max-w-3xl p-5">
  <h1 class="mb-3 text-sm font-semibold">{t('nav.settings')}</h1>

  <nav class="mb-4 flex flex-wrap gap-x-1 border-b border-ink-750" aria-label={t('nav.settings')}>
    {#each AREAS as area (area.value)}
      {@const active = activeArea === area.value}
      <a
        href={hrefFor(area.value)}
        aria-current={active ? 'page' : undefined}
        onclick={(event) => selectArea(event, area.value)}
        class="-mb-px border-b-2 px-3 py-2 text-xs font-medium transition-colors {active
          ? 'border-accent-400 text-ink-100'
          : 'border-transparent text-ink-400 hover:border-ink-600 hover:text-ink-200'}"
      >
        {t(area.label)}
      </a>
    {/each}
  </nav>

  <div class="space-y-4">
    {#if activeArea === 'general'}
      <BehaviourPanel />
      <DataDirPanel />
      <BackupPanel />
    {:else if activeArea === 'images'}
      <SourcesPanel />
      <ArchivePanel />
      <AdoptedFolderPanel />
      <TrashPanel />
    {:else if activeArea === 'metadata'}
      <MetadataPanel />
    {:else if activeArea === 'models'}
      <ModelRootsPanel />
    {:else if activeArea === 'civitai'}
      <ConnectionPanel />
    {:else if activeArea === 'local-llm'}
      <LlmPanel />
    {:else if activeArea === 'about'}
      <AboutPanel />
    {/if}
  </div>
</div>
