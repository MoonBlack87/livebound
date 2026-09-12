<script lang="ts">
  import bearWelcome from '../../assets/moonbear/welcome.png'
  import { api } from '../../api/client'
  import { LOCALES, locale, setLocale, t, type Locale } from '../../i18n/index.svelte'
  import { app, invalidate } from '../../state/store.svelte'
  import Panel from '../ui/Panel.svelte'

  async function pickLanguage(value: string) {
    // Applied immediately, then persisted: waiting for the round trip would make
    // the switch feel broken.
    setLocale(value as Locale)
    await api.saveSettings({ ui_language: value })
    invalidate()
  }

  const watchRetryDelays = $derived(
    new Intl.ListFormat(locale(), { style: 'long', type: 'conjunction' }).format(
      (app.settings?.watch_post_retry_minutes ?? []).map(String),
    ),
  )
</script>

<Panel title={t('settings.behaviour.title')}>
  <div class="grid grid-cols-2 gap-3">
    <div class="col-span-2">
      <span class="label">{t('settings.language.label')}</span>
      <div class="flex gap-1 rounded-lg border border-ink-700 bg-ink-950 p-1">
        {#each LOCALES as entry (entry.value)}
          <button
            type="button"
            class="flex-1 rounded-md px-2 py-1.5 text-xs transition-colors {app.settings
              ?.ui_language === entry.value
              ? 'bg-ink-750 font-medium text-ink-100'
              : 'text-ink-400 hover:text-ink-200'}"
            onclick={() => pickLanguage(entry.value)}
          >
            {entry.label}
          </button>
        {/each}
      </div>
      <p class="mt-1 text-[11px] text-ink-500">{t('settings.language.hint')}</p>
    </div>

    <div>
      <span class="label">{t('settings.threshold.label')}</span>
      <input
        type="number"
        class="input"
        min="0"
        max="32"
        value={app.settings?.phash_threshold ?? 6}
        onblur={async (event) => {
          await api.saveSettings({
            phash_threshold: Number((event.currentTarget as HTMLInputElement).value),
          })
          invalidate()
        }}
      />
      <p class="mt-1 text-[11px] text-ink-500">{t('settings.threshold.hint')}</p>
    </div>
    <div>
      <span class="label">{t('settings.rateLimit.label')}</span>
      <input
        type="number"
        class="input"
        placeholder={t('settings.rateLimit.none')}
        value={app.settings?.rate_limit_posts_per_day ?? ''}
        onblur={async (event) => {
          const value = (event.currentTarget as HTMLInputElement).value
          if (value) await api.saveSettings({ rate_limit_posts_per_day: Number(value) })
          invalidate()
        }}
      />
      <p class="mt-1 text-[11px] text-ink-500">{t('settings.rateLimit.hint')}</p>
    </div>
    <div class="col-span-2">
      <label class="flex cursor-pointer items-center gap-2 text-xs text-ink-200">
        <input
          type="checkbox"
          class="accent-teal-500"
          checked={app.settings?.usage_ping_enabled ?? false}
          onchange={async (event) => {
            await api.saveSettings({
              usage_ping_enabled: (event.currentTarget as HTMLInputElement).checked,
            })
            invalidate()
          }}
        />
        {t('settings.usagePing.label')}
      </label>
      <p class="mt-1 text-[11px] leading-relaxed text-ink-500">
        {t('settings.usagePing.hint')}
      </p>
      <div class="mt-1.5 flex items-start gap-2.5">
        <img src={bearWelcome} alt="" width="36" height="36" class="shrink-0" />
        <p class="text-[11px] leading-relaxed text-teal-400/80">
          {t('settings.usagePing.why')}
        </p>
      </div>
    </div>
    <label class="col-span-2 flex cursor-pointer items-center gap-2 text-xs text-ink-200">
      <input
        type="checkbox"
        class="accent-teal-500"
        checked={app.settings?.require_title ?? false}
        onchange={async (event) => {
          await api.saveSettings({
            require_title: (event.currentTarget as HTMLInputElement).checked,
          })
          invalidate()
        }}
      />
      {t('settings.requireTitle')}
    </label>
    <div class="col-span-2">
      <label class="flex cursor-pointer items-center gap-2 text-xs text-ink-200">
        <input
          type="checkbox"
          class="accent-teal-500"
          checked={app.settings?.watch_posts_enabled ?? false}
          onchange={async (event) => {
            await api.saveSettings({
              watch_posts_enabled: (event.currentTarget as HTMLInputElement).checked,
            })
            invalidate()
          }}
        />
        {t('settings.behaviour.watchPosts')}
      </label>
      <p class="mt-1 text-[11px] leading-relaxed text-ink-500">
        {t('settings.behaviour.watchPostsHint', {
          delays: watchRetryDelays,
          ratingRefresh: app.settings?.watch_post_rating_refresh_minutes ?? '',
        })}
      </p>
    </div>
    <label class="col-span-2 flex cursor-pointer items-center gap-2 text-xs text-ink-200">
      <input
        type="checkbox"
        class="accent-teal-500"
        checked={app.settings?.debug_logging ?? false}
        onchange={async (event) => {
          await api.saveSettings({
            debug_logging: (event.currentTarget as HTMLInputElement).checked,
          })
          invalidate()
        }}
      />
      {t('settings.diagnostics.label')}
    </label>
  </div>
</Panel>
