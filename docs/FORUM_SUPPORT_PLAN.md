# Forum retrieval, local mirror and reviewed posting

Design updated 14 November 2026. This document specifies work to implement; it does not describe capabilities already shipped.

## Goal and priorities

Preserve Discord Promo Helper's established promotion purpose and UI, and support communities whose promotion areas are forums. The generic forum module must also serve external tools that need a usable local representation of a community project.

The priorities are:

1. **GET current forum state from the logged-in Discord view**: topics, messages, starter summaries, guidelines, requirements, tags and available state. Initial retrieval and subsequent refresh are core features, not a manual-paste-only import workflow.
2. **Keep a queryable local mirror with revisions and changes** so a person or another tool can see what exists, what changed, who contributed, what needs checking and how recently it was read.
3. **Prepare the update in the actual correct Discord composer**, including selected attachments, then let the person review, edit and press Send. Opening a link and asking the person to copy many separate items is not the intended endpoint.

Use the existing logged-in session and its actual capabilities. No bot installation is assumed. Respect each community's rules and promotion cadence. Reading a forum never enables promotion into it. This extension must not replace the current UI or change the default promotion workflow.

## Public/private ownership

| Public helper | External/private application |
| --- | --- |
| Generic Discord view adapter and forum traversal | Capture hardware, OBS setup and recordings |
| Local topics/messages/guidelines mirror and change feed | Game-specific OCR regions, tuning, dictionaries and models |
| Search, status, freshness and revision comparisons | Domain entity matching and play-test interpretation |
| Generic promotion templates and draft editor | Community-specific protocols and report generation |
| Navigate to target, stage body/title/tags/attachments, stop before Send | Reference-source acquisition, permanent caches and expected-versus-observed comparisons |
| Neutral snapshot/change/draft/handoff contracts | Private data, deployment configuration and automation |
| Generic OCR improvements with synthetic fixtures | Private annotated images, benchmark corpora and vision pipelines |

Private producers must not become dependencies of this repository. Implement selected generally useful OCR ideas here in small independent changes; do not transplant private source, datasets, profiles or tuning. Public examples use fictional communities and synthetic data. Runtime community content and per-user configuration remain under ignored data paths.

## Existing code and reuse

- `src/ui.py`: keep the Tkinter server-settings interface; add an optional forum/status window and draft-review flow using its existing conventions.
- `src/stream_info.py`: reuse stream metadata for public promotion drafts.
- `src/server_config.py`: retain existing settings; add optional canonical forum destinations and separate catalog/promotion/external-draft flags with backward-compatible defaults.
- `src/channel_nav.py`: current channel scan returns an empty list; single-server scan is a stub. Its title OCR calls a missing `utils._safe_grab`. It is not a forum reader yet.
- `src/discord_nav.py`: reuse useful window/navigation helpers, not broad all-server clicking as a substitute for a configured forum target. Callback errors currently do not establish successful per-server completion.
- `src/promotion_helper.py`: fixed promotional wording and default Enter behavior must not be inherited by the reviewed forum staging path.
- `src/main.py`: filtered CLI targets currently are not passed into the legacy posting queue. Keep the forum module separate; handle that issue in a focused promotion fix.
- `src/utils.py`: already has tooltip cropping, scaling, polarity, contrast/threshold variants, padding, PSM fallbacks and confidence ranking. Build on these rather than re-adding them.
- `tests/test_nav_logic.py`: restore replaced globals after tests; current mock leakage affects full-suite OCR results.

## Runtime adapter and configured scope

Implement a provider interface around the Discord view, independent of the storage and domain layers:

```text
describe_session() -> actor, app/window, available read/stage capabilities
describe_forum(target) -> guidelines, tags, visible permissions, source references
list_topics(target, cursor) -> observations, next cursor, coverage
read_topic(thread, cursor) -> topic, message observations, next cursor, coverage
read_requirements(references) -> versioned guideline/pinned/starter observations
stage_draft(request) -> observed composer state, staged files, handoff status
observe_submission(handoff) -> message evidence or unresolved status
```

Prefer exposed semantic/accessibility text and canonical links. Use screenshots/OCR for regions that do not expose useful text. The source inspection demonstrated readable Discord accessibility content, not a packaged Python adapter: the first implementation spike must establish which platform adapter works in the intended installed runtime. Development-only computer-use tools are not an application dependency.

Select a named forum/profile, not every server on the account. Configuration includes canonical IDs/links where observed, requirements/pinned references, read schedule and topic scope. Keep `catalog_enabled`, `promotion_enabled`, `external_drafts_enabled` and `stage_enabled` distinct. A research catalogue must never inherit promotional text or automatic inclusion in the old queue.

Validate navigation against the observed actor and target. Canonical links/known mappings establish identity; OCR names and icon similarity only suggest candidates. If a link does not expose a parent relationship, retain that relationship as unverified until observed. IDs are strings, never floating-point numbers.

