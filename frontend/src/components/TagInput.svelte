<script lang="ts">
  import { api } from '../api/client'
  import { t } from '../i18n/index.svelte'

  let {
    tags,
    onChange,
    onBeginEdit,
    disabled = false,
  }: {
    tags: string[]
    onChange: (tags: string[]) => void
    // Called the moment an edit starts, so the owner can capture the record it
    // belongs to. The blur below fires 140 ms late, by which time the post this
    // input was showing may already be closed.
    onBeginEdit?: () => void
    disabled?: boolean
  } = $props()

  let draft = $state('')
  let suggestions = $state<string[]>([])
  let highlight = $state(0)
  let blurTimer: number | null = null

  // Suggesting tags CivitAI already knows is not convenience: an invented
  // spelling is a tag nobody browses.
  $effect(() => {
    const query = draft.trim()
    if (query.length < 2) {
      suggestions = []
      return
    }
    let cancelled = false
    const timer = setTimeout(() => {
      api
        .suggestTags(query)
        .then((data) => {
          if (cancelled) return
          suggestions = data.items.filter((name) => !tags.includes(name.toLowerCase()))
          highlight = 0
        })
        .catch(() => (suggestions = []))
    }, 260)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  })

  function add(raw: string) {
    // Accept a pasted comma-separated list as well as one tag at a time.
    const parts = raw
      .split(',')
      .map((part) => part.trim().toLowerCase())
      .filter(Boolean)
    if (!parts.length) return
    const merged = [...tags]
    for (const part of parts) if (!merged.includes(part)) merged.push(part)
    onChange(merged)
    draft = ''
    suggestions = []
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === 'ArrowDown' && suggestions.length) {
      event.preventDefault()
      highlight = (highlight + 1) % suggestions.length
    } else if (event.key === 'ArrowUp' && suggestions.length) {
      event.preventDefault()
      highlight = (highlight - 1 + suggestions.length) % suggestions.length
    } else if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault()
      add(suggestions.length ? suggestions[highlight] : draft)
    } else if (event.key === 'Escape') {
      suggestions = []
    } else if (event.key === 'Backspace' && !draft && tags.length) {
      onChange(tags.slice(0, -1))
    }
  }
</script>

<div class="relative">
  <div class="rounded-lg border border-ink-700 bg-ink-950 p-1.5">
    <div class="flex flex-wrap gap-1.5">
      {#each tags as tag (tag)}
        <span class="chip">
          {tag}
          {#if !disabled}
            <button
              type="button"
              class="text-ink-500 hover:text-ink-100"
              onclick={() => {
                onBeginEdit?.()
                onChange(tags.filter((item) => item !== tag))
              }}
              aria-label={t('tags.remove', { tag })}
            >
              ✕
            </button>
          {/if}
        </span>
      {/each}
      <input
        class="min-w-32 flex-1 bg-transparent px-1.5 py-0.5 text-xs text-ink-100 outline-none placeholder:text-ink-500"
        placeholder={tags.length ? t('tags.addMore') : t('tags.placeholder')}
        bind:value={draft}
        {disabled}
        onkeydown={onKeyDown}
        onblur={() => {
          // Let a click on a suggestion land before the list disappears.
          blurTimer = window.setTimeout(() => {
            add(draft)
            suggestions = []
          }, 140)
        }}
        onfocus={() => {
          if (blurTimer) window.clearTimeout(blurTimer)
          onBeginEdit?.()
        }}
      />
    </div>
  </div>

  {#if suggestions.length}
    <ul class="panel-raised absolute z-20 mt-1 max-h-48 w-full overflow-y-auto py-1">
      {#each suggestions as name, index (name)}
        <li>
          <button
            type="button"
            class="block w-full px-3 py-1.5 text-left text-xs {index === highlight
              ? 'bg-ink-750 text-ink-100'
              : 'text-ink-300'}"
            onmouseenter={() => (highlight = index)}
            onmousedown={(event) => {
              event.preventDefault()
              add(name)
            }}
          >
            {name}
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</div>
