import { app } from '../state/store.svelte'

export function siteBase(): string {
  return app.settings?.site_base ?? 'https://civitai.red'
}

export function modelUrl(modelId: number | null | undefined, versionId: number | null | undefined): string | null {
  if (!modelId || !versionId) return null
  return `${siteBase()}/models/${modelId}?modelVersionId=${versionId}`
}
