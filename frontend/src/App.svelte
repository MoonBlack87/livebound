<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from './api/client'
  import JobBar from './components/JobBar.svelte'
  import Toasts from './components/Toasts.svelte'
  import ImageDrawer from './components/ImageDrawer.svelte'
  import PostEditor from './components/PostEditor.svelte'
  import AccountFooter from './components/AccountFooter.svelte'
  import SetupNotice from './components/SetupNotice.svelte'
  import ArchivePage from './pages/ArchivePage.svelte'
  import BoardPage from './pages/BoardPage.svelte'
  import CalendarPage from './pages/CalendarPage.svelte'
  import DuplicatesPage from './pages/DuplicatesPage.svelte'
  import LibraryPage from './pages/LibraryPage.svelte'
  import ModelsPage from './pages/ModelsPage.svelte'
  import ReviewPage from './pages/ReviewPage.svelte'
  import SettingsPage from './pages/SettingsPage.svelte'
  import SetupPage from './pages/SetupPage.svelte'
  import TrashPage from './pages/TrashPage.svelte'
  import type { SetupStatus } from './types/api'
  import { setLocale, t, type Locale } from './i18n/index.svelte'
  import { app, setAccount, setSettings, setView, type View } from './state/store.svelte'
  import lockup from './assets/brand/livebound-lockup-horizontal.svg'
  import Icon, { type IconName } from './components/ui/Icon.svelte'

  const NAV: { view: View; label: string; icon: IconName; hint: string }[] = [
    { view: 'library', label: 'nav.library', icon: 'library', hint: 'nav.library.hint' },
    { view: 'duplicates', label: 'nav.duplicates', icon: 'duplicates', hint: 'nav.duplicates.hint' },
    { view: 'trash', label: 'nav.trash', icon: 'trash', hint: 'nav.trash.hint' },
    { view: 'models', label: 'nav.models', icon: 'models', hint: 'nav.models.hint' },
    { view: 'board', label: 'nav.board', icon: 'board', hint: 'nav.board.hint' },
    { view: 'calendar', label: 'nav.calendar', icon: 'schedule', hint: 'nav.calendar.hint' },
    { view: 'review', label: 'nav.review', icon: 'review', hint: 'nav.review.hint' },
    { view: 'archive', label: 'nav.archive', icon: 'archive', hint: 'nav.archive.hint' },
  ]
  const SETTINGS = { view: 'settings' as const, label: 'nav.settings', icon: 'settings' as const, hint: 'nav.settings.hint' }

  let setupState = $state<'loading' | 'required' | 'complete' | 'failed'>('loading')
  let setupStatus = $state<SetupStatus | null>(null)
  let setupError = $state('')

  onMount(async () => {
    try {
      setupStatus = await api.setupStatus()
      setupState = setupStatus.setup_required ? 'required' : 'complete'
    } catch (error) {
      setupError = (error as Error).message
      setupState = 'failed'
    }
  })

  if (new URL(window.location.href).searchParams.has('settings')) {
    setView('settings')
  }

  function showView(view: View): void {
    if (view !== 'settings') {
      const url = new URL(window.location.href)
      if (url.searchParams.has('settings')) {
        url.searchParams.delete('settings')
        window.history.replaceState(window.history.state, '', url)
      }
    }
    setView(view)
  }

  $effect(() => {
    if (setupState !== 'complete') return
    // Reads on every revision: settings and account are what the whole shell
    // renders from, and both change from several places.
    void app.revision
    api.settings().then((settings) => {
      setSettings(settings)
      setLocale((settings.ui_language as Locale) || 'en')
      if (app.view === 'trash' && !settings.trash_folder) setView('library')
    }).catch(() => undefined)
    api.account().then(setAccount).catch(() => undefined)
  })

  $effect(() => {
    if (app.view === 'trash' && !app.settings?.trash_folder) setView('library')
  })
</script>

{#if setupState === 'loading'}
  <div class="flex h-full items-center justify-center text-sm text-ink-400">
    {t('firstStart.loading')}
  </div>
{:else if setupState === 'failed'}
  <div class="flex h-full items-center justify-center p-6">
    <p class="panel max-w-lg p-4 text-sm" role="alert" style="color: var(--color-failed);">
      {t('firstStart.loadFailed', { message: setupError })}
    </p>
  </div>
{:else if setupState === 'required' && setupStatus}
  <SetupPage
    initial={setupStatus}
    onComplete={() => {
      setupState = 'complete'
    }}
  />
{:else}
<div class="flex h-full">
  <aside class="flex w-56 shrink-0 flex-col border-r border-ink-800 bg-ink-900">
    <div class="flex flex-col items-center gap-4 px-4 py-4">
      <img
        src={lockup}
        alt="Livebound"
        width="140"
        class="h-auto w-[140px]"
      />
      <p class="text-[10.5px] text-ink-400">{t('nav.subtitle')}</p>
    </div>

    <nav class="flex-1 px-2">
      {#each NAV.filter((item) => item.view !== 'trash' || Boolean(app.settings?.trash_folder)) as item (item.view)}
        {@const active = app.view === item.view}
        <button
          onclick={() => showView(item.view)}
          title={t(item.hint)}
          class="mb-0.5 flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors {active
            ? 'bg-ink-800 font-medium text-ink-100'
            : 'text-ink-300 hover:bg-ink-850 hover:text-ink-100'}"
        >
          <span
            class="flex w-4 justify-center"
            style={active ? 'color: var(--color-accent-400);' : ''}
          >
            <Icon name={item.icon} size={18} />
          </span>
          {t(item.label)}
        </button>
      {/each}
    </nav>

    <div class="px-2">
      <button
        onclick={() => showView(SETTINGS.view)}
        title={t(SETTINGS.hint)}
        class="mb-0.5 flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors {app.view === SETTINGS.view
          ? 'bg-ink-800 font-medium text-ink-100'
          : 'text-ink-300 hover:bg-ink-850 hover:text-ink-100'}"
      >
        <span
          class="flex w-4 justify-center"
          style={app.view === SETTINGS.view ? 'color: var(--color-accent-400);' : ''}
        >
          <Icon name={SETTINGS.icon} size={18} />
        </span>
        {t(SETTINGS.label)}
      </button>
    </div>

    <AccountFooter />
  </aside>

  <main class="flex min-w-0 flex-1 flex-col">
    <JobBar />
    <div class="min-h-0 flex-1 overflow-y-auto">
      {#if app.view === 'library'}
        <LibraryPage />
      {:else if app.view === 'duplicates'}
        <DuplicatesPage />
      {:else if app.view === 'trash'}
        <TrashPage />
      {:else if app.view === 'models'}
        <ModelsPage />
      {:else if app.view === 'board'}
        <BoardPage />
      {:else if app.view === 'calendar'}
        <CalendarPage />
      {:else if app.view === 'review'}
        <ReviewPage />
      {:else if app.view === 'archive'}
        <ArchivePage />
      {:else if app.view === 'settings'}
        <SettingsPage />
      {/if}
    </div>
  </main>

  {#if app.editingPostId !== null}
    <PostEditor postId={app.editingPostId} />
  {/if}
  {#if app.inspectingImageId !== null}
    <ImageDrawer imageId={app.inspectingImageId} />
  {/if}
  <Toasts />
  <SetupNotice />
</div>
{/if}
