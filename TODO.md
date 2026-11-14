# Development tasks

Updated 14 November 2026. Requirements below are planned work unless checked. Preserve the existing promotion product and UI.

Long term: support promotion in owner-designated self-promotion forums as well as text channels. Immediate priority: make the same generic reader/mirror/composer logic usable for a separately configured project-testing workflow (`purpose`: `promotion` or `external_project`) while keeping private domain data/templates separate.

## P0 — Demonstrate the immediate configured workflow

- [ ] Run a real end-to-end producer submission flow through user Send and `reconcile_submission` (live paginated read -> SQLite mirror -> `export_exchange_dir` -> unmodified `<EXT_STORE_01>` ingestion -> live non-sending staging of multiline Unicode + `map-note.txt` via POSIX file paste -> cross-process `refresh` pause -> clean composer teardown all verified at `9f67d5e`/`df2acc4`; <EXT_GATE_01> alignment Mac cross-repo suite at `ddaaea3` verified all 6 fictional bundle/receipt/reconciliation acceptance scenarios against unmodified `<EXT_COORDINATOR_01>`, `<EXT_LEDGER_01>`, and `<EXT_RECEIPT_INBOX_01>`; awaiting a real user-authored Discord Send rather than manufacturing community test posts, plus Screen Recording permission if live OCR / filename verification is needed).
- [x] Keep the project-testing profile opt-in and independent of promotion enablement; preserve existing product defaults (`purpose`: `external_project` forbids `promotion_enabled=True`).

## P0 — GET forum state and keep a local mirror

- [x] Implement a configured-forum reader against the installed Discord view (`src/forums/providers/desktop.py` `DesktopDiscordProvider`: macOS `AXUIElement` semantic text first with `AXDOMIdentifier`/`AXWebArea` extraction, plus `_safe_grab` + structured OCR fallback); verified semantic AX reads live on installed Discord and full flow in unit tests.
- [x] Resolve observed actor and canonical guild/forum/thread/message references (`<ACTOR_OWNER_01>`, canonical `discord.com/channels/...` links, `AXDOMIdentifier` thread/message IDs). Keep uncertain/unresolved identities explicit (`unresolved_topics` with unique `observation_ref`).
- [x] Read forum guidelines/guidance posts, topic directory cards/tags, accessible histories, and canonical links beyond the visible directory (`describe_forum`, `list_topics`, `read_topic`, `read_requirements`).
- [x] Store queryable SQLite entities (`src/forums/store.py` `ForumStore`), immutable revisions/source evidence, content hashes, and resumable traversal/coverage checkpoints under ignored runtime data (`data/forums/`).
- [x] Expose local topic/status/search/message-history queries and JSON exports (`src/forums/__main__.py` `status`, `search`, `changes`, `refresh --export`).
- [x] Refresh requirements/starters and recent activity; rotate deep history rereads (`ForumReader.incremental_refresh` + `pick_topics_for_deep_reread`) to detect edits despite unchanged counts/activity.
- [x] Emit deduplicated topic/message/requirement/tag/attachment changes with before/after revisions and an opaque consumer cursor (`src/forums/changes.py`).
- [x] Handle interrupted/virtualized views without inventing deletions, completion or freshness. Distinguish OCR corrections (`recognition_corrected`) from remote edits (`edited`).
- [ ] Verify all mutation cases on a live remote forum (live initial read, guidance read, starter+reply read on `<THREAD_TOPIC_01>`, quiet second read, and composer inspection verified on Discord; live remote new-reply, old-message edit, changed guidance, and live multi-page resume covered by `FakeForumProvider` tests and await live remote changes).
- [ ] Run live Discord desktop verification for post-`7e23d03` fixes (deferred because Discord is currently in active use: live AX `message-accessories` attachment extraction vs `None`/`[]` preservation, live post-card inline cross-channel link filtering in `_extract_post_cards_and_links`, and live `sync --stage-pending` retry after `release-review`).

## P0 — Stage the complete update for final review and Send

- [x] Navigate to the exact existing topic/create/edit target from canonical target references (`DesktopDiscordProvider.navigate_to_url` + `_inspect_composer`, stripping Slate `\ufeff` empty-editor markers).
- [x] Insert full body/title and selected attachments into the actual Discord composer and verify upload previews (`DesktopDiscordProvider.stage_draft`: explicit composer focus, normalized multiline Unicode check, clipboard save/restore, macOS POSIX file paste + `"More message options"` -> `AXSheet` fallback, bounds-keyed `Remove Attachment` slot & filename verification, `clear_staged_composer()`; verified live on Discord without pressing Send).
- [x] Leave final edits and Send/save to the person. Never reuse a path that automatically presses Enter.
- [x] Serialize Discord UI navigation (`SessionController`); pause refresh during staging/review, preserve existing human drafts, and pause on account/target changes.
- [x] Recheck changed requirements/baseline before staging (`check_draft_conflicts`); preserve user edits when new draft revisions arrive during active review.
- [x] Observe/reconcile submission and human edits where possible (`HandoffManager.reconcile_submission`); retain unresolved status when not confirmed and prevent duplicate staging.

## P1 — Generic forum promotion support

