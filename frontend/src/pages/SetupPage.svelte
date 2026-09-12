<script lang="ts">
  import { onMount } from 'svelte'
  import { api } from '../api/client'
  import lockup from '../assets/brand/livebound-lockup-horizontal.svg'
  import bearStart from '../assets/moonbear/start.png'
  import bearWelcome from '../assets/moonbear/welcome.png'
  import DirBrowser from '../components/settings/DirBrowser.svelte'
  import Icon from '../components/ui/Icon.svelte'
  import Spinner from '../components/ui/Spinner.svelte'
  import { setLocale, t, type Locale } from '../i18n/index.svelte'
  import type { OauthStatus, Settings, SetupStatus } from '../types/api'

  let {
    initial,
    onComplete,
  }: {
    initial: SetupStatus
    onComplete: () => void
  } = $props()

  /**
   * The wizard is a list of steps, not a chain of numbers.
   *
   * Which step is current is derived from what the server holds, never
   * incremented - so a reload, a browser back button and an OAuth round trip all
   * land where the data says they should. Adding a step is adding an entry.
   *
   * `folder` steps share one screen and one directory browser; `done` tells the
   * derivation whether the step is finished, and `save` is what its picker
   * calls. Optional steps can be skipped, which lives in the session rather than
   * in the database: skipping is "not now", not a setting.
   */
  type StepKey = 'data' | 'connection' | 'images' | 'archive' | 'adopted' | 'trash' | 'models'

  type Step = {
    key: StepKey
    required: boolean
    kind: 'data' | 'connection' | 'folder'
    done: (settings: Settings | null, oauth: OauthStatus | null) => boolean
    save?: (path: string) => Promise<unknown>
    /** Passed to DirBrowser, which offers to create a missing archive folder. */
    asArchive?: boolean
  }

  const STEPS: Step[] = [
    {
      key: 'data',
      required: true,
      kind: 'data',
      done: () => dataDirectoryReady,
    },
    {
      key: 'connection',
      required: true,
      kind: 'connection',
      done: (_settings, oauth) => oauth?.state === 'connected',
    },
    {
      key: 'images',
      required: true,
      kind: 'folder',
      done: (settings) => !!settings?.setup.source_folder,
      save: (path) => api.addSource({ path, is_archive: false }),
    },
    {
      key: 'archive',
      required: true,
      kind: 'folder',
      asArchive: true,
      done: (settings) => !!settings?.setup.archive_folder,
      save: (path) => api.addSource({ path, is_archive: true }),
    },
    {
      key: 'adopted',
      required: false,
      kind: 'folder',
      done: (settings) => !!settings?.setup.adopted_folder,
      save: (path) => api.saveSettings({ adopted_folder: path }),
    },
    {
      key: 'trash',
      required: false,
      kind: 'folder',
      // Not part of the server's readiness block, so it is read straight off the
      // setting it writes.
      done: (settings) => !!settings?.trash_folder,
      save: (path) => api.saveSettings({ trash_folder: path }),
    },
    {
      key: 'models',
      required: false,
      kind: 'folder',
      done: (settings) => !!settings?.setup.model_root,
      save: (path) => api.addModelRoot({ path }),
    },
  ]

  let dataDirectoryReady = $state(false)
  let currentKey = $state<StepKey | 'finished'>('data')
  let skipped = $state(new Set<StepKey>())
  let dataPath = $state('')
  let backup = $state<File | undefined>()
  let settings = $state<Settings | null>(null)
  let oauth = $state<OauthStatus | null>(null)
  let busy = $state(false)
  let error = $state('')
  let browsing = $state(false)
  let usagePingEnabled = $state(false)

  const current = $derived(STEPS.find((entry) => entry.key === currentKey) ?? null)
  // A plain function, not a $derived: it reads the reactive state each time it
  // is called, which is what the markup needs when it asks about many steps.
  const isDone = (entry: Step) => entry.done(settings, oauth)
  const requiredSteps = $derived(STEPS.filter((entry) => entry.required))
  const requiredLeft = $derived(requiredSteps.filter((entry) => !entry.done(settings, oauth)).length)
  const position = $derived(current ? STEPS.indexOf(current) + 1 : STEPS.length)
  /** Done or deliberately skipped both count as settled: the bar shows how much
      is behind you, not how much is configured. */
  const settled = $derived(
    STEPS.filter((entry) => entry.done(settings, oauth) || skipped.has(entry.key)).length,
  )

  onMount(() => {
    dataPath = initial.default_path
    dataDirectoryReady = initial.data_directory_ready
    if (dataDirectoryReady) void loadProgress()
  })

  /**
   * The first step that still wants attention: a required one that is not done,
   * otherwise an optional one that is neither done nor skipped. When none is
   * left, the wizard is finished.
   */
  function nextStep(): StepKey | 'finished' {
    for (const entry of STEPS) {
      if (entry.done(settings, oauth)) continue
      if (entry.required || !skipped.has(entry.key)) return entry.key
    }
    return 'finished'
  }

  async function loadProgress() {
    busy = true
    error = ''
    try {
      ;[settings, oauth] = await Promise.all([api.settings(), api.oauthStatus()])
      setLocale((settings.ui_language as Locale) || 'en')
      currentKey = nextStep()
    } catch (caught) {
      error = (caught as Error).message
    } finally {
      busy = false
    }
  }

  function skip() {
    if (!current) return
    skipped = new Set(skipped).add(current.key)
    currentKey = nextStep()
  }

  async function chooseDataDirectory() {
    busy = true
    error = ''
    try {
      await api.completeSetup(dataPath.trim(), backup)
      dataDirectoryReady = true
      await loadProgress()
    } catch (caught) {
      error = (caught as Error).message
      busy = false
    }
  }

  async function connect() {
    busy = true
    error = ''
    try {
      const { authorize_url } = await api.oauthStart()
      window.location.href = authorize_url
    } catch (caught) {
      error = (caught as Error).message
      busy = false
    }
  }

  async function pickFolder(path: string) {
    if (!current?.save) return
    busy = true
    error = ''
    try {
      await current.save(path)
      await loadProgress()
    } catch (caught) {
      error = (caught as Error).message
      busy = false
    }
  }

  async function finish() {
    busy = true
    error = ''
    try {
      await api.saveSettings({ usage_ping_enabled: usagePingEnabled })
      await api.finishSetup()
      onComplete()
    } catch (caught) {
      error = (caught as Error).message
      busy = false
    }
  }
