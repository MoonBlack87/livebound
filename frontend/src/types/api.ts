// Hand-written mirrors of the backend payloads. Kept explicit rather than
// generated: the shapes are small and the compiler catching a rename at the
// boundary is worth more than the generator.

export type PostState =
  | 'draft'
  | 'ready'
  | 'pushing'
  | 'needs_reconcile'
  | 'remote_draft'
  | 'scheduled'
  | 'published'
  | 'failed'
  | 'remote_missing'
  | 'archived'

export type IssueLevel = 'error' | 'warning' | 'info'

export interface Issue {
  level: IssueLevel
  code: string
  message: string
  field: string | null
  /** The values the message names, so a translation can keep them. */
  params?: Record<string, string | number>
}

export interface Checklist {
  issues: Issue[]
  error_count: number
  warning_count: number
  can_push: boolean
  blocked: boolean
}

export interface QuickFacts {
  model?: string
  sampler?: string
  steps?: number | string
  cfg?: number | string
  seed?: number | string
  has_metadata: boolean
}

export type UsageState = 'all' | 'unused' | 'planned' | 'used'

export interface ImageSearchParams {
  source_id?: number
  folder?: string
  q?: string
  usage_state: UsageState
  include_missing?: boolean
  limit?: number
  offset?: number
}

export interface LibraryDuplicateMember {
  id: number
  source_root_id: number
  absolute_path: string
  relative_path: string
  folder: string
  width: number | null
  height: number | null
  file_size: number | null
  thumbnail_path: string | null
  is_missing: boolean
}

export interface LibraryDuplicateGroup {
  confidence: 'certain' | 'probable'
  members: LibraryDuplicateMember[]
}

export interface ImageRow {
  id: number
  source_root_id: number
  absolute_path: string
  relative_path: string
  folder: string
  width: number | null
  height: number | null
  file_size: number | null
  sha256: string | null
  pixel_sha256: string | null
  phash: string | null
  thumbnail_path: string | null
  is_missing: boolean
  duplicate_count: number
  duplicate_confidence: 'certain' | 'probable' | null
  duplicate_members: LibraryDuplicateMember[]
  used: boolean
  /** Already placed in a local post that has not been pushed. */
  planned: boolean
  similar: boolean
  edited: boolean
  quick: QuickFacts
}

export interface ImageListResponse {
  items: ImageRow[]
  total: number
  limit: number
  offset: number
  usage_state: UsageState
}

export type ManualMatchReasonCode =
  | 'identical_bytes'
  | 'identical_pixels'
  | 'same_seed_prompt'
  | 'same_prompt'
  | 'same_seed'
  | 'close_dhash'
  | 'remote_filename'
  | 'same_dimensions'

export interface ManualMatchReason {
  code: ManualMatchReasonCode
  distance?: number
}

export interface ManualMatchCandidate {
  id: number
  relative_path: string
  width: number | null
  height: number | null
  file_size: number | null
  thumbnail_path: string | null
  used: boolean
  reasons: ManualMatchReason[]
}

export interface ManualMatchCandidates {
  items: ManualMatchCandidate[]
  remote_facts: { seed: boolean; prompt: boolean; dhash: boolean }
}

export interface ParsedMetadata {
  prompt: string
  negative_prompt: string | null
  fields: Record<string, unknown>
  field_order: string[]
  warnings: string[]
}

export interface StoredImageEdit {
  image_id?: number
  draft: {
    prompt?: string | null
    negative_prompt?: string | null
    fields: Record<string, unknown>
  }
  touched: string[]
  deleted: string[]
  updated_at?: string
}

export interface ImageFieldDiff {
  key: string
  original: unknown
  value: unknown
  changed: boolean
  deleted: boolean
  derived: boolean
}

export interface ImageResource {
  id: number
  image_id: number
  resource_type: string
  name_in_prompt: string
  hash: string | null
  weight: number | null
  model_id: number | null
  model_version_id: number | null
  model_name: string | null
  version_name: string | null
  resolved_from: string | null
  locked_by_user: number
  added_by_user: number
  deleted_by_user: number
}

