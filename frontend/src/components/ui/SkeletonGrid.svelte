<script lang="ts">
  let { count = 12, min = 170 }: { count?: number; min?: number } = $props()
</script>

<div class="grid gap-3" style="grid-template-columns: repeat(auto-fill, minmax({min}px, 1fr));">
  {#each Array.from({ length: count }) as _, index (index)}
    <div class="skeleton aspect-[3/4]"></div>
  {/each}
</div>
