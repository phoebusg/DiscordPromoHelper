# Forum design review notes

## PR #2 — 14 November 2026

- Copilot topic-identity finding: accepted. Exchanged topics require verified non-null thread/forum IDs. Unresolved local observations have distinct stable keys, remain pending with partial coverage, and cannot supply exchanged messages or stage targets until resolved. Resolution preserves local mappings and emits a single addition; same-title topics never collapse to a null/title key. Added the two-unresolved-topics/resolution scenario to shared fixture requirements.
- Copilot source-baseline finding: accepted. Earlier prose described local uncommitted `channel_nav.py` and a callback addition absent from the PR. Corrected design/TODO to describe tracked sidebar OCR lookup, the existing utility-to-PIL screenshot fallback, and the iterator's list-returning/coverage limitations. Existing uncommitted runtime changes remain outside this documentation PR.
- Product direction: generic promotion support includes owner-designated self-promotion forums, not only text channels. Immediate project-testing use shares the logic through an opt-in profile/purpose and separate enable flags; it does not replace the promotion product or expose private domain code/data.
- Readiness: documentation reviewed; runtime forum retrieval, refresh/diff and actual composer staging remain unimplemented in this PR. The immediate-use gate requires a real vertical-slice run with recorded coverage and staging evidence, stopping before human Send. Do not infer readiness from the design or unrelated green tests.
