import { vitePreprocess } from '@sveltejs/vite-plugin-svelte'

export default {
  // Lets components use <script lang="ts">; Vite does the transpiling.
  preprocess: vitePreprocess(),
}
