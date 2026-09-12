-- ============================================================================
-- Livebound schema
--
-- Every timestamp is ISO-8601 UTC with a trailing Z. Scheduling is this app's
-- core job and CivitAI's 60-minute boundary is unforgiving, so local time
-- exists only in the browser.
--
-- Recurring theme below: local *intent* and confirmed *remote truth* live in
-- separate columns. Overwriting one with the other would hide the exact
-- situation the user needs to see - that someone changed the post on the site.
-- ============================================================================


CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- keys: civitai_username, civitai_oauth_client_id, civitai_oauth_access_token,
--       civitai_oauth_refresh_token, civitai_oauth_expires_at, civitai_oauth_scope,
--       account_json (cached whoami), llm_*,
--       phash_threshold, require_title, trash_folder, generation_time_patterns,
--       setup_completed, usage_ping_enabled, usage_ping_installation_id

INSERT OR IGNORE INTO settings(key, value) VALUES('debug_logging', '0');


-- --- image sources ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS source_roots (
    id               INTEGER PRIMARY KEY,
    path             TEXT UNIQUE NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    -- The move target for already-published images. Scanned like any other
    -- folder, but never a source for the move itself - otherwise archiving
    -- would shuffle files within the archive.
    is_archive       INTEGER NOT NULL DEFAULT 0,
    recursive        INTEGER NOT NULL DEFAULT 1,
    excluded_folders TEXT NOT NULL DEFAULT '[]',   -- JSON array of folder names
    -- Opt-in periodic re-scan of this folder. Off by default, per folder, so a
    -- network share is never polled unless the maintainer says so.
    watch_enabled    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    last_scanned_at  TEXT
);


-- Model folders are deliberately separate from image sources. Putting one in
-- source_roots would make the image scanner try to ingest checkpoints as pictures.
CREATE TABLE IF NOT EXISTS model_roots (
    id             INTEGER PRIMARY KEY,
    path           TEXT UNIQUE NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    enabled        INTEGER NOT NULL DEFAULT 1,
    -- Same opt-in as source_roots, and off for the same reason.
    watch_enabled  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    last_hashed_at TEXT
);


-- The file facts make a later run cheap: unchanged bytes keep their known full
-- SHA256, while resource_map remains the durable CivitAI identity cache.
CREATE TABLE IF NOT EXISTS model_files (
    id              INTEGER PRIMARY KEY,
    model_root_id   INTEGER NOT NULL REFERENCES model_roots(id) ON DELETE CASCADE,
    absolute_path   TEXT NOT NULL,
    relative_path   TEXT NOT NULL,
    file_mtime      REAL NOT NULL,
    file_size       INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    hash_prefix     TEXT NOT NULL,
    file_stem       TEXT NOT NULL DEFAULT '',
    ss_output_name  TEXT,
    modelspec_title TEXT,
    hashed_at       TEXT NOT NULL,
    UNIQUE(model_root_id, absolute_path)
);
CREATE INDEX IF NOT EXISTS idx_model_files_root ON model_files(model_root_id);
CREATE INDEX IF NOT EXISTS idx_model_files_hash ON model_files(sha256);


