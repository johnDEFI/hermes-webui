# Session switch paths

Contract Routing: session metadata, compression lineage, per-client profile
cookies, gateway watchers, and sidebar navigation. See canonical-session-resolution
and webui-run-state-consistency-contract RFCs.

A requested SID can use the shared read-only state.db projection restricted to
its connected lineage. Source normalization, visibility, sidecar metadata, and
compression selection remain owned by the existing projection. Missing results
and background sources fall back to the list resolver. Non-Claude SID lookups
exclude the global Claude Code scan. Root aliases use the list resolver's home
identity order: active home first, then the default root home.

Import refreshes can reuse a previously read sidecar while its file identity and
the DB/WAL fingerprint match, for at most 30 seconds. Capture the fingerprint
before reading and revalidate after saving. Missing or unreadable fingerprints
force a reread. The cache is bounded to 256 entries and does not alter the JSON
response, source permissions, prefix guard, or tool metadata enrichment rules.

POST /api/profile/switch accepts an optional boolean `lightweight: true`. Only
that explicit value omits `profiles`; all remaining defaults and the cookie
contract are unchanged. Conversation navigation uses this option. A live watcher
for the same home and config file identity is reused; explicit restart calls
retain their default replacement behavior.

Skill stats preserve the #4783 freshness contract: a stat-only recursive mtime
probe runs on every call, so out-of-band CLI/git changes are detected immediately.
The independent count-cache TTL remains 300 seconds for mtime-preserving changes.
SKILL.md reads stop at 4,000 characters. Lightweight conversation profile switches
skip profile enumeration and skill stats entirely.

Sidebar navigation shares a current (profile, SID) open and import request.
Superseded clicks cannot start a later transcript load. Profile targets serialize;
only the queue tail shares, preserving A/B/A click intent. The existing load and
list generations, failure restoration, and embargo cleanup remain authoritative.
For existing sidebar conversations, list/panel refresh starts after loadSession's
initial transcript render. Loading feedback uses existing row classes and the
mobile sidebar still closes only after the extension veto hook passes.

Verification without starting a server or installing dependencies:

```
python tests/test_switch_paths_perf.py -v
node tests/switch_paths_client.cjs
node --check static/sessions.js
node --check static/panels.js
```

These source-harness tests use real SQLite readers and extracted production
functions with explicit dependency doubles; they do not prove full HTTP,
upstream skill parsing, or desktop/mobile browser behavior. The integration
pytest fixture starts a server, so do not use it when server startup is forbidden.
The local-only `scripts/bench_switch_paths.py` uses temporary synthetic data and
HEAD functions for comparison. It bypasses the outer list cache and mocks profile
enumeration/frontmatter dependencies; its timings are not operating-server latency.