export interface MetadataWarning {
  /** Picks the sentence out of the catalogue; `message` is the English fallback. */
  code: string
  severity: 'warning' | 'error' | 'info'
  message: string
  params: Record<string, string | number>
  field: string
}

export interface ImageEditState {
  image_id: number
  edit: StoredImageEdit
  original: ParsedMetadata
  effective: ParsedMetadata
  diff: {
    prompts: Record<
      'prompt' | 'negative_prompt',
      { original: string | null; value: string | null; changed: boolean; deleted: boolean }
    >
    fields: ImageFieldDiff[]
    changed: boolean
  }
  effective_infotext: string
  comfyui_workflow_replaced: boolean
  warnings: MetadataWarning[]
  resources: ImageResource[]
  original_resources: Omit<
    ImageResource,
    'id' | 'image_id' | 'locked_by_user' | 'added_by_user' | 'deleted_by_user'
  >[]
}

export interface BulkEditChange {
  field: string
  before: unknown
  after: unknown
}

export interface BulkEditPlanEntry {
  image_id: number
  filename: string
  changes: BulkEditChange[]
}

export interface UsageRow {
  id: number
  sha256: string
  post_id: number | null
  remote_post_id: number | null
  post_title: string | null
  used_at: string
  status: string
  distance?: number
}

export interface Duplicates {
  exact: UsageRow[]
  similar: UsageRow[]
  has_exact: boolean
  has_similar: boolean
}

export interface ResourceCard {
  type: string
  name: string
  version: string | null
  prompt_name: string
  weight: number | null
  hash: string | null
  model_id: number | null
  model_version_id: number | null
  url: string | null
  resolved: boolean
  thumbnail_url: string | null
}

export interface MetadataView {
  prompt: string
  negative_prompt: string | null
  primary: { key: string; label: string; value: string }[]
  hires: { key: string; label: string; value: string }[]
  other: { key: string; label: string; value: string }[]
  resources: ResourceCard[]
  warnings: string[]
  has_metadata: boolean
}

export interface ImageDetail extends ImageRow {
  source_label: string
  metadata: MetadataView
  duplicates: Duplicates
  raw_infotext: string | null
}

export interface PostImage {
  id: number
  post_id: number
  image_id: number | null
  position: number
  source_path: string
  sha256: string
  width: number | null
  height: number | null
  file_size: number | null
  thumbnail_path?: string | null
  remote_uuid: string | null
  remote_image_id: number | null
  /** Delivery URL on CivitAI. Set when there is no local file. */
  remote_url: string | null
  /** image | video | audio — videos are not fetched, only pointed at. */
  media_type: string
  content_type: string | null
  blurhash: string | null
  hide_meta: boolean
  /** CivitAI's rating as the word it uses (PG, PG-13, R, X, XXX, Blocked).
   *  Null while CivitAI has not rated the image yet - not the same as PG. */
  nsfw_label?: string | null
  /** Owner-visible CivitAI metadata origin; null until an owner sync supplies it. */
  remote_on_site: boolean | null
  dedup_ack: boolean
  edited?: boolean
  relative_path?: string
  is_missing?: boolean
  duplicates?: Duplicates
  parsed?: unknown
}

export interface BindingSuggestion {
  model_version_id: number
  model_id: number | null
  model_name: string | null
  version_name: string | null
  resource_type: string | null
  image_count: number
}

export interface PostEvent {
  id: number
  event: string
  at: string
  detail: Record<string, unknown> | null
}

export interface ResourceResolution {
  resolved: number
  unknown: number
  cached: number
  failed: number
  reapplied: number
}

