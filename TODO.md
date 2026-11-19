# Development tasks

Updated 19 November 2026. Requirements below are planned work unless checked. Preserve the existing promotion product and UI.

Long term: support promotion in owner-designated self-promotion forums as well as text channels. Immediate priority: make the same generic reader/mirror/composer logic usable for a separately configured project-testing workflow (`purpose`: `promotion` or `external_project`) while keeping private domain data/templates separate.

## P0 — Demonstrate the immediate configured workflow

- [ ] Demonstrate the external producer's automatic context/evidence path: captured input -> destination resolution -> retained reference/context lookup -> selected screenshot/clip or published media link -> draft bundle -> actual composer. Human work is final review/edit/Send; repeated manual transcription, reference lookup and copying are not acceptance substitutes. Capture recognition/reference acquisition/media publication remain owned by the external application.
- [x] Add `refresh --thread ID --export` for one already mirrored destination plus guidance, without a directory scan; preserve unrelated coverage/work and pause during human review. Reject wrong-thread/forum history instead of associating it with the requested destination. Automated producers can prioritize the recognized destination using this generic path.
- [ ] Verify the actual configured deployment before the first genuine report: refresh the selected existing topic and guidance into the persistent mirror, publish to the configured exchange directory, import through the external consumer, and verify selected evidence is available at the receiver (configured read-only topic + guidance refresh, durable snapshot/delta export, strict external consumer import, live text-only bundle staging receipt, `user_confirmed_submitted` receipt, and live post-send mirror delta import verified cleanly on the active profile; live selected-media bundle staging stays deferred under the text-only reporting policy until later authorization). Temporary integration runs do not establish operational setup or remote freshness. Limit the first report to genuine observations; leave final editing and Send to the person.
- [x] Run a real end-to-end producer submission flow through user Send and `reconcile_submission` (verified end-to-end on the configured external-project profile with a genuine text-only report: live targeted refresh/export + strict external consumer import -> live non-sending composer staging with verified body hash receipt -> human Send + genuine `user_confirmed_submitted` receipt imported cleanly with review pause released -> live post-send read-only refresh capturing the submitted message with Snowflake `remote_created_at` and importing the incremental delta into the external consumer; offline `FakeForumProvider` tests cover auto-observed `submission_observed` upgrades and multi-file evidence paste; live auto-observed `submission_observed` `message_id` capture at Send time and live image-attachment filename OCR remain deferred while reporting policy is text-only).
- [x] Keep the project-testing profile opt-in and independent of promotion enablement; preserve existing product defaults (`purpose`: `external_project` forbids `promotion_enabled=True`).

## P0 — GET forum state and keep a local mirror

