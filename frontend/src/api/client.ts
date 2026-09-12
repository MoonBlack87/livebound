import type {
  AccountInfo,
  AdoptResult,
  ArchiveListResponse,
  ArchivePlanEntry,
  ArchiveStatus,
  BackupInfo,
  CalendarView,
  DuplicateReport,
  DiscoveryResponse,
  ImageDetail,
  ImageEditState,
  Issue,
  ImageListResponse,
  ImageSearchParams,
  Job,
  JobUpdates,
  LlmProfile,
  LlmProfileInput,
  LlmStatus,
  ManualMatchCandidates,
  MetadataFieldInventory,
  ModelSearchResult,
  ModelSuggestion,
  ModelFileDuplicateReport,
  ModelFileReport,
  ModelRoot,
  OauthStatus,
  OffsetParse,
  Post,
  PostHolder,
  PushPreview,
  PushRun,
  Settings,
  SpreadPlanEntry,
  TrashedImage,
  BulkEditPlanEntry,
  SourceDeletionImpact,
  SourceRoot,
  SetupStatus,
  Suggestion,
  SuggestMaterial,
} from '../types/api'
import { t } from '../i18n/index.svelte'

export class ApiError extends Error {
  status: number
  /** Stable identifier for the errors that recur; empty for one-off diagnostics. */
  code: string