One controller owns a Discord UI session. Coordinate read navigation with staging and human review. Refresh may run while the session is available, but must pause before it disturbs user activity, an existing unsent composer, a staged update or an account switch. OBS acquisition remains independent and continues while Discord reading is paused.

## Local representation

Use a dedicated SQLite store under `data/forums/`, plus JSON exports. Do not share the promotion timestamp file or an external application's database.

Store:

- **Forums:** IDs, display name, guidelines, available tags and source link.
- **Topics:** thread/parent IDs, title, tags, starter reference, available archive/lock state and observed activity.
- **Messages:** stable ID/link when exposed, author, available remote creation/edit time, body, attachment references and source evidence.
- **Requirements:** designated guidelines/pinned/template documents with full source text, parsed fields if configured and revision history.
- **Revisions:** immutable prior normalized content, content hash, raw observation reference, recognition metadata, `remote_edited_at` when known and `observed_at`/`captured_at` independently.
- **Changes:** stable change ID and kind, entity reference, before/after revision, observed time and recognition/source confidence.
- **Coverage/checkpoints:** actor, source view, traversal scope, pages/cursors, oldest/newest observed messages, unfinished ranges, errors and last successful refresh.
- **Handoffs:** draft/revision, intended target/actor, staged body/files, current status and eventual message evidence if available.

Change kinds include topic/message added, content edited, requirements changed, tag/state changed, attachment changed and explicitly observed removal. Missing data from a virtualized or partially loaded view is not deletion. Retain prior content and record uncertainty/last seen. Do not equate a successful screenshot with successful full traversal.

Prefer stable remote message IDs. When unavailable, use an explicitly provisional local observation identity and reconcile carefully; author plus timestamp is not a unique message ID. Distinguish an OCR correction from a remote content edit. Preserve raw source evidence for ambiguous differences rather than generating false activity alerts.

Expose local search and per-topic summaries: current imported starter/status, recent contributors, latest findings, open questions, requirements revision, relevant changes since a cursor/session and freshness. Generic summaries must retain source links; domain-specific conclusions belong to the private consumer. A report saying an issue exists is not automatically the organiser's accepted status.

## Retrieval and change detection

### Initial backfill

1. Resolve configured forum and observed account. Read guidelines, pinned guidance, tags and linked topic directory if present.
2. Enumerate topics through the actual view, including available older/archived views. Follow the directory as a seed; also discover unlisted/new topics rather than assuming the directory is complete.
3. Read each topic starter and its accessible message history, paging/scrolling with overlap and stable references. Store checkpoints so interruption resumes without starting over.
4. Commit observations/revisions and a coverage manifest. Export a local snapshot even if some history remains unread, with explicit remaining work.

### Incremental refresh

1. Refresh requirements/pinned guidance and starter summaries at high priority.
2. Discover new topics and revisit recently active topics/messages.
3. Independently rotate deeper message-history rereads, even when counts/activity are unchanged. Editing old text may not add a message or move the topic.
4. Compare stable entities and normalized content, preserve revisions and append deduplicated changes. Requirement changes flag dependent drafts/findings for recheck.
5. Export changes since the consumer cursor and a refreshed snapshot/checkpoint. A quiet forum produces no duplicate changes.

Use configurable bounded work per refresh, pauses, cancellation, retry/backoff and visible progress. Do not repeatedly rescan the entire forum at high frequency. The near-real-time aim is quick detection/preparation when the view is available, with explicit freshness when it is busy or unread. Prove actual coverage and latency on the target view before presenting them as guaranteed.

## Direct composer staging and final human Send

1. Receive a proposed reply, own-message edit or new-topic draft with canonical destination, exact text/title, tag suggestions, evidence references and baseline snapshot revision.
2. Check current requirements/target freshness and conflicting changes. If new requirements affect the draft, show the difference before staging.
3. Acquire the session controller, verify signed-in actor/target and preserve any existing human draft. Never overwrite a nonempty composer automatically.
4. Navigate to the exact topic or relevant create/edit form, insert the complete multiline body and select/upload the intended evidence in one operation. Apply only available permitted tags. Verify the visible target, text and pending attachment previews; a failed upload must not be reported as staged.
5. Leave Discord ready for the person to make final edits and press Send/save. Do not synthesize the final send action or auto-save an edit. Pause navigation while the person reviews.
6. After the person submits, observe a corresponding posted message/link when the view allows, reconcile human edits, and record the receipt. Otherwise retain unresolved/user-confirmed status; do not invent success or stage a duplicate.

Support communities with one permanent topic per streamer, a shared promotion thread or a topic per stream. New-topic creation must not be the default where an existing target is configured. Respect per-community cadence; cooldowns use canonical destinations and confirmed/observed submissions, not copying or staging. Research updates are not promotional queue items.

`Copy draft` remains an optional fallback, not the definition of success. The primary acceptance target is a populated composer in the correct forum/topic with the intended evidence, ready for final human review.

## Cross-project contracts

