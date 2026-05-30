# Changelog

All notable changes to Trove AI are documented here.

## [Unreleased]

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

### Dependencies
- Added `trafilatura>=2.0.0,<3` and `lxml_html_clean>=0.4.0` (the latter is required because `lxml.html.clean` was split into a standalone package as of lxml 5.2).