</script>

<div class="min-h-full bg-ink-950 px-4 py-8 sm:px-8">
  <div class="mx-auto max-w-4xl">
    <header class="mb-7 flex justify-center">
      <img src={lockup} alt="Livebound" width="180" class="h-auto w-[180px]" />
    </header>

    <div class="grid gap-5 md:grid-cols-[13rem_minmax(0,1fr)]">
      <aside class="panel h-fit p-4">
        <p class="mb-2 text-xs font-semibold text-ink-200">{t('firstStart.progress')}</p>
        <div
          class="mb-4 h-1.5 overflow-hidden rounded-full bg-ink-800"
          role="progressbar"
          aria-valuenow={settled}
          aria-valuemin="0"
          aria-valuemax={STEPS.length}
        >
          <div
            class="h-full rounded-full bg-accent-500 transition-all duration-300"
            style="width: {(settled / STEPS.length) * 100}%"
          ></div>
        </div>
        <ol class="space-y-2">
          {#each STEPS as entry (entry.key)}
            {@const finished = isDone(entry)}
            {@const active = currentKey === entry.key}
            <li
              class="flex items-center gap-2 text-xs {active
                ? 'text-ink-100'
                : finished
                  ? 'text-ink-300'
                  : 'text-ink-400'}"
            >
              <span
                class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-[11px] {finished
                  ? 'border-accent-500 bg-accent-600 text-white'
                  : active
                    ? 'border-accent-400 text-accent-300'
                    : 'border-ink-700'}"
              >
                {#if finished}✓{:else if skipped.has(entry.key)}–{:else}{STEPS.indexOf(entry) + 1}{/if}
              </span>
              <span class="min-w-0">
                {t(`firstStart.steps.${entry.key}`)}
                {#if !entry.required}
                  <span class="block text-[10px] text-ink-500">{t('firstStart.optional')}</span>
                {/if}
              </span>
            </li>
          {/each}
        </ol>

        <div
          class="mt-5 rounded-lg border p-3"
          style="border-color: color-mix(in srgb, var(--color-ready) 42%, transparent); background: color-mix(in srgb, var(--color-ready) 9%, transparent);"
        >
          <div class="mb-1 flex items-center gap-2 text-xs font-semibold text-ink-100">
            <Icon name="warning" size={16} />
            {t('firstStart.files.title')}
          </div>
          <p class="text-[11px] leading-relaxed text-ink-300">{t('firstStart.files.warning')}</p>
        </div>
      </aside>

      <main class="panel-raised min-h-[30rem] p-6 sm:p-8">
        {#if currentKey === 'data'}
          <div class="-mx-6 -mt-6 mb-7 flex items-start gap-4 border-b border-ink-750 bg-ink-850 px-6 py-5 sm:-mx-8 sm:-mt-8 sm:px-8">
            <img src={bearWelcome} alt="" width="84" height="84" class="hidden h-21 w-21 shrink-0 sm:block" />
            <div>
              <h2 class="text-lg font-semibold text-ink-100">{t('firstStart.welcome.title')}</h2>
              <p class="mt-1.5 max-w-xl text-sm leading-relaxed text-ink-300">
                {t('firstStart.welcome.explain')}
              </p>
              <p class="mt-2 text-[11px] text-teal-400/80">{t('firstStart.welcome.time')}</p>
            </div>
          </div>
        {/if}

        {#if current}
          <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <p class="label">
              {t('firstStart.step', { current: position, total: STEPS.length })}
            </p>
            {#if !current.required}
              <span
                class="rounded-full border border-ink-700 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-ink-400"
              >
                {t('firstStart.optional')}
              </span>
            {/if}
          </div>
          <h1 class="text-xl font-semibold text-ink-100">
            {t(`firstStart.${current.key}.title`)}
          </h1>
          <p class="mt-2 max-w-xl text-sm leading-relaxed text-ink-300">
            {t(`firstStart.${current.key}.explain`)}
          </p>
        {/if}

        {#if current?.kind === 'data'}
          <div class="mt-6">
            <label class="label" for="first-start-data-path">{t('firstStart.data.path')}</label>
            <input
              id="first-start-data-path"
              class="input mono"
              bind:value={dataPath}
              spellcheck="false"
            />
            <p class="mt-1.5 text-[11px] leading-relaxed text-ink-500">
              {t('firstStart.data.pathHint')}
            </p>
          </div>

          <div class="mt-5 border-t border-ink-750 pt-5">
            <label class="label" for="first-start-backup">{t('firstStart.data.backup')}</label>
            <input
              id="first-start-backup"
              type="file"
              accept=".db,application/octet-stream,application/vnd.sqlite3"
              class="block w-full text-xs text-ink-400 file:mr-3 file:rounded-lg file:border file:border-ink-700 file:bg-ink-800 file:px-3 file:py-2 file:text-xs file:text-ink-100"
              onchange={(event) => {
                backup = (event.currentTarget as HTMLInputElement).files?.[0]
              }}
            />
            <p class="mt-1.5 text-[11px] leading-relaxed text-ink-500">
              {t('firstStart.data.backupHint')}
            </p>
          </div>

          <div class="mt-7 flex justify-end">
            <button
              class="btn btn-primary"
              disabled={busy || !dataPath.trim()}
              onclick={chooseDataDirectory}
            >
              {#if busy}<Spinner />{:else}{t('firstStart.data.accept')}{/if}
            </button>
          </div>
        {:else if current?.kind === 'connection'}
          <div class="mt-7 rounded-lg border border-ink-750 bg-ink-850 p-4">
            <p class="text-xs leading-relaxed text-ink-300">
              {t('firstStart.connection.permissions')}
            </p>
            <button class="btn btn-primary mt-4" disabled={busy} onclick={connect}>
              {#if busy}<Spinner />{:else}{t('settings.connection.connect')}{/if}
            </button>
          </div>
        {:else if current?.kind === 'folder'}
          {#if !current.required}
            <!-- An optional step says what it costs to skip it. Somebody who does
                 not know what they are giving up cannot decide. -->
            <div class="mt-5 rounded-lg border border-ink-750 bg-ink-850 p-4">
              <p class="label mb-1">{t('firstStart.without')}</p>
              <p class="text-[11px] leading-relaxed text-ink-300">
                {t(`firstStart.${current.key}.without`)}
              </p>
            </div>
          {/if}

          <div class="mt-7 flex flex-wrap items-center gap-3">
            <button class="btn btn-primary" disabled={busy} onclick={() => (browsing = true)}>
              {t(`firstStart.${current.key}.choose`)}
            </button>
            {#if !current.required}
              <button class="btn btn-ghost" disabled={busy} onclick={skip}>
                {t('firstStart.skip')}
              </button>
            {/if}
          </div>
        {:else}
          <div class="flex items-start gap-4">
            <img src={bearStart} alt="" width="72" height="72" class="hidden shrink-0 sm:block" />
            <div>
              <h1 class="text-xl font-semibold text-ink-100">{t('firstStart.finished.title')}</h1>
              <p class="mt-1 text-sm leading-relaxed text-ink-300">
                {t('firstStart.finished.explain')}
              </p>
            </div>
          </div>

          <ul class="mt-6 grid gap-2 sm:grid-cols-2">
            {#each STEPS as entry (entry.key)}
              {@const finished = isDone(entry)}
              <li
                class="flex items-center gap-2 rounded-lg border px-3 py-2 text-xs {finished
                  ? 'border-ink-750 bg-ink-850 text-ink-200'
                  : 'border-dashed border-ink-750 text-ink-500'}"
              >
                <span class={finished ? 'text-accent-400' : 'text-ink-600'}>{finished ? '✓' : '–'}</span>
                {t(`firstStart.steps.${entry.key}`)}
              </li>
            {/each}
          </ul>

          <p class="mt-3 text-[11px] leading-relaxed text-ink-500">{t('firstStart.later')}</p>

          <label class="mt-6 flex cursor-pointer items-start gap-3 rounded-lg border border-ink-700 bg-ink-850 p-4">
            <input
              type="checkbox"
              class="mt-0.5 accent-teal-500"
              bind:checked={usagePingEnabled}
            />
            <img src={bearWelcome} alt="" width="44" height="44" class="mt-0.5 shrink-0" />
            <span>
              <span class="block text-xs font-medium text-ink-100">{t('firstStart.ping.label')}</span>
              <span class="mt-1 block text-[11px] leading-relaxed text-ink-400">
                {t('firstStart.ping.explain')}
              </span>
              <span class="mt-1 block text-[11px] leading-relaxed text-teal-400/80">
                {t('settings.usagePing.why')}
              </span>
            </span>
          </label>

          <div class="mt-7 flex justify-end">
            <button class="btn btn-primary" disabled={busy || requiredLeft > 0} onclick={finish}>
              {#if busy}<Spinner />{:else}{t('firstStart.finished.open')}{/if}
            </button>
          </div>
        {/if}

        {#if error}
          <p class="mt-4 text-xs" role="alert" style="color: var(--color-failed);">{error}</p>
        {/if}
      </main>
    </div>
  </div>
</div>

{#if browsing && current?.kind === 'folder'}
  {@const step = current}
  <DirBrowser
    asArchive={step.asArchive ?? false}
    title={t(`firstStart.${step.key}.choose`)}
    onClose={() => (browsing = false)}
    onPick={(path) => {
      browsing = false
      void pickFolder(path)
    }}
  />
{/if}
