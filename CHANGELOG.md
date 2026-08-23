# Changelog

All notable changes to Trove AI are documented here. This project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.4.1] — 2026-08-23

### Added
- **Agent URL-to-folder action** — web, WeChat, and Feishu now understand both “create folder X and save URL Y into it” and “save URL Y into folder X”. The confirmed write action creates or reuses the folder, ingests a new URL through the existing AI-processing pipeline, and moves an already-saved URL without duplicating it.

### Changed
- Channel adapters keep the fast path for plain URLs, but route URL messages with folder/organization intent to the unified Agent so the full instruction is preserved.

## [1.4.0] — 2026-08-23

### Added
- **Unified knowledge-management Agent** — the open-source web assistant now uses the same session memory, long-term user memory, implicit intent routing, streaming Markdown, and follow-up context as the maintained deployment.
- **Confirmed write tools** — the Agent can tag articles, move articles into folders, create graph relationships, synthesize concept pages, and configure periodic review. Every data-changing tool pauses at a confirmation gate and executes only after the user confirms; all reads and writes are scoped to the authenticated user.
- **Feishu Bot channel** — a self-built Feishu app can receive text, links, files, images, and voice input through `im.message.receive_v1`. Users bind their Feishu identity with a short-lived six-digit code from Personal Settings; private chats are supported and group chats respond only when mentioned.
- **Clash/Mihomo subscription proxy** — Docker Compose now includes an internal Mihomo sidecar. Administrators can paste a subscription URL in System Management, test it, and choose direct or proxy collection per platform. The backend generates a restricted config, masks the subscription URL, and hot-reloads Mihomo without exposing its controller publicly.
- **Vision and web-search settings** — system management includes OpenAI-compatible vision and Tavily search configuration, used by image ingestion and the Agent's external research tools.

### Changed
- Collection routing is now `direct / proxy`; the open-source build contains no local/VPS acquisition-task mechanism.
- The web, WeChat, and Feishu entry points share the same Agent contract. Voice is input-only; outbound TTS remains intentionally excluded.
- Sensitive system configuration, cache, rebuild, and update-check endpoints now require an explicit super-admin credential instead of accepting the anonymous default-user fallback.

### Migrations
- `013_agent_memory.sql`: Agent sessions, messages, and long-term user memory.
- `014_channel_bindings.sql`: channel identity bindings, one-time codes, and webhook event deduplication.

## [1.3.0] — 2026-07-06

### Added
- **Knowledge-management Agent positioning** — README copy now frames Trove AI as an open-source, self-hostable knowledge-management Agent instead of only a read-later tool. The web AI assistant and WeChat Bot are documented as Agent entry points over the same private library.
- **WeChat Bot media input** — the bot can now receive files, images, and voice messages. Files/images are downloaded from iLink media, decrypted, and uploaded through the existing `/api/articles/upload` pipeline; voice messages use `voice_item.text` when available and fall back to the transcription service before routing the text into the Agent. Outbound TTS/voice bubbles are intentionally not included.
- **Open-source maintenance signals** — system settings now show the GitHub repository, release/changelog link, current version, commit, and a best-effort GitHub Releases update check (`GET /api/system/version`, `GET /api/system/update-check`). The sidebar also links to the public repository.
- **Broader upload formats** — file upload now accepts `.webp`, `.gif`, and `.htm`, matching the WeChat Bot media ingestion path.
- **MCP write tools (opt-in)** — the MCP server can now create/modify content, gated behind a per-user "allow write" switch (default **off**). When enabled it additionally exposes `add_article`, `add_note`, `update_article`, `set_article_tags`; read tools stay always-on. New `PUT /api/auth/mcp-write` toggle + a switch and live tool list in the settings "外部 AI 接入 (MCP)" card. Migration `012` (`users.mcp_write_enabled`).

### Changed
- The open-source WeChat Bot default question flow routes to the available tool Agent; the unified memory and confirmed-write endpoint is included from v1.4.0 onward.

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
