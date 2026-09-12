/**
 * The one piece of shared state.
 *
 * Flat on purpose. `revision` is the broad cache-invalidation mechanism for
 * changes that can affect several domains. Post-owned changes use
 * `postRevision`, so they do not imply that the image library or app shell
 * changed.
 */

import type { AccountInfo, Job, Settings } from '../types/api'

export type View =
  | 'library'
  | 'duplicates'
  | 'trash'
  | 'models'
  | 'board'
  | 'calendar'
  | 'review'
  | 'archive'
  | 'settings'

export interface Toast {
  id: number
  kind: 'success' | 'error' | 'info'
  message: string
}

export const app = $state({
  view: 'library' as View,

  /** The post open in the editor overlay, if any. */
  editingPostId: null as number | null,
  /** The image open in the metadata drawer, if any. */
  inspectingImageId: null as number | null,

  /** Selection in the library, kept across filter changes. */
  selectedImages: [] as number[],
  /** Selection on the board. Separate from the library's - they never mix. */
  selectedPosts: [] as number[],

  settings: null as Settings | null,
  account: null as AccountInfo | null,

  /** Bumped whenever something changed; every list re-reads on a new value. */
  revision: 0,
  /** Bumped when only post data changed. */
  postRevision: 0,

  activeJob: null as Job | null,
  toasts: [] as Toast[],
})

export function setView(view: View): void {
  app.view = view
}

export function openPost(id: number | null): void {
  app.editingPostId = id
}

export function inspectImage(id: number | null): void {
  app.inspectingImageId = id
}

export function toggleImage(id: number, range?: number[]): void {
  if (range && range.length) {
    // Shift-click adds the whole span rather than replacing the selection, so
    // several ranges can be collected for one post.
    app.selectedImages = [...new Set([...app.selectedImages, ...range])]
    return
  }
  app.selectedImages = app.selectedImages.includes(id)
    ? app.selectedImages.filter((x) => x !== id)
    : [...app.selectedImages, id]
}

export function setSelectedImages(ids: number[]): void {
  app.selectedImages = ids
}

export function clearSelection(): void {
  app.selectedImages = []
}

export function togglePost(id: number): void {
  app.selectedPosts = app.selectedPosts.includes(id)
    ? app.selectedPosts.filter((x) => x !== id)
    : [...app.selectedPosts, id]
}

export function setSelectedPosts(ids: number[]): void {
  app.selectedPosts = ids
}

export function clearPostSelection(): void {
  app.selectedPosts = []
}

export function setSettings(settings: Settings | null): void {
  app.settings = settings
}

export function setAccount(account: AccountInfo | null): void {
  app.account = account
}

export function invalidate(): void {
  app.revision += 1
}

export function invalidatePosts(): void {
  app.postRevision += 1
}

export function setActiveJob(job: Job | null): void {
  app.activeJob = job
}

let toastId = 0

export function toast(kind: Toast['kind'], message: string): void {
  const id = ++toastId
  app.toasts = [...app.toasts, { id, kind, message }]
  // An error gets longer than the rest - long enough to read a sentence twice -
  // but it goes too. A message still waiting for attention a minute later has
  // stopped being one, and it covers the next thing the user does.
  setTimeout(() => dismissToast(id), kind === 'error' ? 12000 : 4200)
}

export function dismissToast(id: number): void {
  app.toasts = app.toasts.filter((entry) => entry.id !== id)
}
