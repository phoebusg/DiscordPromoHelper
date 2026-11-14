# Forum exchange v1 — shared field contract

Updated 14 November 2026. Normative field names for parallel implementation; transport/validation code is still to implement. See the [public forum design](https://github.com/phoebusg/DiscordPromoHelper/blob/661d07577daefa9ad8b8b5eaceee0952c7248fe4/docs/FORUM_SUPPORT_PLAN.md) for behavior.

## Common encoding and fields

One UTF-8 JSON object per file. Write temporary files then rename atomically; persist a received record before acknowledging. Repeat delivery of the same `record_id` is harmless; different content under the same ID is a conflict.

Every record requires these keys:

| Key | Type / meaning |
| --- | --- |
| `schema` | Exactly one of the four record names below; no second discriminator |
| `record_id` | Globally unique opaque string for the record |
| `producer_id` | Stable opaque producer installation ID |
| `created_at` | RFC 3339 UTC string with `Z` (whole-second or fractional-second precision; compare chronologically as parsed UTC timestamps, not raw ASCII strings) |
| `scope` | Object with `guild_id`, `forum_id`, `actor` |

All Discord IDs are decimal **strings**; null is allowed only where a field explicitly says so. Scope guild/forum IDs and exchanged topic thread IDs must be resolved non-null IDs. Actor shape: `{ "user_id": null, "label": "Selected account" }`; `user_id` is an observed ID string or null. A label alone does not verify identity. Staging checks the current account/target, not just these fields.

Revision fields are positive integers local to the named entity. Hashes are lowercase SHA-256 hex of UTF-8 text with CRLF converted to LF, preserving other whitespace, punctuation and Unicode. Unknown IDs/remote times are null, never invented. Source observations are preserved separately.

Cursor shape: `{ "stream_id": "opaque-stream", "position": "opaque-position" }`. Consumers persist/return opaque positions without incrementing/comparing them. Changed stream ID requires snapshot reconciliation. Each producer owns one ordered change stream per mirror scope.

## forum.snapshot.v1

Additional required keys: `snapshot_id` (stable mirror-scope string), `revision`, `captured_at` (UTC), `cursor`, `forum`, `topics`, `messages`, `requirements`, `coverage`.

`forum` required keys: `forum_id`, `name`, `source_url`, `tags` (array), `revision`, `observed_at`.

Topic required keys: `thread_id`, `forum_id`, `title`, `source_url`, `revision`, `observed_at`, `tags`, `starter_message_id` (string/null), `archived` (boolean/null), `locked` (boolean/null). Optional `remote_last_activity_at` is string/null, distinct from local capture time.

**Unresolved topics are not exchanged as topics.** `thread_id` and `forum_id` must be verified non-null decimal strings; a topic `source_url` must identify that thread. Keep unresolved observations locally under distinct stable `observation_ref` keys, never a shared null key or a title-derived identity. Report them in `coverage.pending` with `observation_ref`, null `thread_id`, a reason and a resume token. Do not emit their messages or topic changes into the exchange until the parent thread is resolved. This keeps every exchanged message associated with a known topic.

When resolved, retain the local observation-to-thread mapping, reconcile repeated observations without merging distinct topics by name, and emit one `added` topic event under its verified thread ID. Then export associated messages using that ID. Repeated refreshes must not emit another addition. Two unresolved topics with identical titles remain distinct local observations and pending entries; neither is stageable until resolved. A previously verified ID is never reassigned merely because OCR/title text changes.

Message required keys: `entity_ref` (stable opaque string), `message_id` (string/null), `thread_id`, `author` (actor shape), `body` (string), `content_hash`, `revision`, `observed_at`, `remote_created_at` (string/null), `remote_edited_at` (string/null), `source_url` (string/null), `identity_verified` (boolean), `attachments` (array). With no remote ID, use an explicitly provisional `entity_ref`; do not merge messages just by author/time. Later reconciliation retains an alias. Optional `recognition` supplies method/confidence without rewriting source text.

Requirement required keys: `requirement_id` (stable string), `source_entity_ref` (string/null), `source_url` (string/null), `body`, `content_hash`, `revision`, `observed_at`. Optional parsed checks supplement full source text.

Tag shape: `{ "tag_id": null, "name": "Example" }`, using a remote string ID when known. Attachment shape: `attachment_id` (string/null), `name`, `source_url` (string/null), `sha256` (string/null), `local_ref` (string/null). Null local data does not mean an attachment was archived.

Coverage required shape:

```json
{
  "topics": "partial",
  "histories": "partial",
  "requirements": "partial",
  "pending": [{"thread_id": "100000000000000003", "reason": "older_history_unread", "resume_token": null}],
  "errors": [],
  "last_success_at": null
}
```

Coverage status values: `not_started`, `partial`, `complete`, each for the configured accessible scope/checkpoint. Resume tokens are opaque provider strings/null. A pending entry with null `thread_id` requires a unique stable `observation_ref`; pending known topics retain their verified ID. Unresolved observations keep topic coverage partial. Errors are objects with `code`, `message` and nullable `thread_id`. Topic coverage does not imply history coverage. Merge partial snapshots without deleting unseen prior records.

## forum.changes.v1

Additional required keys: `snapshot_id`, `snapshot_revision`, `after_cursor`, `cursor`, `changes` (array). `after_cursor` is the consumer's previous cursor; `cursor` is the producer checkpoint through this batch.

Every change requires:

| Key | Type / meaning |
| --- | --- |
| `change_id` | Stable opaque ID, deduplicated independently of delivery |
| `entity_type` | `forum`, `topic`, `message`, `requirement` |
| `entity_ref` | Stable key matching the snapshot entity |
| `kind` | `added`, `edited`, `requirements_changed`, `tags_changed`, `state_changed`, `attachment_changed`, `removal_observed`, `recognition_corrected` |
| `before_revision`, `after_revision` | Positive integer/null |
| `observed_at` | UTC time |
| `source` | Object: `method` (string), `source_url` (string/null), `confidence` (0..1/null) |
| `after` | Updated full entity object/null |

`after` enables an incremental consumer without a fresh full snapshot for every event. For topic/forum/requirement entities, `entity_ref` is respectively the `thread_id`/`forum_id`/`requirement_id`; messages use their explicit `entity_ref`. Removal needs supporting evidence; missing from a partial scan does not qualify. Recognition confidence is not accepted community status. Apply a batch and its cursor transactionally; an empty changes array can advance a checkpoint.

## draft.request.v1

Additional required keys: `draft_id` (stable string), `revision`, `action`, `destination`, `body` (exact multiline text), `title` (string/null), `tags` (array), `evidence` (array), `baseline`.

Action values: `reply_existing`, `edit_own_message`, `create_topic`. Reply/edit need a thread ID; edit needs an observed own message ID. Create-topic requires a title and starts with null thread/message IDs. Destination and scope IDs must agree; unknown/conflicting identity requires resolution before staging.

Destination and baseline shape, with fictional IDs:

```json
{
  "destination": {
    "guild_id": "100000000000000001",
    "forum_id": "100000000000000002",
    "thread_id": "100000000000000003",
    "message_id": null,
    "source_url": "https://discord.com/channels/100000000000000001/100000000000000003"
  },
  "baseline": {
    "snapshot_id": "mirror-example",
    "snapshot_revision": 4,
    "requirements": [{"requirement_id": "guidelines-example", "revision": 2}],
    "message_content_hash": null
  }
}
```

All shown destination/baseline keys are required, nullable where shown. Unknown baseline snapshot fields may be null; null is not current. Own-message edits require the imported message content hash. Changed requirements/baseline cause review, not an unnoticed rewrite.

Evidence required keys: `evidence_id`, `name`, `media_type`, `sha256`, `local_ref`. `local_ref` is a relative path under a configured transfer root; absolute producer paths are not portable. Reject traversal outside that root, missing files and hash mismatches. Stage only selected evidence, after referenced files have arrived. Hash evidence bytes without text normalization.

Same draft ID/revision with different content is a conflict. A later revision must not overwrite the person's edits in an existing composer.

## handoff.receipt.v1

Additional required keys: `receipt_id`, `draft_id`, `draft_revision`, `destination` (same shape), `status`, `observed_at`, `message_id` (string/null), `message_url` (string/null), `observed_body_hash` (string/null), `evidence_ids` (array), `reason` (string/null). IDs are opaque strings except remote Discord IDs.

Statuses: `staged`, `in_discord_review`, `submission_observed`, `user_confirmed_submitted`, `needs_review`, `failed`. Common `scope.actor` describes the observed account at this step. Staged means verified text/selected attachment previews in the correct composer, not sent. The application never invokes final Send/save for this flow.

Submission observation needs a later correlated message and actor/target evidence. Preserve the submitted hash if the person edited the draft; ambiguous correlation remains unresolved, not a duplicate staging attempt. User confirmation stays labelled as such. Receipts are immutable observations: new states emit new receipt/record IDs, not mutations of prior receipts.

## Compatibility and shared fixtures

Readers tolerate additive unknown fields, reject missing required keys/unsupported schemas, and preserve unrecognized metadata when round-tripping. Breaking type/semantic changes require a new version. Private producers remain optional.

Both implementations must validate the same fictional fixture set: partial snapshot, two distinct unresolved same-title topics, their later verified resolution with correctly associated messages and no duplicate additions, new reply, silent edit, requirements change, draft with one small attachment, staged receipt and human-edited submission receipt. Live community content and private evidence never enter public fixtures.