  constructor(status: number, message: string, code = '') {
    super(message)
    this.status = status
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (!(init?.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(path, {
    ...init,
    headers,
  })
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    let code = ''
    let params: Record<string, string | number> = {}
    try {
      const body = await response.json()
      // Three shapes reach here: our own {code, message, params}, FastAPI's plain
      // string, and its validation list.
      const detail = body.detail
      if (detail && typeof detail === 'object' && !Array.isArray(detail) && detail.message) {
        message = detail.message
        code = detail.code ?? ''
        params = detail.params ?? {}
        if (code === 'request_validation_error' && Array.isArray(detail.errors)) {
          const requestLabel = `${String(params.method ?? '')} ${String(params.endpoint ?? '')}`.trim()
          params = {
            ...params,
            errors: detail.errors
              .map((issue: any) =>
                t('error.request_validation_item', {
                  request: requestLabel,
                  field: String(issue.location ?? ''),
                  reason: String(issue.message ?? ''),
                }),
              )
              .join('\n'),
          }
        }
      } else if (typeof detail === 'string') message = detail
      else if (Array.isArray(detail)) message = detail.map((d: any) => d.msg).join(', ')
      else if (body.error) message = body.error
    } catch {
      /* keep the status line */
    }
    throw new ApiError(response.status, translateError(code, message, params), code)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

/**
 * Translate a coded error, falling back to the English sentence the backend
 * sent. The backend speaks English only; this is where a German UI gets its
 * German error message back.
 */
function translateError(
  code: string,
  message: string,
  params: Record<string, string | number>,
): string {
  return translateCoded('error', code, message, params)
}

/**
 * Translate a checklist issue. The backend gives every issue a stable code and
 * the values its sentence names; this renders it in the user's language and
 * falls back to the backend's English when the catalogue has nothing.
 */
export function translateIssue(issue: Issue): string {
  return translateCoded('issue', issue.code, issue.message, issue.params ?? {})
}

/**
 * Translate a coded backend refusal that did not arrive as a thrown error -
 * a per-item failure inside a bulk answer, for instance.
 */
export function translateCode(
  code: string,
  message: string,
  params: Record<string, string | number> = {},
): string {
  return translateCoded('error', code, message, params)
}

function translateCoded(
  prefix: string,
  code: string,
  message: string,
  params: Record<string, string | number>,
): string {
  if (!code) return message
  const key = `${prefix}.${code}`
  const text = t(key, params)
  // An unresolved placeholder means the catalogue expects a value the backend
  // did not send. The English sentence always carries its own values, so it is
  // the better answer than a message with a `{hole}` in it.
  if (text === key || /\{\w+\}/.test(text)) return message
  return text
}

const get = <T,>(
  path: string,
  params?: object,
  signal?: AbortSignal,
) => {
  const query = params
    ? '?' +
      new URLSearchParams(
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && v !== '')
          .map(([k, v]) => [k, String(v)]),
      ).toString()
    : ''
  return request<T>(path + query, { signal })
}
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
const put = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: 'PUT', body: JSON.stringify(body) })
const patch = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
const del = <T,>(path: string, params?: Record<string, unknown>) => {
  const query = params ? '?' + new URLSearchParams(params as Record<string, string>) : ''
  return request<T>(path + query, { method: 'DELETE' })
}

export const MIN_IMAGE_SEARCH_LENGTH = 3

export const api = {

  // first start
  setupStatus: () => get<SetupStatus>('/api/setup'),
  completeSetup: (path: string, backup?: File) => {
    const body = new FormData()
    body.append('path', path)
    if (backup) body.append('backup', backup)
    return request<{
      setup_required: boolean
      data_directory_ready: boolean
      data_dir: string
      restored: boolean
    }>('/api/setup', { method: 'POST', body })
  },
  finishSetup: () => post<{ setup_required: boolean }>('/api/setup/finish'),

  // settings & account
  settings: () => get<Settings>('/api/settings'),
  saveSettings: (body: Record<string, unknown>) => put<Settings>('/api/settings', body),
  metadataFields: (scope?: { postId?: number; imageIds?: number[] }) =>
    get<{ items: MetadataFieldInventory[] }>('/api/settings/metadata-fields', {
      post_id: scope?.postId,
      image_ids: scope?.imageIds?.length ? scope.imageIds.join(',') : undefined,
    }),
  oauthStatus: () => get<OauthStatus>('/api/oauth/status'),
  oauthStart: () => post<{ state: string; authorize_url: string }>('/api/oauth/start'),
  oauthDisconnect: () => post<{ ok: boolean }>('/api/oauth/disconnect'),
  account: () => get<AccountInfo>('/api/account'),
  dataDir: () => get<Record<string, unknown>>('/api/settings/data-dir'),
  moveDataDir: (path: string) => post<Job>('/api/settings/data-dir', { path }),
  browseDirs: (path?: string) =>
    get<{ path: string; parent: string | null; entries: { name: string; path: string }[] }>(
      '/api/browse-dirs',
      { path },
    ),

  // sources
  sources: () => get<{ items: SourceRoot[] }>('/api/sources'),
  addSource: (body: Record<string, unknown>) => post<SourceRoot>('/api/sources', body),
  sourceDeletionImpact: (id: number) =>
    get<SourceDeletionImpact>(`/api/sources/${id}/delete-impact`),
  deleteSource: (id: number) => del<{ ok: boolean }>(`/api/sources/${id}`),
  scan: (sourceId?: number) => post<Job>('/api/sources/scan' + (sourceId ? `?source_id=${sourceId}` : '')),
  folders: (sourceId?: number) =>
    get<{ items: { folder: string; count: number }[] }>('/api/folders', { source_id: sourceId }),

  // local model folders
  modelRoots: () =>
    get<{
      items: ModelRoot[]
      recognized: number
      unrecognized: number
      unqueried: number
    }>('/api/model-roots'),
  addModelRoot: (body: Record<string, unknown>) => post<ModelRoot>('/api/model-roots', body),
  deleteModelRoot: (id: number) => del<{ ok: boolean }>(`/api/model-roots/${id}`),
  hashModelRoots: (rootId?: number) =>
    post<Job>('/api/model-roots/hash' + (rootId ? `?root_id=${rootId}` : '')),
  modelFiles: (params: Record<string, unknown>) =>
    get<ModelFileReport>('/api/model-files', params),
  modelFileDuplicates: (params: Record<string, unknown>) =>
    get<ModelFileDuplicateReport>('/api/model-file-duplicates', params),
  assignModelFile: (id: number, url: string) =>
    post<Record<string, unknown>>(`/api/model-files/${id}/assignment`, { url }),
  resolveModelFile: (id: number) =>
    post<{ resolved: number; unknown: number; cached: number; failed: number; reapplied: number }>(
      `/api/model-files/${id}/resolve`,
    ),

  // images
  images: (params: ImageSearchParams, signal?: AbortSignal) =>
    get<ImageListResponse>('/api/images', params, signal),
  image: (id: number) => get<ImageDetail>(`/api/images/${id}`),
  imageEdit: (id: number) => get<ImageEditState>(`/api/images/${id}/edit`),
  saveImageEdit: (id: number, body: Record<string, unknown>) =>
    put<ImageEditState>(`/api/images/${id}/edit`, body),
  bulkImageEdit: (body: Record<string, unknown>) =>
    post<{
      applied: boolean
      changed: number
      plan: BulkEditPlanEntry[]
      unmatched_delete_fields: string[]
    }>('/api/images/edit/bulk', body),
  duplicates: () => get<DuplicateReport>('/api/duplicates'),
  scanDuplicates: () => post<Job>('/api/duplicates/scan'),
  dismissDuplicates: (imageIds: number[]) =>
    post<{ dismissed: number }>('/api/duplicates/dismiss', { image_ids: imageIds }),
  imageDeletionPlan: (imageIds: number[], withSidecars = true) =>
    post<{ paths: string[] }>('/api/images/delete-plan', {
      image_ids: imageIds,
      with_sidecars: withSidecars,
    }),
  deleteImages: (imageIds: number[], withSidecars = true) =>
    post<{ deleted: number; sidecars: number; failed: { path: string; reason: string }[] }>(
      '/api/images/delete',
      { image_ids: imageIds, with_sidecars: withSidecars },
    ),
  postHolders: (imageIds: number[]) =>
    post<{ items: { image_id: number; posts: PostHolder[] }[] }>('/api/images/post-holders', {
      image_ids: imageIds,
    }),
  trashPlan: (imageIds: number[]) =>
    post<{
      post_bound_image_ids: number[]
      items: { id: number; absolute_path: string; post_bound: boolean }[]
    }>('/api/images/trash-plan', { image_ids: imageIds }),
  trash: () => get<{ items: TrashedImage[] }>('/api/trash'),
  trashImages: (imageIds: number[], withSidecars = true) =>
    post<Job>('/api/images/trash', {
      image_ids: imageIds,
      with_sidecars: withSidecars,
    }),
  restoreImages: (imageIds: number[]) =>
    post<{ restored: number; sidecars: number; failed: { path: string; reason: string }[] }>(
      '/api/images/restore',
      { image_ids: imageIds },
    ),
  thumbnailUrl: (id: number, large = false, cacheKey?: string | null) => {
    const params = new URLSearchParams()
    if (large) params.set('size', 'large')
    if (cacheKey) params.set('v', cacheKey)
    const query = params.toString()
    return `/api/images/${id}/thumbnail` + (query ? `?${query}` : '')
  },
  previewUrl: (id: number) => `/api/images/${id}/preview`,

  // posts
  posts: (state?: string) => get<{ items: Post[] }>('/api/posts', { state }),
  post: (id: number) => get<Post>(`/api/posts/${id}`),
  archivedPosts: (limit = 50, offset = 0, fromDate?: string, toDate?: string) =>
    get<ArchiveListResponse>('/api/posts/archive', {
      limit,
      offset,
      from_date: fromDate,
      to_date: toDate,
    }),
  archivePublished: (days: number) =>
    post<{ archived: number; post_ids: number[]; cutoff: string }>(
      `/api/posts/archive-published?days=${days}`,
    ),
  renameArchiveFolder: (id: number) =>
    post<{ renamed: boolean; from: string; to: string; images?: number; usage_rows?: number }>(
      `/api/posts/${id}/archive-folder/rename`,
    ),
  createPost: (body: Record<string, unknown>) => post<Post>('/api/posts', body),
  patchPost: (id: number, body: Record<string, unknown>) => patch<Post>(`/api/posts/${id}`, body),
  deletePost: (id: number, remote = false) =>
    del<{ ok: boolean }>(`/api/posts/${id}`, { remote: String(remote) }),
  setPostImages: (id: number, imageIds: number[]) =>
    put<Post>(`/api/posts/${id}/images`, { image_ids: imageIds }),
  setPostImageOrder: (id: number, postImageIds: number[]) =>
    put<Post>(`/api/posts/${id}/image-order`, { post_image_ids: postImageIds }),
  setPostTags: (id: number, tags: string[]) => put<Post>(`/api/posts/${id}/tags`, { tags }),
  ackDuplicate: (postId: number, postImageId: number, value = true) =>
    post<{ ok: boolean }>(`/api/posts/${postId}/images/${postImageId}/dedup-ack?value=${value}`),
  hideMeta: (postId: number, postImageId: number, value: boolean) =>
    post<Record<string, unknown>>(
      `/api/posts/${postId}/images/${postImageId}/hide-meta?value=${value}`,
    ),
  markReady: (id: number) => post<Post>(`/api/posts/${id}/ready`),
  markDraft: (id: number) => post<Post>(`/api/posts/${id}/unready`),
  archivePost: (id: number) => post<Post>(`/api/posts/${id}/archive`),
  jobUpdates: (after?: number) => get<JobUpdates>('/api/jobs/active', { after }),
  setSourceWatch: (id: number, enabled: boolean) =>
    post<{ ok: boolean }>(`/api/sources/${id}/watch`, { enabled }),
  setModelRootWatch: (id: number, enabled: boolean) =>
    post<{ ok: boolean }>(`/api/model-roots/${id}/watch`, { enabled }),
  archiveSelectedPosts: (postIds: number[]) =>
    post<{
      archived: number
      post_ids: number[]
      refused: { post_id: number; code: string; message: string; params: Record<string, string | number> }[]
    }>('/api/posts/archive-selected', { post_ids: postIds }),
  reschedule: (id: number, body: { scheduled_at?: string | null; offset_minutes?: number | null }) =>
    post<Post>(`/api/posts/${id}/reschedule`, body),
  pushText: (id: number) => post<Post>(`/api/posts/${id}/push-text`),
  remoteDelete: (id: number) => post<Post>(`/api/posts/${id}/remote-delete`),
  rebuild: (id: number) => post<Post>(`/api/posts/${id}/rebuild`),
  adoptSnapshot: (id: number) => post<Post>(`/api/posts/${id}/adopt-remote-snapshot`),
  reconcileCandidates: (id: number) =>
    get<{
      candidates: Record<string, unknown>[]
      reason: string
      manual_url?: string
      reconcile_tag?: string | null
    }>(`/api/posts/${id}/reconcile`),
  reconcileAdopt: (id: number, remotePostId: number) =>
    post<Post>(`/api/posts/${id}/reconcile`, { remote_post_id: remotePostId }),
  syncPost: (id: number) => post<Post>(`/api/posts/${id}/sync`),
  fetchImages: (id: number) => post<Job>(`/api/posts/${id}/fetch-images`),
  matchLocalAll: () => post<Job>('/api/posts/match-local'),
  matchLocal: (id: number, download = true) =>
    post<Post>(`/api/posts/${id}/match-local?download=${download}`),
  matchCandidates: (postId: number, postImageId: number) =>
    get<ManualMatchCandidates>(`/api/posts/${postId}/images/${postImageId}/match-candidates`),
  matchLocalManually: (postId: number, postImageId: number, imageId: number) =>
    post<Post>(`/api/posts/${postId}/images/${postImageId}/match-local/${imageId}`),

  // schedule
  calendar: (start?: string, end?: string) =>
    get<CalendarView>('/api/schedule/calendar', { start, end }),
  spread: (body: Record<string, unknown>) =>
    post<{ applied: boolean; relative: boolean; plan: SpreadPlanEntry[] }>(
      '/api/schedule/spread',
      body,
    ),
  parseOffset: (text: string) => get<OffsetParse>('/api/schedule/parse-offset', { text }),

  // push & sync
  pushPreview: (postIds: number[]) =>
    post<{ items: PushPreview[] }>('/api/push/preview', { post_ids: postIds }),
  push: (postIds: number[]) => post<Job>('/api/push', { post_ids: postIds }),
  pushRuns: (hours?: number) => get<{ items: PushRun[] }>('/api/push/runs', { hours }),
  pushRun: (id: number) => get<PushRun>(`/api/push/runs/${id}`),
  resumeRun: (id: number) => post<Job>(`/api/push/runs/${id}/resume`),
  sync: () => post<Job>('/api/sync'),
  discover: (count: number, days?: number, cursor?: string | number) =>
    get<DiscoveryResponse>('/api/sync/discover', { count, days, cursor }),
  importRemote: (remotePostId: number) =>
    post<AdoptResult>('/api/sync/import', { remote_post_id: remotePostId }),
  importRemoteBatch: (remotePostIds: number[]) =>
    post<Job>('/api/sync/import/batch', { remote_post_ids: remotePostIds }),

  backups: () => get<{ items: BackupInfo[]; folder: string }>('/api/backups'),
  createBackup: (reason = 'manual') => post<BackupInfo>(`/api/backups?reason=${encodeURIComponent(reason)}`),
  restoreBackup: (name: string) =>
    post<{ restored: string; safety_copy: string; token_required: boolean }>(
      `/api/backups/${encodeURIComponent(name)}/restore`,
    ),
  deleteBackup: (name: string) => del<{ ok: boolean }>(`/api/backups/${encodeURIComponent(name)}`),

  archiveBackupStatus: () =>
    get<{ configured: boolean; exists?: boolean; path?: string; files?: number; bytes?: number }>(
      '/api/backups/archive/status',
    ),
  backupArchive: (splitMb?: number) =>
    post<Job>('/api/backups/archive' + (splitMb ? `?split_mb=${splitMb}` : '')),

  archiveStatus: () => get<ArchiveStatus>('/api/archive/status'),
  archivePlan: (limit?: number) =>
    get<{ items: ArchivePlanEntry[] }>('/api/archive/plan', { limit }),
  runArchive: (imageIds: number[] = []) => post<Job>('/api/archive/run', { image_ids: imageIds }),

  // resources
  suggestTags: (q: string) => get<{ items: string[] }>('/api/tags/suggest', { q }),
  searchModels: (q: string, types?: string) =>
    get<{ items: ModelSearchResult[] }>('/api/models/search', { q, types }),
  modelSuggestions: (q: string) =>
    get<{ items: ModelSuggestion[] }>('/api/model-suggestions', { q }),
  addImageResource: (imageId: number, body: Record<string, unknown>) =>
    post<Record<string, unknown>>(`/api/images/${imageId}/resources`, body),
  patchImageResource: (resourceId: number, body: Record<string, unknown>) =>
    patch<Record<string, unknown>>(`/api/resources/${resourceId}`, body),
  attachImageResource: (
    resourceId: number,
    model: Record<string, unknown>,
    version: Record<string, unknown>,
  ) =>
    post<{ resource: Record<string, unknown>; reapplied: number; hash: string | null }>(
      `/api/resources/${resourceId}/attach`,
      { model, version },
    ),
  imageResourceHashScope: (resourceId: number) =>
    get<{ hash: string | null; images: number }>(`/api/resources/${resourceId}/hash-scope`),
  unlockImageResource: (resourceId: number) =>
    post<Record<string, unknown>>(`/api/resources/${resourceId}/unlock`),
  deleteImageResource: (resourceId: number) =>
    del<Record<string, unknown>>(`/api/resources/${resourceId}`),
  restoreImageResource: (resourceId: number) =>
    post<Record<string, unknown>>(`/api/resources/${resourceId}/restore`),

  // llm
  llmStatus: () => get<LlmStatus>('/api/llm/status'),
  llmEndpointModels: () =>
    get<{ items: string[]; url: string; reachable: boolean }>('/api/llm/endpoint/models'),
  llmProfiles: () => get<{ items: LlmProfile[]; default_id: string | null }>('/api/llm/profiles'),
  createLlmProfile: (body: LlmProfileInput) => post<LlmProfile>('/api/llm/profiles', body),
  updateLlmProfile: (id: number, body: LlmProfileInput) =>
    patch<LlmProfile>(`/api/llm/profiles/${id}`, body),
  llmProfileSeed: (id: number) =>
    get<{ system_prompt: string }>(`/api/llm/profiles/${id}/seed`),
  restoreLlmProfiles: () => post<{ restored: string[] }>('/api/llm/profiles/restore', {}),
  deleteLlmProfile: (id: number) => del<{ ok: boolean }>(`/api/llm/profiles/${id}`),
  setDefaultLlmProfile: (id: number) =>
    post<{ ok: boolean }>(`/api/llm/profiles/${id}/default`),
  llmSuggest: (
    postIds: number[], profileId?: number, hint?: string, seed?: number,
    material?: SuggestMaterial,
  ) =>
    post<Job>('/api/llm/suggest', {
      post_ids: postIds,
      profile_id: profileId,
      hint: hint || null,
      seed: seed ?? null,
      material: material ?? 'auto',
    }),
  llmShorten: (imageIds: number[]) => post<Job>('/api/llm/shorten', { image_ids: imageIds }),
  llmSuggestions: (postId: number) =>
    get<{ items: Suggestion[] }>(`/api/llm/suggestions/${postId}`),
  llmAccept: (suggestionId: number) =>
    post<Record<string, unknown>>(`/api/llm/suggestions/${suggestionId}/accept`),
  llmDismiss: (suggestionId: number) =>
    del<Record<string, unknown>>(`/api/llm/suggestions/${suggestionId}`),
  llmStop: () => post<{ ok: boolean }>('/api/llm/stop'),

  // jobs
  job: (id: number) => get<Job>(`/api/jobs/${id}`),
  cancelJob: (id: number) => post<{ ok: boolean }>(`/api/jobs/${id}/cancel`),
}

/**
 * Poll a job until it finishes. Used by every long-running action; there are no
 * websockets in this app on purpose.
 */
export async function waitForJob(
  jobId: number,
  onTick?: (job: Job) => void,
  intervalMs = 700,
): Promise<Job> {
  for (;;) {
    const job = await api.job(jobId)
    onTick?.(job)
    if (job.status !== 'running' && job.status !== 'starting') return job
    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }
}