export interface Post {
  id: number
  title: string
  detail: string
  state: PostState
  origin: string
  model_version_id: number | null
  bound_model_version_id: number | null
  model_name: string | null
  version_name: string | null
  collection_id: number | null
  publish_mode: 'schedule' | 'now' | 'draft_only'
  schedule_mode: 'relative' | 'absolute'
  schedule_offset_minutes: number | null
  scheduled_at: string | null
  resolved_publish_at: string | null
  publish_at_is_pinned?: boolean
  offset_label: string
  remote_post_id: number | null
  remote_published_at: string | null
  remote_state: string | null
  remote_url: string | null
  remote_diverged: boolean
  dirty: boolean
  last_error: string | null
  notes: string
  created_at: string
  updated_at: string
  can_schedule: boolean
  can_edit_remote: boolean
  has_remote: boolean
  image_count?: number
  thumbnails?: { image_id: number; thumbnail_path: string | null }[]
  tags: string[]
  images?: PostImage[]
  checklist?: Checklist
  suggested_bindings?: BindingSuggestion[]
  events?: PostEvent[]
  resource_resolution?: ResourceResolution
  remote_snapshot?: Record<string, unknown> | null
  archive_folder?: string | null
  archive_folder_rename?: { from: string; to: string } | null
  /** Images with no local file yet - what the board's search would look for. */
  unmatched_images?: number
}

export interface ArchivePreview {
  position: number
  image_id: number | null
  thumbnail_path: string | null
  remote_url: string | null
}

export interface ArchivePost extends Post {
  archive_date: string | null
  archive_published_at: string | null
  previews: ArchivePreview[]
}

export interface ArchiveListResponse {
  items: ArchivePost[]
  total: number
  offset: number
}

export interface SourceRoot {
  id: number
  path: string
  label: string
  enabled: boolean
  excluded_folders: string[]
  is_archive: boolean
  /** Opt-in periodic re-scan. Off by default, per folder. */
  watch_enabled: boolean
  last_scanned_at: string | null
  exists: boolean
}

export interface SourceDeletionImpact {
  images: number
  edits: number
  resource_overrides: number
}

export interface ModelRoot {
  id: number
  path: string
  label: string
  enabled: boolean
  /** Opt-in periodic re-hash. Off by default, per folder. */
  watch_enabled: boolean
  last_hashed_at: string | null
  exists: boolean
}

export interface ModelFileIdentity {
  source: 'file_stem' | 'ss_output_name' | 'modelspec_title'
  name: string
}

export interface ModelFileRow {
  id: number | null
  model_root_id: number | null
  absolute_path: string | null
  relative_path: string | null
  folder: string | null
  file_size: number | null
  sha256: string
  file_stem: string | null
  ss_output_name: string | null
  modelspec_title: string | null
  root_path: string | null
  root_label: string | null
  model_id: number | null
  model_version_id: number | null
  model_name: string | null
  version_name: string | null
  source: string | null
  http_status: number | null
  recognized: boolean
  locally_available: boolean
  identities: ModelFileIdentity[]
}

export interface ModelFileDuplicateGroup {
  sha256: string
  copies: number
  file_size: number
  redundant_size: number
  paths: {
    absolute_path: string
    relative_path: string
    model_root_id: number
    root_path: string
    root_label: string
  }[]
}

export interface ModelFileReport {
  items: ModelFileRow[]
  total: number
  limit: number
  offset: number
}

export interface ModelFileDuplicateReport {
  groups: ModelFileDuplicateGroup[]
  total: number
  redundant_size: number
  limit: number
  offset: number
}

export interface BackupInfo {
  name: string
  path: string
  size: number
  created_at: string
  automatic: boolean
  contents: Record<string, number | null>
}

export interface ArchivePlanEntry {
  image_id: number
  source_path: string
  name: string
  remote_post_id: number
  remote_image_id: number | null
  published_on: string | null
  how: string
  sha256: string | null
  target: string
}

export interface ArchiveStatus {
  configured: boolean
  reason?: string
  archive?: SourceRoot
  blocked?: string | null
  candidates?: number
  posts?: number
  unresolved?: number
  duplicates?: { remote_post_id: number; remote_image_id: number; paths: string[] }[]
}

export interface Job {
  id: number
  kind: string
  status: 'starting' | 'running' | 'done' | 'error' | 'cancelled'
  stage: string
  /** A stable name for the stage, so the frontend can say it in German. */
  stage_code?: string
  total: number
  processed: number
  succeeded: number
  failed: number
  skipped: number
  error: string | null
  result: Record<string, unknown>
  items: Record<string, unknown>[]
  created_at: string
  finished_at: string | null
}

export interface JobUpdates {
  items: Job[]
  cursor: number
}

export type DiscoveryState = 'draft' | 'scheduled' | 'published'