- [x] Add optional forum/topic destinations to existing settings (`src/forums/config.py`, `src/server_config.py::get_forum_profiles` / `set_forum_profile`) without replacing the Tkinter UI or changing defaults.
- [x] Keep catalogue, promotion, external-draft and staging enablement separate. Imported/project forums forbid `promotion_enabled=True`.
- [ ] Support shared-topic replies, one-topic-per-streamer updates and new-topic drafts across both backend and UI (`reply_existing`, `one_topic_per_streamer`, and `create_topic` modeled in `ForumProfile`/`render_forum_promo_draft`; Tkinter UI controls pending).
- [x] Reuse stream title/game/link generically in `render_forum_promo_draft` without private protocols/data.
- [x] Key promotion cadence/cooldowns to canonical forum targets (`forum:<guild_id>:<target_id>`) and observed/confirmed submission.

## P0 — Stable parallel-development boundary

- [x] Document exact `forum.snapshot.v1`, `forum.changes.v1`, `draft.request.v1` and `handoff.receipt.v1` keys/types in [the shared contract](docs/FORUM_EXCHANGE_V1.md).
- [x] Implement validation for that field contract (`src/forums/exchange.py`) and keep producers/consumers aligned.
- [x] Add fictional round-trip examples (`tests/fixtures/forum_exchange_v1/`), atomic file exchange (`write_record_atomic`), stable IDs/revisions/cursors and `transfer_root`-bounded evidence SHA-256 verification without cross-repo Python imports or credentials.
- [x] Export reads/changes independently of publishing; accept externally prepared drafts and return staging/submission observations.
- [x] Verify an independent producer (<EXT_APP_01>) can consume a live exported snapshot/change feed and return exchange contracts without private code in the public app (verified unmodified `<EXT_STORE_01>` + `<EXT_INBOX_01>` ingesting both synthetic full lifecycle—bootstrap, `observation_ref` resolution, deltas with continuous `after_cursor` & anchored `snapshot_revision`, quiet refresh, cutover archive—and live Discord exported bootstrap snapshot of `50` topics / `23` messages; real user-initiated Send + post-send `reconcile_submission` remains non-manufactured).
- [x] Complete <EXT_GATE_01> alignment & post-review hardening against unmodified `<EXT_APP_01>` on `<EXT_BRANCH_01>` (`<EXT_REV_01>`): scoped mirror/draft baseline checks, full `forum.changes.v1` `after` validation, isolated per-profile DB defaults & store scoping, `catalog_enabled` & target/actor/`reply_existing` fail-closed guards, partial metadata/requirement preservation & terminal pagination fixes, portable `<digest>/` bundle intake, atomic `in_discord_review` transition, microsecond UTC timestamp alignment + `ts_sort_key` SQLite chronological sorting, unverified (`None`) vs verified-empty (`[]`) message attachment preservation, stricter post-card `AXLink` scoping, `sync --stage-pending` transient `needs_review` retry, legacy active-server promo filtering, and OCR inverse box transform (`44` offline pytest / `30` `test_forums` unit cases / `6` cross-repo fictional scenarios passing; live Discord UI checks for post-`7e23d03` fixes deferred).

## P1 — Focused existing correctness fixes

- [x] Preserve/test the tracked `_safe_grab` utility-to-PIL fallback and the existing sidebar OCR lookup while adding the forum adapter. Left pre-existing uncommitted channel-scanner work untouched outside this PR.
- [x] Restore module globals mocked by `tests/test_nav_logic.py`; verify OCR tests independently and in the full suite (33 tests OK).
- [x] Add structured coverage (`build_server_scan_coverage` and `ForumStore` `coverage`) and stop/error reasons around traversal.
- [x] Fix the legacy live CLI queue (`src/promotion_helper.py::queue_updates(..., servers=servers)` and `src/main.py`) ignoring its computed filtered targets.

## P2 — Measured public OCR improvements

- [x] Structured OCR output (`OCRTokenBox`, `OCRResult`, `ocr_image_structured`) preserving raw text, lines, boxes, confidence and task/settings; existing `ocr_image_to_text` wrapper preserved.
- [x] Task-specific cleanup/preprocessing, settled-view helper (`_capture_settled_region`), and bounded content/task crop cache (`_OCR_CROP_CACHE`).
- [ ] Synthetic fixtures and accuracy/latency measurements across multiline forum content, Unicode, links, one-word names, dark/light themes, and scaling (unit tests cover multiline, dark/light, one-word title, and cache hit; systematic Unicode/link/scaling benchmark metrics remain).
- [x] Implement generic mechanisms without moving private <EXT_APP_01> source, game tuning, models, or datasets into this repository.

## Parallel ownership and starting point

This repository owns the generic Discord reader/mirror/change feed, composer staging and public interchange. External/private applications own capture, domain-specific interpretation, reference-data caches, specialized templates and private community configuration.

Private integration work can start against the four record contracts above using fixtures while this repository builds the reader. Do not make gameplay capture or reference archiving wait for the public UI. Resolve contract changes additively and keep actual runtime forum content out of public commits.

The first end-to-end milestone is **read one topic and its requirements -> query local state -> detect a change -> stage an evidence-backed update in that topic -> person edits and presses Send**. Then broaden coverage, not the product's visual direction.

See [forum design](docs/FORUM_SUPPORT_PLAN.md) for the data model, refresh algorithm, contracts, layout and acceptance criteria.