Start with versioned JSON files and atomic exports; add a CLI over the same interfaces. External producers need no private imports into this application and exchange no credentials. The [shared v1 field contract](FORUM_EXCHANGE_V1.md) fixes exact keys, types, cursors, baselines and evidence/receipt identities for parallel work.

| Record | Required meaning |
| --- | --- |
| `forum.snapshot.v1` | Snapshot ID/revision, capture time, observed actor, forum/topics/messages/requirements with source references and coverage/cursors |
| `forum.changes.v1` | Stable change IDs/cursor, affected entity, change kind, before/after revisions, observed time and source/recognition confidence |
| `draft.request.v1` | Stable draft ID/revision, canonical target, action, exact body/title, optional tags/evidence, baseline snapshot/requirements revision |
| `handoff.receipt.v1` | Draft/revision, actual target/actor, status (`staged`, `in_discord_review`, `submission_observed`, `user_confirmed_submitted`, `needs_review`, `failed`), optional message link and observations |

Imports are data, never commands. Validate URLs/IDs, keep attachments within configured handoff roots, check existence/hashes and select only intended evidence. Preserve private runtime content outside version control. Stage the named revision; new findings must not silently replace the person's reviewed edits.

Use idempotent draft IDs/revisions and receipt IDs. If submission is ambiguous, reconcile before suggesting another send. Cross-machine payloads use evidence IDs/checksums and a configured resolver/transfer, not the assumption that a Windows file path is readable on a Mac. Freeze these contract names/semantics before parallel implementation; extensions should remain additive.

## OCR work

Add a structured result with raw text, preserved lines/token boxes, confidence, crop dimensions, engine/settings and source observation. Keep the old string-returning wrapper for compatibility.

Use task-specific processing (`server_tooltip`, `channel_name`, `forum_title`, `message_body`), relative ROIs, settled-view capture, a cheap first pass and bounded low-confidence fallback. Current tooltip cleanup removes words/punctuation and flattens lines; do not apply it unchanged to forum text. Cache identical crops by image content, task, language and settings. Valid one-word titles remain valid.

Measure quality and latency on synthetic neutral fixtures before adding engines. Private vision pipelines, game profiles and benchmark images stay private. OCR is a reading fallback, not the authority for target identity or permissions.

## Proposed source layout

```text
src/forums/
  models.py             # entities, revisions, coverage and contract types
  config.py             # explicit forum profiles and independent flags
  providers/base.py     # read/stage interface
  providers/desktop.py  # installed-runtime view adapter; platform spike first
  reader.py             # initial traversal and incremental refresh
  store.py              # SQLite revisions/checkpoints/change log
  changes.py            # reconciliation and deduplicated changes
  drafts.py             # generic rendering, revisions and conflicts
  handoff.py            # session lock, composer staging, no final Send
  exchange.py           # versioned snapshot/change/draft/receipt files
  dialog.py             # optional window consistent with current UI
  __main__.py           # refresh/status/search/changes/stage CLI
```

This is a proposed layout, not created code. Generic fixture/schema files accompany it; private community profiles and data do not.

## Implementation sequence and proof

1. **Read/store vertical slice:** one configured forum, its guidance and one topic history into SQLite/JSON; local query and repeat refresh detect a new reply and an edit. Establish installed-runtime AX/OCR adapter, canonical identity and checkpoint behavior first.
2. **Full catalogue and status:** complete accessible topic/history traversal with resumable coverage, requirements refresh and rotating deep rereads. Demonstrate local status/change queries without opening Discord manually for every topic.
3. **Composer staging:** one request stages multiline text and evidence in the correct existing topic, leaving the person to edit and Send. Verify the later message/receipt without duplicate staging.
4. **Generic promotion integration and exchange:** optional forum UI, existing-topic routing, stream templates, independent flags/cooldowns and cross-project contracts. Preserve legacy settings/defaults.
5. **Targeted OCR and correctness work:** fix the missing screenshot helper and test isolation, then measure structured/task-specific OCR. Do not replace polished UI or transplant private code.

Acceptance scenarios:

- A new topic/reply is recorded once with available author/time/link; a quiet refresh adds no duplicates.
- An old message edit is detected even when message count and topic activity appear unchanged.
- Edited guidance produces a requirement change and marks dependent drafts for recheck.
- Interrupted/partially loaded traversal resumes without false deletions or false completion.
- OCR-only text correction is not confidently announced as a remote edit without evidence.
- Account switch or a pre-existing human draft pauses staging without content loss.
- The correct composer contains exact multiline Unicode text and selected uploaded evidence; no final Send occurs.
- Human-edited submitted content is reconciled, not overwritten by an old draft revision.
- Importing a research forum never enables promotions. Existing settings/default launch still work.
- Public tests/build succeed without private repositories, profiles, screenshots or datasets.

Keep the task list in [TODO.md](../TODO.md). Implementation status and test results belong in project notes, not customer-facing copy.
