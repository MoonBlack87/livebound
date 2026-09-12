<script lang="ts">
  import { t } from '../i18n/index.svelte'
  import { siteBase } from '../lib/civitai'
  import { app } from '../state/store.svelte'

  const budget = $derived(app.account?.budget)

  function barColor(remaining: number): string {
    if (remaining <= 2) return 'var(--color-failed)'
    if (remaining <= 5) return 'var(--color-ready)'
    return 'var(--color-accent-500)'
  }
</script>

<div class="border-t border-ink-800 px-3 py-3">
  <!-- `CVT-20` is about having no credential at all, not about the pasted key:
       an account connected by OAuth must not be told to add one. -->
  {#if app.account && app.account.auth_mode !== 'none'}
    <div class="flex items-center gap-2">
      <span
        class="h-1.5 w-1.5 rounded-full"
        style="background: var(--color-published);"
        aria-hidden="true"
      ></span>
      {#if app.account.username}
        <a
          class="truncate text-xs text-ink-200 underline decoration-dotted underline-offset-2"
          href="{siteBase()}/user/{encodeURIComponent(app.account.username)}"
          target="_blank"
          rel="noreferrer noopener"
        >{app.account.username}</a>
      {:else}
        <span class="truncate text-xs text-ink-200">{t('account.connected')}</span>
      {/if}
    </div>
    {#if budget}
      <div class="mt-2">
        <div class="flex items-baseline justify-between text-[10.5px] text-ink-400">
          <span>{t('account.postsToday')}</span>
          <span class="mono">{budget.limit === null ? budget.used : `${budget.used} / ${budget.limit}`}</span>
        </div>
        {#if budget.limit !== null && budget.remaining !== undefined}
          <div class="mt-1 h-1 overflow-hidden rounded-full bg-ink-800">
            <div
              class="h-full rounded-full"
              style="width: {Math.min(100, (budget.used / Math.max(1, budget.limit)) * 100)}%;
                     background: {barColor(budget.remaining)};"
            ></div>
          </div>
        {/if}
      </div>
    {/if}
  {/if}
  <!-- Nothing here when no account is connected: the setup notice already says
       it, with a link straight to the right settings area. Two places saying
       the same thing, and this one still spoke of an API key that no longer
       exists. -->
</div>
