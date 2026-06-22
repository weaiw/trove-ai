# Changelog

All notable changes to Trove AI are documented here. This project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **MCP write tools (opt-in)** — the MCP server can now create/modify content, gated behind a per-user "allow write" switch (default **off**). When enabled it additionally exposes `add_article`, `add_note`, `update_article`, `set_article_tags`; read tools stay always-on. New `PUT /api/auth/mcp-write` toggle + a switch and live tool list in the settings "外部 AI 接入 (MCP)" card. Migration `012` (`users.mcp_write_enabled`).

## [1.2.0] — 2026-06-22

A large feature drop inspired by the "LLM Wiki" pattern: turn the library from per-article storage into an interconnected, synthesizable knowledge base — plus stronger multi-tenant isolation.

### Added
- **🧩 Concept pages** — synthesize everything you saved about one concept into a single cited "living encyclopedia entry." Sources are gathered by *semantic coherence* (not raw tag membership); broad/heterogeneous tags are auto-split into focused sub-concepts via embedding clustering (networkx + Louvain). Contradictions between sources surface in a dedicated section. New endpoints under `/api/concepts`, new pages at `/concepts`.
  - Per-page **auto-update** toggle: when new relevant content arrives, either just flag the page stale (default) or auto-regenerate it. A semantic *centroid* per page powers "new relevant content" detection for all page types.
- **🕸️ Graph Insights** — community detection (Louvain), hubs/centrality, "surprising connections" (Adamic-Adar link prediction: pages that *should* be linked but aren't), and knowledge gaps (orphan articles). New `/api/knowledge/insights` + an insights panel on the graph page; a one-line insight is also appended to the periodic WeChat review digest.
- **🔗 Graph-augmented retrieval** — RAG and deep-research expand from vector hits along the knowledge graph, pulling in explicitly-related articles (incl. `contradicts`/`prerequisite`) that pure similarity misses.
- **🎯 Knowledge-base purpose** — describe what your library is for; it's injected into Q&A and research prompts so answers match your domain's framing.
- **⚖️ Contradiction detection on ingest** — new articles are checked against existing knowledge for opposing views; conflicts create a `contradicts` edge and (if bound) a WeChat heads-up.
- **🐙 GitHub repo capture** — `github.com/owner/repo` links fetch README + metadata (stars/forks/language/topics) via the REST API.
- **🔄 Obsidian two-way sync** — new `PATCH /api/sync/articles/{id}` writeback endpoint with last-write-wins + timestamp-guard conflict detection; editing a synced note's body/title flows back to the server (re-embeds on content change). (Companion plugin update ships separately.)
- **🔌 MCP server** — `POST /api/mcp` (Streamable-HTTP, Bearer auth) exposes the knowledge base to external AI agents: search_knowledge / get_article / knowledge_insights / list_recent_articles.
- **♻️ Content de-duplication** — articles carry a SHA256 `content_hash` so the same content saved via different URLs is detected.

### Changed / Fixed
- **Multi-tenant isolation hardening** — folders, tags, and tag stats default to the current user even for superadmins (use `?username=` to view another user); tags are isolated per user (`UNIQUE(user_id, name)` instead of global); graph generation only compares a user's own articles (no cross-user content reaching the LLM).
- Removed the standalone **Spark** page from the nav (inspiration writing still lives in the "Add content" dialog).

### Dependencies
- Added `networkx>=3.2,<4` and `python-louvain>=0.16`.

### Migrations
- `008`–`011`: `users.kb_purpose`, `articles.content_hash`, per-user tag uniqueness, and the `concept_pages` table (+ `centroid` / `auto_update`).

## [1.1.0] — 2026-05-31

### Added
- **🎬 WeChat Channels (视频号) capture** — links from `channels.weixin.qq.com` are now fetched and saved. WeChat Channels pages are JavaScript-rendered, so they are handled by the new generic extraction cascade (below), which renders the page with a headless browser before extracting the main content.
- **🪶 Smart generic extraction cascade** — pages without a dedicated parser (WeChat Channels, CSDN, Juejin, Medium, SSPai, 36Kr, and any other site) now go through a three-stage pipeline for far cleaner main-content extraction:
  1. `trafilatura` extracts the article body from the raw HTML (stable; strips nav/footer/ads);
  2. if the extracted text is too short (a sign of a client-rendered page), the page is rendered with the bundled headless Chromium and re-extracted, keeping the longer result;
  3. as a last resort it falls back to the original BeautifulSoup heuristic cleaner.
  The downstream `clean_to_markdown` pipeline is unchanged — `trafilatura` outputs HTML, so existing processing just works.
- **📄 Article-scoped Q&A** — on an article detail page the assistant can now answer questions **strictly from that one article** (the whole article is fed into context, with no library-wide vector search). A 📄 this-article / 📚 whole-library toggle appears in the chat box on article pages; the explicit `/r` `/a` `/c` commands still escalate to whole-library research/creation.

### Fixed
- **Generic web capture was broken** — the generic fetch path called content-extraction helpers (`_extract_content`, `_extract_title`, `_extract_author`, `_extract_cover`) that were missing, so capturing any site without a dedicated parser would error. These helpers are restored and the path now works end to end.
- **Xiaohongshu image proxying was broken** — the XHS parser called image-proxy helpers (`_proxy_url`, `_proxy_imgs_in_html`) that were missing, so XHS capture would error before saving. These helpers (and the hotlink-protected CDN list) are restored.

### Dependencies
- Added `trafilatura>=2.0.0,<3` and `lxml_html_clean>=0.4.0` (the latter is required because `lxml.html.clean` was split into a standalone package as of lxml 5.2).

## [1.0.0] — 2026-05-23

Initial open-source release of Trove AI (拾遗 AI) — a self-hostable, AI-powered second brain for turning saved links into structured, searchable knowledge.

### Added
- Multi-platform article capture with platform-specific parsers (WeChat 公众号, Bilibili, Toutiao, Douyin, Xiaohongshu) plus a generic-web fallback.
- AI processing pipeline: title / summary / key-points / tags / embedding / mind-map.
- RAG Q&A with citations + pgvector semantic search.
- Automatic knowledge graph and learning-path generation.
- WeChat Bot ingress.
- One-way Obsidian sync with revocable sync tokens; multi-tenant support.
- Docker-based self-hosting; responsive UI for PC / pad / mobile.
