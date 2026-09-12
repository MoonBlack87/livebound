<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { setActiveJob, toast } from '../../state/store.svelte'

  /**
   * Backing the image archive up as a zip.
   *
   * The database backup protects the state, not the images — and the archive
   * increasingly holds pictures that exist nowhere else: everything generated
   * straight on CivitAI and fetched from there.
   */

  const SPLITS = [
    { value: 0, label: 'settings.archiveZip.single' },
    { value: 500, label: 'settings.archiveZip.split500' },
    { value: 1000, label: 'settings.archiveZip.split1000' },
    { value: 4000, label: 'settings.archiveZip.split4000' },
  ]

  let info = $state<Record<string, unknown> | null>(null)
  let splitMb = $state(0)

  const bytes = $derived((info?.bytes as number) ?? 0)

  $effect(() => {
    api.archiveBackupStatus().then((value) => (info = value)).catch(() => undefined)
  })

  async function makeZip() {
    try {
      setActiveJob(await api.backupArchive(splitMb || undefined))
    } catch (error) {
      toast('error', (error as Error).message)
    }
  }
</script>

{#if info?.configured}
  <div class="border-t border-ink-800 pt-3">
    <p class="label mb-1.5">{t('settings.archiveZip.title')}</p>
    <div class="flex flex-wrap items-center gap-2">
      <span class="text-xs text-ink-400">
        {t('settings.archiveZip.size', {
          files: (info.files as number) ?? 0,
          gb: (bytes / 1024 ** 3).toFixed(2),
        })}
      </span>
      <div class="flex-1"></div>
      <select
        class="input w-auto"
        value={splitMb}
        onchange={(event) => (splitMb = Number((event.currentTarget as HTMLSelectElement).value))}
      >
        {#each SPLITS as option (option.value)}
          <option value={option.value}>{t(option.label)}</option>
        {/each}
      </select>
      <button class="btn btn-sm" disabled={!info.files} onclick={makeZip}>
        {t('settings.archiveZip.create')}
      </button>
    </div>
    <p class="mt-1.5 text-[11px] leading-relaxed text-ink-500">{t('settings.archiveZip.hint')}</p>
  </div>
{/if}