export interface DiscoveryItem {
  remote_post_id: number
  title: string | null
  state: DiscoveryState
  published_at: string | null
  image_count: number
  url: string
  cover_url: string | null
}

export interface DiscoverySummary {
  examined: number
  outside_period: number
  already_known: number
  adoptable: number
}

export interface DiscoveryResponse {
  items: DiscoveryItem[]
  summary: DiscoverySummary
  reason: string
  since: string | null
  next_cursor: string | number | null
  range_complete: boolean
  drafts_not_listable: boolean
  drafts_url: string | null
}

export interface AdoptResult {
  post_id: number
  created: boolean
}

export interface AdoptBatchOutcome {
  remote_post_id: number
  post_id: number | null
  outcome: 'created' | 'skipped' | 'not_yours' | 'not_found' | 'failed'
  /** How many images the adopted post has, and how many of them are local. */
  images: number
  images_local: number
  message: string
}

export interface DuplicateMember {
  id: number
  absolute_path: string
  relative_path: string
  folder: string
  file_size: number | null
  file_mtime: number | null
  width: number | null
  height: number | null
  sha256: string | null
  phash: string | null
  thumbnail_path: string | null
  seed: string | null
  first_seen_at: string
  generation_time: string | null
  post_bound: boolean
}

export interface DuplicateGroup {
  key: string
  /** certain = matching non-empty file or decoded-pixel SHA-256. */
  confidence: 'certain' | 'probable'
  reason: string
  archive_duplicate: boolean
  first_seen_by: 'generation_time' | 'library_entry'
  members: DuplicateMember[]
}

export interface DuplicateReport {
  running: Job | null
  groups: DuplicateGroup[]
  checked_at: string | null
}

export interface TrashedImage {
  id: number
  absolute_path: string
  trash_original_path: string
  trashed_at: string
  file_size: number | null
  width: number | null
  height: number | null
  thumbnail_path: string | null
}

export interface SpreadPlanEntry {
  post_id: number
  title: string
  publish_at: string
  offset_minutes: number | null
  locked: boolean
}

export interface ModelVersionResult {
  id: number
  name: string | null
  base_model: string | null
  published_at: string | null
  hash_autov2: string | null
  hash_sha256: string | null
  thumbnail_url: string | null
}

export interface ModelSearchResult {
  id: number
  name: string | null
  type: string | null
  nsfw: boolean | null
  creator: string | null
  download_count: number | null
  versions: ModelVersionResult[]
  thumbnail_url: string | null
}

/** A name somebody can give a hand-added resource, with the hash behind it. */
export interface ModelSuggestion {
  name: string
  hash: string
  model_id: number | null
  model_version_id: number | null
  model_name: string | null
  version_name: string | null
  /** local = a file in a model folder · known = an identity already in the map */
  source: 'local' | 'known'
}

/** A post that already holds an image somebody is about to put into another one. */
export interface PostHolder {
  post_id: number
  title: string
  state: string
}

export interface OauthStatus {
  /** idle · pending · connected · denied · failed */
  state: string
  scope: number | null
  redirect_uri: string
  error_code?: string | null
}

export interface SetupStatus {
  setup_required: boolean
  data_directory_ready: boolean
  default_path: string
}

export interface Settings {
  /** Whether the next authenticated call carries an OAuth connection. */
  civitai_auth_mode: 'oauth' | 'none'
  civitai_username: string | null
  site_base: string
  site_base_default: string
  phash_threshold: number
  require_title: boolean
  /** Periodic read of remote posts and their image ratings. Off by default. */
  watch_posts_enabled: boolean
  /** Publication retry ladder advertised in the behaviour panel. */
  watch_post_retry_minutes: number[]
  /** Slower cadence once every image already has a rating. */
  watch_post_rating_refresh_minutes: number
  app_version: string
  app_licence: string
  app_rights_holder: string
  /** Any of the three periodic checks is on, so a run may appear unasked. */
  watchers_active: boolean
  debug_logging: boolean
  usage_ping_enabled: boolean
  ui_language: string
  ui_grid_size: number
  ui_page_size: number
  ui_discover_limit: number
  ui_discover_days: number
  rate_limit_posts_per_day: string | null
  llm_python: string
  llm_model_dir: string
  llm_device: string
  llm_load_in_4bit: boolean
  llm_trust_remote_code: boolean
  llm_endpoint: string
  llm_endpoint_model: string
  llm_variants: number
  metadata_excluded_fields: string[]
  prompt_exclusions: string[]
  trash_folder: string
  adopted_folder: string
  setup: {
    source_folder: boolean
    archive_folder: boolean
    adopted_folder: boolean
    model_root: boolean
  }
  generation_time_patterns: string[]
  limits: {
    min_schedule_minutes: number
    max_schedule_months: number
    max_upload_bytes: number
  }
}

