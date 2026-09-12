<script lang="ts">
  import { t } from '../../i18n/index.svelte'
  import { app } from '../../state/store.svelte'
  import Panel from '../ui/Panel.svelte'

  /**
   * What this application is, which version is running, and under what terms.
   *
   * Everything factual here is derived rather than typed: a version somebody has
   * to remember to change is wrong the first time they forget. The two prose
   * slots are the maintainer's own words and are marked as missing until he
   * writes them - a placeholder that reads like finished text is worse than an
   * empty section.
  */

  const version = $derived(app.settings?.app_version ?? '')
  const licence = $derived(app.settings?.app_licence ?? '')
  const rightsHolder = $derived(app.settings?.app_rights_holder ?? '')
</script>

<Panel title={t('settings.about.title')}>
  <div class="space-y-3 text-xs">
    <dl class="grid gap-x-4 gap-y-1.5 sm:grid-cols-[auto_1fr]">
      <dt class="text-ink-400">{t('settings.about.version')}</dt>
      <dd class="mono text-ink-100">{version || '—'}</dd>

      <dt class="text-ink-400">{t('settings.about.licence')}</dt>
      <dd>
        <a class="text-ink-100 underline" href="/LICENSE" target="_blank" rel="noopener noreferrer">
          {licence || '—'} ↗
        </a>
      </dd>

      <dt class="text-ink-400">{t('settings.about.rightsHolder')}</dt>
      <!-- The name stays derived from pyproject.toml; only the two profiles it
           refers to are linked. Same host as the account link elsewhere. -->
      <dd class="text-ink-100">
        {rightsHolder || '—'}
        <span class="ml-1 whitespace-nowrap">
          <a
            class="text-teal-400 hover:underline"
            href="https://civitai.com/user/Moonbear_AIArt"
            target="_blank"
            rel="noopener noreferrer">CivitAI ↗</a>
          <span class="text-ink-600">·</span>
          <a
            class="text-teal-400 hover:underline"
            href="https://github.com/MoonBlack87"
            target="_blank"
            rel="noopener noreferrer">GitHub ↗</a>
        </span>
      </dd>
    </dl>

    <section class="space-y-2">
      <p class="label">{t('settings.about.whatItIs')}</p>
      <p class="text-ink-300">{t('settings.about.summary')}</p>
      <p class="text-ink-300">{t('settings.about.summaryTwo')}</p>
    </section>

    <section class="space-y-2">
      <p class="label">{t('settings.about.terms')}</p>
      <p class="text-ink-300">{t('settings.about.termsSummary')}</p>
      <p class="text-ink-300">{t('settings.about.termsSummaryTwo')}</p>
      <!-- The one paragraph on this screen that must not be skimmed past. -->
      <div class="flex gap-2 rounded-lg border border-amber-600/50 bg-amber-950/30 px-3 py-2.5">
        <span aria-hidden="true" class="text-base leading-none">⚠️</span>
        <p class="text-amber-100/90">{t('settings.about.files')}</p>
      </div>
    </section>

    <section>
      <p class="label mb-1">{t('settings.about.documentation')}</p>
      <a
        class="text-teal-400 hover:underline"
        href="https://moonblack87.github.io/livebound/"
        target="_blank"
        rel="noopener noreferrer"
      >
        moonblack87.github.io/livebound ↗
      </a>
    </section>

    <section>
      <p class="label mb-1">{t('settings.about.publicRepository')}</p>
      <a
        class="text-teal-400 hover:underline"
        href="https://github.com/MoonBlack87/livebound"
        target="_blank"
        rel="noopener noreferrer"
      >
        github.com/MoonBlack87/livebound ↗
      </a>
    </section>

    <p class="text-xs text-ink-500">{t('settings.about.independence')}</p>
  </div>
</Panel>