- [x] Implement a configured-forum reader against the installed Discord view (`src/forums/providers/desktop.py` `DesktopDiscordProvider`: macOS `AXUIElement` semantic text first with `AXDOMIdentifier`/`AXWebArea` extraction, plus `_safe_grab` + structured OCR fallback); verified semantic AX reads live on installed Discord and full flow in unit tests.
- [x] Resolve observed actor and canonical guild/forum/thread/message references (`<ACTOR_OWNER_01>`, canonical `discord.com/channels/...` links, `AXDOMIdentifier` thread/message IDs). Keep uncertain/unresolved identities explicit (`unresolved_topics` with unique `observation_ref`).
- [x] Read forum guidelines/guidance posts, topic directory cards/tags, accessible histories, and canonical links beyond the visible directory (`describe_forum`, `list_topics`, `read_topic`, `read_requirements`).
- [x] Store queryable SQLite entities (`src/forums/store.py` `ForumStore`), immutable revisions/source evidence, content hashes, and resumable traversal/coverage checkpoints under ignored runtime data (`data/forums/`).
- [x] Expose local topic/status/search/message-history queries and JSON exports (`src/forums/__main__.py` `status`, `topics`, `search`, `changes`, `refresh --export`).
- [x] Refresh requirements/starters and recent activity; rotate deep history rereads (`ForumReader.incremental_refresh` + `pick_topics_for_deep_reread`) to detect edits despite unchanged counts/activity.
- [x] Emit deduplicated topic/message/requirement/tag/attachment changes with before/after revisions and an opaque consumer cursor (`src/forums/changes.py`).
- [x] Handle interrupted/virtualized views without inventing deletions, completion or freshness. Distinguish OCR corrections (`recognition_corrected`) from remote edits (`edited`).
- [ ] Verify all mutation cases on a live remote forum (live initial read, guidance read, starter+reply read on a deeply-read topic, live remote new-reply capture with Snowflake `remote_created_at`, repeated-read `quiet_noop` with zero heading-level churn, live filtered-directory fail-closed guard `directory_filtered`, live two-page `list_topics` `dirscroll:` pagination/resume with disjoint results, and composer inspection verified on Discord; live remote old-message edit and third-party changed guidance text edit remain covered by `FakeForumProvider` tests and await live remote changes).
- [x] Run live Discord desktop verification for post-`7e23d03` fixes (`81ef8fa`): verified live split-pane forum cards (`6` cards, `0` leaked cross-channel/sidebar `AXLink`s), `AXList` reaction-only `message-accessories-*` extraction (`14` guidance messages with verified-empty `[]` attachments alongside isolated `None` vs `[]` store preservation), contract placeholder actor (`"Selected account"`) matching, `cmd_sync --stage-pending` human-draft preservation (`existing_human_draft_present`), `release-review`, non-sending live retry into `status="staged"` with multiline Unicode body + staged attachment slot (`map-note.txt`) consumed cleanly by `<EXT_EXCHANGE_01>.status()`, and clean composer teardown (`clear_staged_composer()`).

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
- [x] Support shared-topic replies, one-topic-per-streamer updates and new-topic drafts across both backend and UI (`src/forums/drafts.py::render_forum_promo_draft` resolves `one_topic_per_streamer` from `ForumStore`, supports `create_topic` fallback and `edit_own_message`, and auto-populates baselines; `src/forums/dialog.py::ForumMirrorController` & `ForumMirrorDialog` support routing/profile editing, single-topic refresh + exchange export, `<digest>/` bundle loading, promo draft building, and separate observe vs confirm-submitted actions).
- [x] Reuse stream title/game/link generically in `render_forum_promo_draft` without private protocols/data.
- [x] Key promotion cadence/cooldowns to canonical forum targets (`forum:<guild_id>:<target_id>`) and observed/confirmed submission.

## P0 — Stable parallel-development boundary

- [x] Document exact `forum.snapshot.v1`, `forum.changes.v1`, `draft.request.v1` and `handoff.receipt.v1` keys/types in [the shared contract](docs/FORUM_EXCHANGE_V1.md).
- [x] Implement validation for that field contract (`src/forums/exchange.py`) and keep producers/consumers aligned.
- [x] Add fictional round-trip examples (`tests/fixtures/forum_exchange_v1/`), atomic file exchange (`write_record_atomic`), stable IDs/revisions/cursors and `transfer_root`-bounded evidence SHA-256 verification without cross-repo Python imports or credentials.
- [x] Export reads/changes independently of publishing; accept externally prepared drafts and return staging/submission observations.
- [x] Verify an independent producer (<EXT_APP_01>) can consume a live exported snapshot/change feed and return exchange contracts without private code in the public app (verified unmodified `<EXT_STORE_01>` + `<EXT_INBOX_01>` ingesting both synthetic full lifecycle—bootstrap, `observation_ref` resolution, deltas with continuous `after_cursor` & anchored `snapshot_revision`, quiet refresh, cutover archive—and live Discord exported bootstrap snapshot of `50` topics / `23` messages; real user-initiated Send + post-send `reconcile_submission` remains non-manufactured).
- [x] Complete <EXT_GATE_01> alignment & post-review hardening against unmodified `<EXT_APP_01>` on `<EXT_BRANCH_01>` (`<EXT_REV_01>`): scoped mirror/draft baseline checks, full `forum.changes.v1` `after` validation, isolated per-profile DB defaults & store scoping, `catalog_enabled` & target/actor/`reply_existing` fail-closed guards, partial metadata/requirement preservation & terminal pagination fixes, portable `<digest>/` bundle intake, atomic `in_discord_review` transition, microsecond UTC timestamp alignment + `ts_sort_key` SQLite chronological sorting, unverified (`None`) vs verified-empty (`[]`) message attachment preservation, stricter post-card `AXLink` scoping, `sync --stage-pending` transient `needs_review` retry, legacy active-server promo filtering, and OCR inverse box transform (`44` offline pytest / `30` `test_forums` unit cases / `6` cross-repo fictional scenarios passing at that checkpoint; subsequent live desktop verification recorded at `81ef8fa`).
- [x] Implement durable two-phase exchange publication & crash/restart recovery (`e53b935de6e3bc388b79ccf396a697386e959a37`, `src/forums/store.py`, `src/forums/exchange.py`): persistent SQLite `export_outbox`, `.tmp` cleanup on write/rename failure, deferred delta archiving on cutover until after new snapshot file exists on disk, deferred `mirror_meta` checkpoint commit until after durable publication, and deterministic replay of identical `record_id`/canonical payload across restarts without dropping pending deltas on quiet retry (`47` offline pytest / `33` `test_forums` unit cases / external unmodified `<EXT_INBOX_01>` + `<EXT_STORE_01>` recovery scenarios, `<EXT_PROBE_01>` probe, and `6` <EXT_GATE_01> alignment scenarios passing at `<EXT_REV_02>`).

