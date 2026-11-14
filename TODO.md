# Development tasks

Updated 14 November 2026. Requirements below are planned work unless checked. Preserve the existing promotion product and UI.

## P0 — GET forum state and keep a local mirror

- [ ] Implement a configured-forum reader against the installed Discord view; semantic/accessibility text first, OCR fallback. Verify the adapter in the shipped Python/runtime, not just developer tools.
- [ ] Resolve observed actor and canonical guild/forum/thread/message references. Keep uncertain identities explicit.
- [ ] Read forum guidelines, pinned requirements, topic directory, starter summaries, tags and accessible histories; discover new/unlisted topics beyond the directory.
- [ ] Store queryable SQLite entities, immutable revisions/source evidence, content hashes and resumable traversal/coverage checkpoints under ignored runtime data.
- [ ] Expose local topic/status/search/message-history queries and JSON exports for other tools.
- [ ] Refresh requirements/starters and recent activity; rotate deep history rereads to detect edits despite unchanged counts/activity.
- [ ] Emit deduplicated topic/message/requirement/tag/attachment changes with before/after revisions and a consumer cursor.
- [ ] Handle interrupted/virtualized views without inventing deletions, completion or freshness. Distinguish OCR corrections from remote edits.
- [ ] Verify on a real configured forum: initial read, new reply, old-message edit, changed guidance, resume and quiet refresh.

## P0 — Stage the complete update for final review and Send

- [ ] Navigate to the exact existing topic/create/edit form from canonical target references.
- [ ] Insert full body/title and selected attachments into the actual Discord composer; verify text/target/upload previews. Copy/paste-by-the-user is a fallback, not the primary workflow.
- [ ] Leave final edits and Send/save to the person. Never reuse a path that automatically presses Enter.
- [ ] Serialize Discord UI navigation; pause refresh during staging/review, preserve existing human drafts and pause on account/target changes.
- [ ] Recheck changed requirements/baseline before staging; preserve user edits when new evidence arrives.
- [ ] Observe/reconcile submission and human edits where possible; retain unresolved status when not confirmed; avoid duplicate staging.

## P1 — Generic forum promotion support

- [ ] Add optional forum/topic destinations to existing settings without replacing the Tkinter UI or changing defaults.
- [ ] Keep catalogue, promotion, external-draft and staging enablement separate. Imported discussion/research forums are not promotion targets.
- [ ] Support shared-topic replies, one-topic-per-streamer updates and new-topic drafts according to each community's rules.
- [ ] Reuse stream title/game/link in generic templates; keep specialized private protocols out of public examples.
- [ ] Key promotion cadence/cooldowns to canonical targets and confirmed/observed submission.

## P0 — Stable parallel-development boundary

- [x] Document exact `forum.snapshot.v1`, `forum.changes.v1`, `draft.request.v1` and `handoff.receipt.v1` keys/types in [the shared contract](docs/FORUM_EXCHANGE_V1.md).
- [ ] Implement validation for that field contract and keep both producers/consumers aligned.
- [ ] Add fictional round-trip examples, atomic file exchange, stable IDs/revisions/cursors and evidence resolution without cross-repo Python imports or credentials.
- [ ] Export reads/changes independently of publishing; accept externally prepared drafts and return staging/submission observations.
- [ ] Verify an independent producer can consume a snapshot/change feed and stage one draft without private code in the public app.

## P1 — Focused existing correctness fixes

- [ ] Fix `channel_nav`'s missing screenshot helper reference and test the actual helper boundary rather than mocking away its existence.
- [ ] Restore module globals mocked by `tests/test_nav_logic.py`; verify OCR tests independently and in the full suite.
- [ ] Report partial channel/callback scan failures accurately rather than always declaring completion.
- [ ] Fix the legacy live CLI queue ignoring its computed filtered targets in a separate scoped change.

## P2 — Measured public OCR improvements

- [ ] Structured OCR output preserving raw text, lines, boxes, confidence and source/settings; compatibility wrapper for existing callers.
- [ ] Purpose-specific cleanup/ROIs, bounded fallback, settled-view handling and content/config crop caching.
- [ ] Synthetic fixtures for multiline forum content, Unicode, links, one-word names, dark/light themes and scaling; measure accuracy and latency.
- [ ] Reimplement selected generic ideas without moving private source, game tuning, models or datasets into this repository.

## Parallel ownership and starting point

This repository owns the generic Discord reader/mirror/change feed, composer staging and public interchange. External/private applications own capture, domain-specific interpretation, reference-data caches, specialized templates and private community configuration.

Private integration work can start against the four record contracts above using fixtures while this repository builds the reader. Do not make gameplay capture or reference archiving wait for the public UI. Resolve contract changes additively and keep actual runtime forum content out of public commits.

The first end-to-end milestone is **read one topic and its requirements -> query local state -> detect a change -> stage an evidence-backed update in that topic -> person edits and presses Send**. Then broaden coverage, not the product's visual direction.

See [forum design](docs/FORUM_SUPPORT_PLAN.md) for the data model, refresh algorithm, contracts, layout and acceptance criteria.
