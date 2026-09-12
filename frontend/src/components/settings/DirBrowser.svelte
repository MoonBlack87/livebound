<script lang="ts">
  import { api } from '../../api/client'
  import { t } from '../../i18n/index.svelte'
  import { invalidate, toast } from '../../state/store.svelte'
  import Modal from '../ui/Modal.svelte'
  import Spinner from '../ui/Spinner.svelte'

  /**
   * Choosing a folder - by clicking or by typing.
   *
   * Clicking is right for browsing, but anyone who already knows half the path
   * types faster. So both: the line at the top is an input, and while typing the
   * matching subfolders are offered.
   */

  let {
    onClose,
    asArchive = false,
    title,
    onPick,
  }: {
    onClose: () => void
    asArchive?: boolean
    title?: string
    /** Given, the dialog just hands the path back instead of adding a source. */
    onPick?: (path: string) => void
  } = $props()

  interface Listing {
    path: string
    parent: string | null
    entries: { name: string; path: string }[]
  }

  let current = $state<Listing | null>(null)
  let typed = $state('')
  let editing = $state(false)
  let suggestions = $state<{ name: string; path: string }[]>([])
  let error = $state('')

  const listed = $derived(
    editing && typed.trim() !== current?.path ? suggestions : (current?.entries ?? []),
  )

  function load(path?: string) {
    error = ''
    api
      .browseDirs(path)
      .then((data) => {
        current = data
        typed = data.path
        editing = false
        suggestions = []
      })
      .catch((err) => (error = (err as Error).message))
  }

  $effect(() => {
    load()
  })

  // While typing: list the folder above the partial name and filter by its
  // prefix. A path ending in / means the folder itself.
  $effect(() => {
    if (!editing) return
    const value = typed.trim()
    if (!value.startsWith('/')) {
      suggestions = []
      return
    }
    const cut = value.lastIndexOf('/')
    const parent = cut <= 0 ? '/' : value.slice(0, cut)
    const partial = value.slice(cut + 1).toLowerCase()

    let cancelled = false
    const timer = setTimeout(() => {
      api
        .browseDirs(parent)
        .then((data) => {
          if (cancelled) return
          suggestions = data.entries
            .filter((entry) => entry.name.toLowerCase().startsWith(partial))
            .slice(0, 40)
        })
        .catch(() => !cancelled && (suggestions = []))
    }, 180)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  })

  async function accept() {
    if (onPick) {
      onPick(typed.trim())
      onClose()
      return
    }
    try {
      await api.addSource({ path: typed.trim(), is_archive: asArchive })
      invalidate()
      toast('success', asArchive ? t('dirs.archiveAdded') : t('dirs.added'))
      onClose()
    } catch (err) {
      error = (err as Error).message
    }
  }
</script>

<Modal title={title ?? (asArchive ? t('dirs.titleArchive') : t('dirs.title'))} {onClose}>
  <div class="mb-2">
    <div class="flex gap-2">
      <input
        class="input mono text-xs"
        value={typed}
        spellcheck="false"
        placeholder={t('dirs.placeholder')}
        oninput={(event) => {
          typed = (event.currentTarget as HTMLInputElement).value
          editing = true
          error = ''
        }}
        onkeydown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            load(typed.trim())
          } else if (event.key === 'Escape' && editing) {
            event.preventDefault()
            typed = current?.path ?? ''
            editing = false
          }
        }}
      />
      <button class="btn btn-sm" onclick={() => load(typed.trim())}>{t('dirs.open')}</button>
    </div>
    <p class="mt-1 text-[11px] text-ink-500">{t('dirs.hint')}</p>
    {#if error}
      <p class="mt-1 text-[11px]" style="color: var(--color-failed);">{error}</p>
    {/if}
  </div>

  {#if !current}
    <Spinner />
  {:else}
    {@const parent = current.parent}
    <div class="max-h-80 space-y-0.5 overflow-y-auto">
      {#if !editing && parent}
        <button
          class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-ink-800"
          onclick={() => load(parent)}
        >
          <span aria-hidden="true">↰</span> ..
        </button>
      {/if}
      {#each listed as entry (entry.path)}
        <button
          class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-ink-800"
          onclick={() => load(entry.path)}
        >
          <span aria-hidden="true" class="text-ink-500">▸</span>
          <span class="truncate">{entry.name}</span>
        </button>
      {/each}
      {#if !listed.length}
        <p class="py-4 text-center text-xs text-ink-500">
          {editing ? t('dirs.noMatch') : t('dirs.noSubfolders')}
        </p>
      {/if}
    </div>
  {/if}

  {#snippet footer()}
    <button class="btn" onclick={onClose}>{t('ui.cancel')}</button>
    <button class="btn btn-primary" disabled={!typed.trim()} onclick={accept}>
      {t('dirs.take')}
    </button>
  {/snippet}
</Modal>
