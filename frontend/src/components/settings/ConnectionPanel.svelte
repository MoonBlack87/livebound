<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import type { OauthStatus } from '../../types/api'
  import { app, invalidate, toast } from '../../state/store.svelte'
  import Panel from '../ui/Panel.svelte'
  import Spinner from '../ui/Spinner.svelte'

  /** The permission bits this application asks for, in the order CivitAI
      defines them. Kept here rather than fetched: they are what the consent
      screen will show, and they do not change per account. */
  const GRANTED = [
    { bit: 1, key: 'settings.connection.scope.userRead' },
    { bit: 4, key: 'settings.connection.scope.modelsRead' },
    { bit: 32, key: 'settings.connection.scope.mediaRead' },
    { bit: 64, key: 'settings.connection.scope.mediaWrite' },
    { bit: 128, key: 'settings.connection.scope.mediaDelete' },
  ]
  const DOMAINS = ['https://civitai.red', 'https://civitai.com']

  let status = $state<OauthStatus | null>(null)
  let busy = $state(false)
  let outcomeReported = false

  // Read once on mount. This also reports the outcome of a return trip: the
  // callback redirects back here, so a denied or failed authorization is what
  // the user finds waiting rather than a dead browser tab.
  $effect(() => {
    void load()
  })

  $effect(() => {
    // The initial settings load also selects the language for this message.
    if (!status || !app.settings || outcomeReported) return
    outcomeReported = true
    if (status.state === 'denied') toast('info', t('settings.connection.denied'))
    if (status.state === 'failed') {
      toast('error', status.error_code
        ? t(`error.${status.error_code}`)
        : t('settings.connection.failed'))
    }
  })

  async function load() {
    try {
      status = await api.oauthStatus()
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }

  async function connect() {
    busy = true
    try {
      const { authorize_url } = await api.oauthStart()
      // Same tab on purpose: the callback redirects back into this application,
      // and from a popup that return would land in the wrong window.
      window.location.href = authorize_url
    } catch (error) {
      toast('error', (error as Error).message)
      busy = false
    }
  }

  async function disconnect() {
    busy = true
    try {
      await api.oauthDisconnect()
      await load()
      invalidate()
      toast('info', t('settings.connection.disconnected'))
    } catch (error) {
      toast('error', (error as Error).message)
    } finally {
      busy = false
    }
  }

  async function pickDomain(base: string) {
    await api.saveSettings({ site_base: base })
    invalidate()
    toast('info', t('settings.domain.set', { domain: base.replace('https://', '') }))
  }

  const connected = $derived(status?.state === 'connected')
  const granted = $derived(status?.scope ?? 0)
  const username = $derived(app.account?.username ?? app.settings?.civitai_username)
  const siteBase = $derived(app.settings?.site_base ?? 'https://civitai.red')
</script>

<Panel title={t('settings.connection.title')}>
  <div class="space-y-3">
    {#if connected}
      <div class="rounded-lg border border-ink-750 bg-ink-850 px-3 py-2.5">
        <div class="flex flex-wrap items-center gap-2">
          <div class="min-w-0 flex-1">
            <p class="text-xs text-ink-100">
              {#if username}
                <a
                  class="underline decoration-dotted underline-offset-2"
                  href="{siteBase}/user/{encodeURIComponent(username)}"
                  target="_blank"
                  rel="noreferrer noopener"
                >{t('settings.connection.account', { username })}</a>
              {:else}
                {t('settings.connection.connected')}
              {/if}
            </p>
            <p class="mt-1 text-[11px] leading-relaxed text-ink-400">
              {t('settings.connection.accountHint')}
            </p>
            <p class="text-[11px] text-ink-400">{t('settings.connection.revokeHint')}</p>
          </div>
          <button class="btn btn-sm" disabled={busy} onclick={disconnect}>
            {#if busy}<Spinner />{:else}{t('settings.connection.disconnect')}{/if}
          </button>
        </div>
        <ul class="mt-2 space-y-1">
          {#each GRANTED as scope (scope.bit)}
            <li class="text-[11px] leading-relaxed text-ink-300">
              <span class="mono text-ink-200">{(granted & scope.bit) === scope.bit ? '✓' : '—'}</span
              >
              {t(scope.key)}
            </li>
          {/each}
        </ul>
        {#if app.account?.scopes?.known && app.account.scopes.problems.length}
          <div
            class="mt-2 rounded-lg border px-3 py-2.5"
            style="border-color: color-mix(in srgb, var(--color-ready) 45%, transparent);
                   background: color-mix(in srgb, var(--color-ready) 10%, transparent);"
          >
            <p class="text-xs font-medium text-ink-100">{t('settings.scopes.missing')}</p>
            <ul class="mt-1.5 space-y-1">
              {#each app.account.scopes.problems as problem (problem.scope)}
                <li class="text-[11px] leading-relaxed text-ink-300">
                  <span class="mono text-ink-200">{problem.scope}</span> — {problem.label}.
                  {problem.consequence}
                </li>
              {/each}
            </ul>
            <p class="mt-1.5 text-[11px] text-ink-500">{t('settings.scopes.fix')}</p>
          </div>
        {/if}
      </div>
    {:else}
      <div>
        <button class="btn btn-primary" disabled={busy} onclick={connect}>
          {#if busy}<Spinner />{:else}{t('settings.connection.connect')}{/if}
        </button>
        <p class="mt-1.5 text-xs leading-relaxed text-ink-500">
          {t('settings.connection.connectHint')}
        </p>
      </div>
    {/if}

    <div class="border-t border-ink-750 pt-3">
      <span class="label">{t('settings.domain.label')}</span>
      <div class="flex gap-1 rounded-lg border border-ink-700 bg-ink-950 p-1">
        {#each DOMAINS as base (base)}
          <button
            type="button"
            class="flex-1 rounded-md px-2 py-1.5 text-xs transition-colors {app.settings
              ?.site_base === base
              ? 'bg-ink-750 font-medium text-ink-100'
              : 'text-ink-400 hover:text-ink-200'}"
            onclick={() => pickDomain(base)}
          >
            {base.replace('https://', '')}{base === app.settings?.site_base_default
              ? ` ${t('settings.domain.default')}`
              : ''}
          </button>
        {/each}
      </div>
      <p class="mt-1.5 text-xs leading-relaxed text-ink-500">{t('settings.domain.hint')}</p>
    </div>
  </div>
</Panel>