## P1 — Focused existing correctness fixes

- [x] Preserve/test the tracked `_safe_grab` utility-to-PIL fallback and the existing sidebar OCR lookup while adding the forum adapter. Left pre-existing uncommitted channel-scanner work untouched outside this PR.
- [x] Restore module globals mocked by `tests/test_nav_logic.py`; verify OCR tests independently and in the full suite (33 tests OK).
- [x] Add structured coverage (`build_server_scan_coverage` and `ForumStore` `coverage`) and stop/error reasons around traversal.
- [x] Fix the legacy live CLI queue (`src/promotion_helper.py::queue_updates(..., servers=servers)` and `src/main.py`) ignoring its computed filtered targets.

## P2 — Measured public OCR improvements

- [x] Structured OCR output (`OCRTokenBox`, `OCRResult`, `ocr_image_structured`) preserving raw text, lines, boxes, confidence and task/settings; existing `ocr_image_to_text` wrapper preserved.
- [x] Task-specific cleanup/preprocessing, settled-view helper (`_capture_settled_region`), and bounded content/task crop cache (`_OCR_CROP_CACHE`).
- [x] Synthetic fixtures and accuracy/latency measurements across multiline forum content, Unicode, links, one-word names, dark/light themes, and scaling (`src/utils.py::benchmark_synthetic_forum_ocr` benchmarked 8 synthetic Tesseract cases covering multiline, accented Unicode, canonical `discord.com/channels/...` links, one-word forum title, dark/light themes, and `1.0x`/`2.0x` scaling with `0.9934` mean char similarity, `1.0` mean token recall, `0.9382` mean confidence, `191.39ms` cold vs `0.2359ms` cached latency / `811.3x` cache speedup).
- [x] Implement generic mechanisms without moving private <EXT_APP_01> source, game tuning, models, or datasets into this repository.

## Parallel ownership and starting point

This repository owns the generic Discord reader/mirror/change feed, composer staging and public interchange. External/private applications own capture, domain-specific interpretation, reference-data caches, specialized templates and private community configuration.

Private integration work can start against the four record contracts above using fixtures while this repository builds the reader. Do not make gameplay capture or reference archiving wait for the public UI. Resolve contract changes additively and keep actual runtime forum content out of public commits.

The first end-to-end milestone is **read one topic and its requirements -> query local state -> detect a change -> stage an evidence-backed update in that topic -> person edits and presses Send**. Then broaden coverage, not the product's visual direction.

See [forum design](docs/FORUM_SUPPORT_PLAN.md) for the data model, refresh algorithm, contracts, layout and acceptance criteria.