-- One row per file on disk. sha256 is deliberately NOT unique: the same bytes
-- legitimately live at several paths (an "approved" copy next to the original).
-- The question "was this already posted?" is answered from image_usage, never
-- from here.
CREATE TABLE IF NOT EXISTS images (
    id             INTEGER PRIMARY KEY,
    source_root_id INTEGER NOT NULL REFERENCES source_roots(id) ON DELETE CASCADE,
    absolute_path  TEXT UNIQUE NOT NULL,
    relative_path  TEXT NOT NULL,
    folder         TEXT NOT NULL DEFAULT '',
    file_mtime     REAL,
    file_size      INTEGER,
    content_type   TEXT,
    width          INTEGER,
    height         INTEGER,
    sha256         TEXT,          -- exact-duplicate key
    pixel_sha256   TEXT,          -- SHA256 of RGB dimensions and decoded pixels
    phash          TEXT,          -- 16 hex chars = 64-bit dHash
    raw_infotext   TEXT,          -- the A1111 `parameters` chunk, verbatim
    parsed_json    TEXT,          -- the parsed document, for display only
    ingest_revision INTEGER NOT NULL DEFAULT 0,
    thumbnail_path TEXT,          -- file name in the cache; UI only, NEVER an upload
    is_missing     INTEGER NOT NULL DEFAULT 0,
    is_trashed     INTEGER NOT NULL DEFAULT 0,
    trash_original_path TEXT,     -- exact restore target while the file is in trash
    trashed_at     TEXT,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,

    -- The same banded index image_usage carries, for the same reason: finding
    -- the pictures that look alike within the library itself would otherwise be
    -- O(n^2) over every row. See image_usage below for why eight bands.
    phash_b0       INTEGER,
    phash_b1       INTEGER,
    phash_b2       INTEGER,
    phash_b3       INTEGER,
    phash_b4       INTEGER,
    phash_b5       INTEGER,
    phash_b6       INTEGER,
    phash_b7       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_images_root   ON images(source_root_id, is_missing);
CREATE INDEX IF NOT EXISTS idx_images_trash  ON images(is_trashed, trashed_at);
CREATE INDEX IF NOT EXISTS idx_images_folder ON images(source_root_id, folder);
CREATE INDEX IF NOT EXISTS idx_images_sha    ON images(sha256);
CREATE INDEX IF NOT EXISTS idx_images_pixels ON images(pixel_sha256);
CREATE INDEX IF NOT EXISTS idx_images_phash  ON images(phash);
CREATE INDEX IF NOT EXISTS idx_images_b0 ON images(phash_b0);
CREATE INDEX IF NOT EXISTS idx_images_b1 ON images(phash_b1);
CREATE INDEX IF NOT EXISTS idx_images_b2 ON images(phash_b2);
CREATE INDEX IF NOT EXISTS idx_images_b3 ON images(phash_b3);
CREATE INDEX IF NOT EXISTS idx_images_b4 ON images(phash_b4);
CREATE INDEX IF NOT EXISTS idx_images_b5 ON images(phash_b5);
CREATE INDEX IF NOT EXISTS idx_images_b6 ON images(phash_b6);
CREATE INDEX IF NOT EXISTS idx_images_b7 ON images(phash_b7);


-- A dismissal answers a pairwise question: these two library rows are not the
-- same picture even when an automatic fingerprint says otherwise. Canonical
-- ordering makes the decision independent of which image was selected first.
CREATE TABLE IF NOT EXISTS duplicate_dismissals (
    image_id_a   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    image_id_b   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    dismissed_at TEXT NOT NULL,
    CHECK(image_id_a < image_id_b),
    PRIMARY KEY(image_id_a, image_id_b)
);


-- Literal, case-insensitive substring search over the two user-visible text
-- fields. External content avoids storing a second copy of the raw metadata.
CREATE VIRTUAL TABLE IF NOT EXISTS image_search USING fts5(
    relative_path,
    raw_infotext,
    content='images',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS images_search_insert
AFTER INSERT ON images
BEGIN
    INSERT INTO image_search(rowid, relative_path, raw_infotext)
    VALUES (NEW.id, NEW.relative_path, NEW.raw_infotext);
END;

CREATE TRIGGER IF NOT EXISTS images_search_delete
AFTER DELETE ON images
BEGIN
    INSERT INTO image_search(image_search, rowid, relative_path, raw_infotext)
    VALUES ('delete', OLD.id, OLD.relative_path, OLD.raw_infotext);
END;

CREATE TRIGGER IF NOT EXISTS images_search_update
AFTER UPDATE OF relative_path, raw_infotext ON images
BEGIN
    INSERT INTO image_search(image_search, rowid, relative_path, raw_infotext)
    VALUES ('delete', OLD.id, OLD.relative_path, OLD.raw_infotext);
    INSERT INTO image_search(rowid, relative_path, raw_infotext)
    VALUES (NEW.id, NEW.relative_path, NEW.raw_infotext);
END;


-- Every generation parameter seen by a scan. Membership is kept separately so
-- rescanning one image replaces its fields instead of inflating these counts.
-- Inventory rows remain at zero when their last image disappears: last_seen_at
-- is useful precisely because it remembers that the field existed before.
CREATE TABLE IF NOT EXISTS metadata_field_inventory (
    field_name   TEXT PRIMARY KEY,
    image_count  INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_metadata_fields (
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL REFERENCES metadata_field_inventory(field_name),
    PRIMARY KEY(image_id, field_name)
);

CREATE TRIGGER IF NOT EXISTS metadata_field_membership_insert
AFTER INSERT ON image_metadata_fields
BEGIN
    UPDATE metadata_field_inventory
    SET image_count = image_count + 1
    WHERE field_name = NEW.field_name;
END;

CREATE TRIGGER IF NOT EXISTS metadata_field_membership_delete
AFTER DELETE ON image_metadata_fields
BEGIN
    UPDATE metadata_field_inventory
    SET image_count = MAX(image_count - 1, 0)
    WHERE field_name = OLD.field_name;
END;


-- Resources parsed out of one image's infotext. Sole purpose: propose a
-- modelVersionId for the post binding. Rebuilt from scratch on every scan.
CREATE TABLE IF NOT EXISTS image_resources (
    id               INTEGER PRIMARY KEY,
    image_id         INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    resource_type    TEXT NOT NULL,   -- checkpoint | lora | embedding | upscaler
    name_in_prompt   TEXT NOT NULL,
    hash             TEXT,
    hash_prefix      TEXT NOT NULL DEFAULT '',   -- see resource_map.hash_prefix
    weight           REAL,
    model_id         INTEGER,
    model_version_id INTEGER,
    model_name       TEXT,
    version_name     TEXT,
    resolved_from    TEXT,            -- embedded | resource_map | local_name | rest | unresolved

    -- User overrides. replace_resources() deletes and re-inserts every row on
    -- each scan, so these three are carried forward there by hand - without
    -- that, the next scan would quietly undo the user's work.
    locked_by_user   INTEGER NOT NULL DEFAULT 0,   -- keep this resolution, do not re-resolve
    added_by_user    INTEGER NOT NULL DEFAULT 0,   -- not in the infotext; the user put it here
    deleted_by_user  INTEGER NOT NULL DEFAULT 0,   -- in the infotext, but not to be credited
    UNIQUE(image_id, resource_type, name_in_prompt)
);
CREATE INDEX IF NOT EXISTS idx_image_resources_image ON image_resources(image_id);
CREATE INDEX IF NOT EXISTS idx_image_resources_hash  ON image_resources(hash_prefix);


-- Metadata the user changed, kept apart from what the file says.
--
-- The file is never rewritten: an original image is never modified or re-encoded.
-- An edit lives here until the moment of an upload, when a temporary copy carrying
-- it is made and thrown away again afterwards. That way the original state and the
-- change are both still here, and the picture exists once on disk rather than twice.
--
-- `draft_json` holds only the fields that differ, not a whole document: the file
-- remains the source, and a rescan that finds new values must not be overruled
-- by a copy of the old ones.
CREATE TABLE IF NOT EXISTS image_edits (
    image_id            INTEGER PRIMARY KEY REFERENCES images(id) ON DELETE CASCADE,
    draft_json          TEXT NOT NULL DEFAULT '{}',   -- {prompt, negative_prompt, fields{}}
    touched_fields_json TEXT NOT NULL DEFAULT '[]',   -- what the user set, even to ''
    deleted_fields_json TEXT NOT NULL DEFAULT '[]',   -- what the user removed
    updated_at          TEXT NOT NULL
);


-- hash -> CivitAI resource. Seeded once during the v1.1 migration and maintained
-- by REST lookups. Negative results are cached too (http_status
-- 404): without that, every rescan re-queries the same permanently unknown
-- hashes and burns the rate limit.
CREATE TABLE IF NOT EXISTS resource_map (
    hash             TEXT PRIMARY KEY,   -- uppercased, as written in the infotext
    -- Generators disagree on how much of the SHA256 they write: `Model hash`
    -- carries the 10-char AutoV2, `Lora hashes` often 12, `Hashes` sometimes the
    -- full digest. They are prefixes of one another, so all lookups go through
    -- this common 10-char key - matching on the raw string would silently miss.
    hash_prefix      TEXT NOT NULL DEFAULT '',
    resource_type    TEXT,
    model_id         INTEGER,
    model_version_id INTEGER,
    model_name       TEXT,
    version_name     TEXT,
    thumbnail_url    TEXT,
    http_status      INTEGER,
    -- manual is a behavioural lock: automatic lookups may not replace its prefix.
    source           TEXT NOT NULL,      -- metadata-app (historic) | rest | manual
    queried_at       TEXT NOT NULL,
    raw_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_resource_map_version ON resource_map(model_version_id);
CREATE INDEX IF NOT EXISTS idx_resource_map_prefix  ON resource_map(hash_prefix);


-- Model versions the user posts against, for the binding picker.
CREATE TABLE IF NOT EXISTS model_bindings (
    model_version_id INTEGER PRIMARY KEY,
    model_id         INTEGER,
    model_name       TEXT,
    version_name     TEXT,
    resource_type    TEXT,               -- checkpoint | lora
    is_favorite      INTEGER NOT NULL DEFAULT 0,
    last_used_at     TEXT
);


-- --- posts ------------------------------------------------------------------

-- `state` is written ONLY by posts/lifecycle.py::transition().
CREATE TABLE IF NOT EXISTS posts (
    id                     INTEGER PRIMARY KEY,
    title                  TEXT NOT NULL DEFAULT '',
    detail                 TEXT NOT NULL DEFAULT '',
    state                  TEXT NOT NULL DEFAULT 'draft',
    origin                 TEXT NOT NULL DEFAULT 'local',    -- local | imported

    -- modelVersionId can only be set at creation - post.update has no such
    -- field. `model_version_id` is the editable intent, `bound_model_version_id`
    -- is what CivitAI actually holds. A difference means "rebuild required",
    -- never "send an update", because the update would be silently ignored.
    model_version_id       INTEGER,
    bound_model_version_id INTEGER,
    model_name             TEXT,
    version_name           TEXT,
    collection_id          INTEGER,
    collection_tag_id      INTEGER,

    publish_mode           TEXT NOT NULL DEFAULT 'schedule',  -- schedule | now | draft_only

    -- A locally planned post may sit in the queue for days, so an absolute time
    -- chosen while drafting can be in the past by the time it is pushed - and a
    -- past publishedAt makes CivitAI publish immediately. A relative plan ("in
    -- 3h 20m") is therefore resolved to a wall-clock time at push time, never
    -- before, and can never go stale.
    schedule_mode          TEXT NOT NULL DEFAULT 'relative',  -- relative | absolute
    schedule_offset_minutes INTEGER,      -- used when schedule_mode='relative'
    scheduled_at           TEXT,          -- absolute intent, UTC Z; also the resolved
                                          -- value written back when a push computes it

    remote_post_id         INTEGER UNIQUE,
    remote_published_at    TEXT,          -- last value CONFIRMED by get_post
    remote_state           TEXT,          -- draft | scheduled | published | missing
    remote_url             TEXT,
    remote_synced_at       TEXT,
    remote_snapshot_json   TEXT,          -- last get_post payload, for the 3-way diff
    remote_diverged        INTEGER NOT NULL DEFAULT 0,

    -- exactly what we last successfully sent; the base for a minimal re-push
    pushed_fields_json     TEXT,
    dirty                  INTEGER NOT NULL DEFAULT 0,

    push_run_id            INTEGER,       -- claim marker while a run owns this post
    last_error             TEXT,
    notes                  TEXT NOT NULL DEFAULT '',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_state     ON posts(state);
CREATE INDEX IF NOT EXISTS idx_posts_scheduled ON posts(scheduled_at);


-- Ordered membership plus the local -> remote image mapping.
--
-- The file snapshot (source_path, sha256, size, dimensions) is duplicated here
-- on purpose: a post must stay pushable and auditable after the source file has
-- been moved or deleted. That is also why image_id is SET NULL, not CASCADE.
CREATE TABLE IF NOT EXISTS post_images (
    id              INTEGER PRIMARY KEY,
    post_id         INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    image_id        INTEGER REFERENCES images(id) ON DELETE SET NULL,
    position        INTEGER NOT NULL,

    source_path     TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    phash           TEXT,
    file_size       INTEGER,
    width           INTEGER,
    height          INTEGER,
    content_type    TEXT,
    -- image | video | audio. Videos are deliberately neither fetched nor
    -- pushed - those are handled outside this app. The row stays all the same:
    -- the post has it, and hiding that would be a lie.
    media_type      TEXT NOT NULL DEFAULT 'image',

    remote_uuid     TEXT,      -- from upload_image, consumed when the post is created
    uploaded_at     TEXT,      -- uuid freshness; a stale uuid is re-uploaded
    -- The placeholder CivitAI shows while the image loads. Computed locally,
    -- because nothing sends it back and without it the edit view shows an empty
    -- grey box where a blurred preview belongs.
    blurhash        TEXT,
    remote_image_id INTEGER,   -- numeric id from get_post: reorderImages / image.delete
    remote_url      TEXT,
    hide_meta       INTEGER NOT NULL DEFAULT 0,
    -- CivitAI's owner-visible meta says whether it was generated on site.
    -- NULL means the owner detail has not supplied that fact yet.
    remote_on_site  INTEGER CHECK(remote_on_site IN (0, 1) OR remote_on_site IS NULL),
    -- CivitAI's content rating as the bitmask it sends (config.NSFW_LEVEL_LABELS).
    -- NULL or 0 means not rated yet, which is not the same as PG.
    nsfw_level      INTEGER,

    dedup_ack       INTEGER NOT NULL DEFAULT 0,  -- user acknowledged a duplicate warning
    UNIQUE(post_id, position),
    UNIQUE(post_id, image_id)
);
CREATE INDEX IF NOT EXISTS idx_post_images_post   ON post_images(post_id, position);
CREATE INDEX IF NOT EXISTS idx_post_images_sha    ON post_images(sha256);
-- NOTE: UNIQUE(post_id, position) makes a reorder a two-phase renumber inside a
-- single transaction (first to negative positions, then to the final values).
-- SQLite has no deferrable constraints.


-- remote_tag_id is not a nicety: post.removeTag takes a tagId, not a name, so
-- without it a tag added by this app can never be removed again.
CREATE TABLE IF NOT EXISTS post_tags (
    id            INTEGER PRIMARY KEY,
    post_id       INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    position      INTEGER NOT NULL DEFAULT 0,
    remote_tag_id INTEGER,
    is_transient  INTEGER NOT NULL DEFAULT 0,  -- the reconcile marker tag
    pushed_at     TEXT,
    UNIQUE(post_id, name)
);
CREATE INDEX IF NOT EXISTS idx_post_tags_post ON post_tags(post_id);


-- --- upload / dedup memory --------------------------------------------------

-- Content-addressed record of every successful MCP upload. This is what makes an
-- interrupted batch resumable without re-sending megabytes per image.
CREATE TABLE IF NOT EXISTS upload_assets (
    sha256              TEXT NOT NULL,
    uuid                TEXT NOT NULL,
    width               INTEGER,
    height              INTEGER,
    content_type        TEXT,
    file_size           INTEGER,
    uploaded_at         TEXT NOT NULL,
    consumed_by_post_id INTEGER,
    PRIMARY KEY(sha256, uuid)
);
CREATE INDEX IF NOT EXISTS idx_upload_assets_sha ON upload_assets(sha256, uploaded_at DESC);


-- The dedup memory, append-only and deliberately denormalised. Deleting the
-- local post, the image row or the file itself must NOT erase the fact that
-- these bytes were once published - that is the entire point of the warning.
-- Hence ON DELETE SET NULL, never CASCADE, and the copied hashes.
CREATE TABLE IF NOT EXISTS image_usage (
    id              INTEGER PRIMARY KEY,
    sha256          TEXT NOT NULL,
    pixel_sha256    TEXT,
    phash           TEXT,
    post_id         INTEGER REFERENCES posts(id) ON DELETE SET NULL,
    remote_post_id  INTEGER,
    remote_image_id INTEGER,
    source_path     TEXT,
    post_title      TEXT,
    used_at         TEXT NOT NULL,
    status          TEXT NOT NULL,  -- planned | pushed | published | withdrawn
    origin          TEXT NOT NULL DEFAULT 'push',  -- push | backfill

    -- Generation identity is retained as comparison detail for perceptual
    -- candidates. It never settles image identity: one request can produce
    -- several different outputs.
    seed            TEXT,
    prompt_hash     TEXT,           -- sha256 of the normalised positive prompt

    -- The 64-bit phash split into eight 8-bit bands, indexed. By the pigeonhole
    -- principle two hashes within Hamming distance <= 7 must agree on at least
    -- one band, so a candidate lookup is eight indexed equality probes instead
    -- of a scan over the whole history. Without this, checking one library page
    -- costs a full pass per tile - measurably seconds once a few thousand
    -- images have been posted, and growing from there.
    phash_b0        INTEGER,
    phash_b1        INTEGER,
    phash_b2        INTEGER,
    phash_b3        INTEGER,
    phash_b4        INTEGER,
    phash_b5        INTEGER,
    phash_b6        INTEGER,
    phash_b7        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_image_usage_sha    ON image_usage(sha256);
CREATE INDEX IF NOT EXISTS idx_image_usage_pixels ON image_usage(pixel_sha256);
CREATE INDEX IF NOT EXISTS idx_image_usage_seed   ON image_usage(seed, prompt_hash);
CREATE INDEX IF NOT EXISTS idx_image_usage_remote ON image_usage(remote_post_id);
CREATE INDEX IF NOT EXISTS idx_usage_b0 ON image_usage(phash_b0);
CREATE INDEX IF NOT EXISTS idx_usage_b1 ON image_usage(phash_b1);
CREATE INDEX IF NOT EXISTS idx_usage_b2 ON image_usage(phash_b2);
CREATE INDEX IF NOT EXISTS idx_usage_b3 ON image_usage(phash_b3);
CREATE INDEX IF NOT EXISTS idx_usage_b4 ON image_usage(phash_b4);
CREATE INDEX IF NOT EXISTS idx_usage_b5 ON image_usage(phash_b5);
CREATE INDEX IF NOT EXISTS idx_usage_b6 ON image_usage(phash_b6);
CREATE INDEX IF NOT EXISTS idx_usage_b7 ON image_usage(phash_b7);


-- --- push runs (resumability) -----------------------------------------------

CREATE TABLE IF NOT EXISTS push_runs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,   -- push | repush | reschedule | remote_delete | sync
    status       TEXT NOT NULL,   -- pending|running|done|error|cancelled|interrupted
    options_json TEXT,
    total        INTEGER NOT NULL DEFAULT 0,
    succeeded    INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);


-- The resume cursor. `step` advances only AFTER the write that makes that step's
-- effect durable, so a crash always resumes at a step which is safe to repeat.
--
-- `create_attempted_at` is committed BEFORE the create_post call. It is the only
-- way to notice a post that may exist on CivitAI but is unknown here, because
-- create_post has no idempotency key: a blind retry would create a second post.
CREATE TABLE IF NOT EXISTS push_items (
    id                  INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES push_runs(id) ON DELETE CASCADE,
    post_id             INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error|skipped
    step                TEXT NOT NULL DEFAULT 'queued',
    images_uploaded     INTEGER NOT NULL DEFAULT 0,
    create_attempted_at TEXT,
    create_request_json TEXT,     -- the exact arguments sent, used for reconciliation
    reconcile_tag       TEXT,     -- e.g. livebound-r17-p204; removed again before scheduling
    remote_post_id      INTEGER,
    attempts            INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    updated_at          TEXT NOT NULL,
    UNIQUE(run_id, post_id)
);
CREATE INDEX IF NOT EXISTS idx_push_items_run ON push_items(run_id, status);


-- Append-only audit trail, and the source of truth for the rate-limit budget:
-- counting 'created' events survives the local deletion of a post row, which
-- counting posts would not.
CREATE TABLE IF NOT EXISTS post_events (
    id             INTEGER PRIMARY KEY,
    post_id        INTEGER,
    remote_post_id INTEGER,
    event          TEXT NOT NULL,   -- created|scheduled|published|updated|tag_added|
                                    -- tag_removed|image_deleted|deleted|synced|error
    at             TEXT NOT NULL,
    detail_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_post_events_at   ON post_events(at);
CREATE INDEX IF NOT EXISTS idx_post_events_post ON post_events(post_id, at);


-- Bounded diagnostic log, aimed at the one unofficial surface (tRPC). Bodies are
-- stored WITHOUT the Authorization header, truncated, and pruned to the last N.
CREATE TABLE IF NOT EXISTS api_calls (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    transport     TEXT NOT NULL,   -- mcp | trpc | rest
    procedure     TEXT NOT NULL,
    http_status   INTEGER,
    duration_ms   INTEGER,
    ok            INTEGER NOT NULL DEFAULT 1,
    request_json  TEXT,
    response_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_calls_at ON api_calls(at);


-- --- LLM --------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS llm_profiles (
    id              INTEGER PRIMARY KEY,
    -- The identity. It survives a rename, a reorder and a delete-and-restore,
    -- and it is what `restore_builtins` matches on. The four shipped profiles
    -- carry fixed values written out in `backend/llm/profiles.py`.
    uuid            TEXT NOT NULL,
    -- Only a label. Two profiles may share one; the uuid tells them apart,
    -- and the settings list shows it under the name.
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    system_prompt   TEXT NOT NULL,
    title_max_words INTEGER NOT NULL DEFAULT 7,
    tag_count       INTEGER NOT NULL DEFAULT 7,
    include_images  INTEGER NOT NULL DEFAULT 1,
    max_images      INTEGER NOT NULL DEFAULT 1,   -- 1 = the grid is the whole view
    vision_max_side INTEGER NOT NULL DEFAULT 768,   -- size of one picture in the grid
    -- Generous on purpose: a reasoning model still preambles a little, and
    -- running out mid-sentence yields no JSON at all.
    max_new_tokens  INTEGER NOT NULL DEFAULT 700,
    temperature     REAL NOT NULL DEFAULT 0.7,
    top_p           REAL NOT NULL DEFAULT 0.8,
    top_k           INTEGER NOT NULL DEFAULT 20,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);


-- Suggestions are kept rather than written straight into the post, so the user
-- can regenerate, compare and undo without losing what they had written.
CREATE TABLE IF NOT EXISTS llm_suggestions (
    id         INTEGER PRIMARY KEY,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,      -- title | detail | tags
    text       TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    profile_id INTEGER,
    seed       INTEGER,             -- NULL for rows written before seeds were kept
    accepted   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_suggestions_post ON llm_suggestions(post_id, field);
-- As an index rather than an inline UNIQUE, so a database that was migrated
-- and one created from this file have the same shape: SQLite cannot add a
-- UNIQUE column with ALTER TABLE, only an index afterwards.
CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_profiles_uuid ON llm_profiles(uuid);


-- Cached collection picker list.
CREATE TABLE IF NOT EXISTS collections (
    id         INTEGER PRIMARY KEY,   -- CivitAI collection id
    name       TEXT NOT NULL,
    kind       TEXT,
    fetched_at TEXT NOT NULL
);