export interface MetadataFieldInventory {
  field_name: string
  image_count: number
  last_seen_at: string
}

export interface Budget {
  limit: number | null
  used: number
  remaining?: number
}

export interface ScopeProblem {
  scope: string
  label: string
  consequence: string
}

export interface ScopeReport {
  known: boolean
  mask?: number
  granted: string[]
  missing: string[]
  problems: ScopeProblem[]
}

export interface AccountInfo {
  account: Record<string, unknown> | null
  username: string | null
  budget: Budget | null
  /** Whether the next authenticated call carries an OAuth connection. */
  auth_mode: 'oauth' | 'none'
  scopes: ScopeReport
}

export interface CalendarEntry {
  post_id: number
  title: string
  state: PostState
  publish_at: string
  day: string
  is_relative: boolean
  /** True once CivitAI holds the time; the local plan no longer applies. */
  pinned: boolean
  offset_minutes: number | null
  offset_label: string
  remote_post_id: number | null
  locked: boolean
}

export interface CalendarView {
  entries: CalendarEntry[]
  per_day: Record<string, number>
  now: string
  earliest: string
  latest: string
  min_lead_minutes: number
  daily_limit: number | null
  used_today: number
}

export interface PlannedCall {
  transport: string
  call: string
  detail: string
  changed?: boolean
}

export interface PushPreview extends Checklist {
  post_id: number
  title: string
  publish_mode: string
  resolved_publish_at: string | null
  calls: PlannedCall[]
  comfyui_workflow_replaced_images: string[]
}

export interface PushItem {
  id: number
  post_id: number
  title: string
  status: string
  step: string
  step_label: string
  images_uploaded: number
  remote_post_id: number | null
  attempts: number
  error: string | null
}

export interface PushRun {
  id: number
  kind: string
  status: string
  total: number
  succeeded: number
  failed: number
  skipped: number
  error: string | null
  created_at: string
  finished_at: string | null
  items: PushItem[]
}

/**
 * What a suggestion is built from. `auto` is the rule - a model that sees works
 * from the pictures, one that cannot gets the generation prompts instead.
 */
export type SuggestMaterial = 'auto' | 'images' | 'prompt' | 'both'

export interface LlmStatus {
  python: string
  python_exists: boolean
  model_dir: string
  model_dir_exists: boolean
  device: string
  load_in_4bit: boolean
  trust_remote_code: boolean
  worker_running: boolean
  ready: boolean
  vision: boolean | null
  vision_error: string | null
  offloaded: boolean | null
  vision_fix_command: string
  resolved_device: string | null
  endpoint: string
  endpoint_model: string
  runtime: 'endpoint' | 'local'
}

export interface LlmProfile {
  id: number
  /** The identity. Survives a rename; what a restore matches on. */
  uuid: string
  name: string
  description: string
  system_prompt: string
  title_max_words: number
  tag_count: number
  include_images: boolean
  max_images: number
  vision_max_side: number
  max_new_tokens: number
  temperature: number
  top_p: number
  top_k: number
  is_builtin: boolean
  sort_order: number
}

export type LlmProfileInput = Omit<LlmProfile, 'id' | 'uuid' | 'is_builtin' | 'sort_order'>

export interface Suggestion {
  id: number
  post_id: number
  field: string
  text: string
  reason: string
  seed: number | null
  accepted: boolean
  created_at: string
}

export interface OffsetParse {
  ok: boolean
  minutes: number | null
  effective_minutes?: number
  raised?: boolean
  label: string
  publish_at: string | null
  floor_minutes?: number
}
