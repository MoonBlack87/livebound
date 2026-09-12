<script lang="ts" module>
  import archive from '../../assets/icons/lb-archive.svg?raw'
  import board from '../../assets/icons/lb-board.svg?raw'
  import draft from '../../assets/icons/lb-draft.svg?raw'
  import duplicates from '../../assets/icons/lb-duplicates.svg?raw'
  import failed from '../../assets/icons/lb-failed.svg?raw'
  import info from '../../assets/icons/lb-info.svg?raw'
  import library from '../../assets/icons/lb-library.svg?raw'
  import models from '../../assets/icons/lb-models.svg?raw'
  import needsReconcile from '../../assets/icons/lb-needs-reconcile.svg?raw'
  import published from '../../assets/icons/lb-published.svg?raw'
  import pushing from '../../assets/icons/lb-pushing.svg?raw'
  import ready from '../../assets/icons/lb-ready.svg?raw'
  import remoteDraft from '../../assets/icons/lb-remote-draft.svg?raw'
  import remoteMissing from '../../assets/icons/lb-remote-missing.svg?raw'
  import review from '../../assets/icons/lb-review.svg?raw'
  import schedule from '../../assets/icons/lb-schedule.svg?raw'
  import settings from '../../assets/icons/lb-settings.svg?raw'
  import success from '../../assets/icons/lb-success.svg?raw'
  import trash from '../../assets/icons/lb-trash.svg?raw'
  import warning from '../../assets/icons/lb-warning.svg?raw'

  const ICONS = {
    archive,
    board,
    draft,
    duplicates,
    failed,
    info,
    library,
    models,
    needs_reconcile: needsReconcile,
    published,
    pushing,
    ready,
    remote_draft: remoteDraft,
    remote_missing: remoteMissing,
    review,
    schedule,
    settings,
    success,
    trash,
    warning,
  } as const

  export type IconName = keyof typeof ICONS
</script>

<script lang="ts">
  let { name, size }: { name: IconName; size: number } = $props()
</script>

<span class="icon" style:width={`${size}px`} style:height={`${size}px`} aria-hidden="true">
  {@html ICONS[name]}
</span>

<style>
  .icon {
    display: inline-block;
    flex: none;
  }

  .icon :global(svg) {
    display: block;
    height: 100%;
    width: 100%;
  }
</style>
