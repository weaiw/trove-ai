"""Content parsing service - extract clean content from web pages."""
import re
import json
import httpx
import logging
from typing import Dict, Optional
from urllib.parse import unquote
from bs4 import BeautifulSoup
from markdownify import markdownify as md_convert

logger = logging.getLogger(__name__)


# Match http(s) URL up to whitespace, CJK char, or common Chinese punctuation.
# Used to extract the actual link from share text (抖音/头条 share blobs etc.).
_URL_RE = re.compile(
    r'https?://[^\s一-鿿"\'<>{}|\\^`，。、；：！？【】（）《》""'']+',
    re.IGNORECASE,
)


def extract_url_from_text(text: str) -> Optional[str]:
    """Pull the first http(s):// URL out of a possibly-noisy share string."""
    if not text:
        return None
    m = _URL_RE.search(text)
    if not m:
        return None
    # Strip trailing punctuation that often clings to URLs in share blobs.
    return m.group(0).rstrip('.,;:!?)]')


class ParserService:
    """Extract and clean article content from various platforms."""
    
    PLATFORM_DETECT = {
        'weixin.qq.com': 'wechat',
        'mp.weixin.qq.com': 'wechat',
        'toutiao.com': 'toutiao',
        'jianshu.com': 'jianshu',
        'csdn.net': 'csdn',
        'medium.com': 'medium',
        'juejin.cn': 'juejin',
        'sspai.com': 'sspai',
        '36kr.com': '36kr',
        'weibo.com': 'weibo',
        'bilibili.com': 'bilibili',
        'b23.tv': 'bilibili',          # bilibili 短链
        'douban.com': 'douban',
        'douyin.com': 'douyin',
        'iesdouyin.com': 'douyin',     # 抖音分享口令短链域
        'xiaohongshu.com': 'xhs',      # 小红书
        'xhslink.com': 'xhs',          # 小红书短链
    }
    
    def detect_platform(self, url: str) -> str:
        """Detect source platform from URL."""
        for domain, platform in self.PLATFORM_DETECT.items():
            if domain in url:
                return platform
        return 'other'
    
    def _get_headers(self, platform: str, url: str) -> Dict[str, str]:
        """Get platform-specific HTTP headers to avoid 403 and anti-scraping."""
        base_headers = {
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        }
        
        if platform == 'toutiao':
            base_headers.update({
                'Referer': 'https://www.toutiao.com/',
                'Cache-Control': 'max-age=0',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'cross-site',
                'Sec-Fetch-User': '?1',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Ch-Ua': '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="24"',
                'Sec-Ch-Ua-Mobile': '?0',
                'Sec-Ch-Ua-Platform': '"macOS"',
            })
        elif platform == 'wechat':
            base_headers.update({
                'Referer': 'https://mp.weixin.qq.com/',
            })
        
        return base_headers
    
    def _extract_og_metadata(self, soup: BeautifulSoup) -> Dict:
        """Extract OpenGraph and other meta tag metadata."""
        meta = {}
        for og_tag in soup.find_all('meta'):
            prop = og_tag.get('property', '') or og_tag.get('name', '')
            content = og_tag.get('content', '')
            if not content:
                continue
            
            if prop == 'og:title':
                meta['title'] = content
            elif prop == 'og:description' or prop == 'description':
                if 'description' not in meta:
                    meta['description'] = content
            elif prop == 'og:image':
                meta['image'] = content
            elif prop == 'og:site_name':
                meta['site_name'] = content
            elif prop == 'author' or prop == 'og:article:author':
                meta['author'] = content
            elif prop == 'article:published_time':
                meta['published_time'] = content
        return meta
    
    async def fetch_content(self, url: str) -> Dict:
        """Fetch and parse article content from URL."""
        url = extract_url_from_text(url) or url
        platform = self.detect_platform(url)
        
        # P0: Toutiao — use mobile SSR endpoint (m.toutiao.com) to bypass
        # byte跳's byted_acrawler JS VM on desktop site
        if platform == 'toutiao':
            return await self._fetch_toutiao(url)
        
        # P2: Douyin — use douyin-tiktok-scraper library (X-Bogus + API)
        if platform == 'douyin':
            return await self._fetch_douyin(url)

        # P3: Bilibili — split into 专栏 (HTML) vs 视频 (API + 字幕)
        if platform == 'bilibili':
            return await self._fetch_bilibili(url)

        # P4: Xiaohongshu — follow xhslink redirect → curl_cffi mobile UA →
        # parse __INITIAL_STATE__ inline JSON → Playwright/OG fallback
        if platform == 'xhs':
            return await self._fetch_xhs(url)

        # P5: 通用网页 — trafilatura 优先,内容过短再 Playwright 渲染,最后 BeautifulSoup 兜底
        # 视频号(channels.weixin.qq.com)、CSDN、掘金、Medium、少数派、36氪 等 JS 动态页同走此路
        return await self._fetch_generic(url, platform)

    # ── 通用网页提取级联 ──────────────────────────────────────────────
    # trafilatura(快、正文质量稳)→ 内容过短则 Playwright 渲染后重试 → BeautifulSoup 兜底。
    _MIN_CONTENT_CHARS = 200  # 正文纯文本短于此值视为提取不足,触发下一级

    @staticmethod
    def _text_len(html_or_text: Optional[str]) -> int:
        if not html_or_text:
            return 0
        return len(BeautifulSoup(html_or_text, 'lxml').get_text(strip=True))

    def _trafilatura_extract(self, html: str, url: str) -> Optional[str]:
        """用 trafilatura 提取正文(输出 HTML,保持与下游 clean_to_markdown 一致)。失败/未装返回 None。"""
        try:
            import trafilatura
        except ImportError:
            return None
        try:
            return trafilatura.extract(
                html, url=url, output_format='html',
                include_images=True, include_links=True, include_tables=True,
                include_formatting=True,
            )
        except Exception as e:
            logger.warning(f"trafilatura extract failed for {url}: {e}")
            return None

    async def _render_with_playwright(self, url: str) -> Optional[str]:
        """复用现成 headless Chromium 渲染 JS 动态页,返回完整 HTML。失败返回 None。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return None
        ua = self._get_headers('other', url)['User-Agent']
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
                )
                ctx = await browser.new_context(
                    user_agent=ua, viewport={'width': 1920, 'height': 1080}, locale='zh-CN',
                )
                page = await ctx.new_page()
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                except Exception as e:
                    logger.warning(f"playwright nav {url}: {e}")
                import asyncio as _a
                await _a.sleep(3)  # 给 CSR 一点渲染时间
                html = await page.content()
                await browser.close()
                return html
        except Exception as e:
            logger.warning(f"playwright render failed for {url}: {e}")
            return None

    def _build_generic_result(self, html: str, url: str, platform: str) -> Dict:
        """从原始 HTML 构建解析结果:trafilatura 优先,BeautifulSoup 兜底;元数据走 OG/soup。"""
        soup = BeautifulSoup(html, 'lxml')
        og_meta = self._extract_og_metadata(soup)

        # 1) trafilatura 优先(正文 HTML)
        content_html = self._trafilatura_extract(html, url)

        # 2) 回退:BeautifulSoup 启发式清洗 + 提取
        if not content_html or self._text_len(content_html) < self._MIN_CONTENT_CHARS:
            for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'iframe', 'noscript']):
                tag.decompose()
            # 精确类名匹配,避免子串误伤(如 'comment' 命中 'comment_feature')
            for cls in ['advertisement', 'comment', 'recommend', 'related', 'sidebar', 'share',
                        'sharing', 'bottom-bar', 'toolbar', 'report', 'copyright']:
                pattern = re.compile(r'(?:^|\s)' + re.escape(cls) + r'(?:\s|$)', re.I)
                for tag in soup.find_all(class_=pattern):
                    tag.decompose()
            content_html = self._extract_content(soup, platform, og_meta)

        title = og_meta.get('title') or self._extract_title(soup, platform)
        author = og_meta.get('author') or self._extract_author(soup, platform)
        cover = og_meta.get('image') or self._extract_cover(soup, platform)

        # WeChat/视频号 封面图走代理(mmbiz.qpic.cn 有 referer 防盗链)
        if cover and 'mmbiz.qpic.cn' in cover:
            from urllib.parse import quote
            cover = f"/api/images/proxy?url={quote(cover, safe='')}"

        return {
            'title': title,
            'raw_html': html,
            'raw_content': content_html,
            'platform': platform,
            'author': author,
            'cover_image': cover,
            'og_meta': og_meta,
        }

    async def _fetch_generic(self, url: str, platform: str) -> Dict:
        """通用网页抓取 + 提取级联。"""
        headers = self._get_headers(platform, url)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text

        result = self._build_generic_result(html, url, platform)

        # 内容过短 → 大概率是 JS 动态渲染页(视频号/部分前端框架站),Playwright 渲染后重试,取更长者
        if self._text_len(result['raw_content']) < self._MIN_CONTENT_CHARS:
            rendered = await self._render_with_playwright(url)
            if rendered:
                alt = self._build_generic_result(rendered, url, platform)
                if self._text_len(alt['raw_content']) > self._text_len(result['raw_content']):
                    logger.info(f"generic fetch: playwright render improved content for {url}")
                    result = alt
        return result

    # ── 通用提取辅助(供 _build_generic_result 兜底使用) ──────────────
    def _extract_content(self, soup: BeautifulSoup, platform: str, og_meta: Dict) -> str:
        """Extract main content based on platform-specific selectors."""
        content_html = ""

        # 公众号/视频号(channels.weixin.qq.com → 'wechat')正文容器
        if platform == 'wechat':
            article = soup.find('div', id='js_content') or soup.find('div', class_='rich_media_content')
            if article:
                content_html = str(article)

        # Generic extraction fallback
        if not content_html:
            article = soup.find('article')
            if article:
                content_html = str(article)
            else:
                for selector in [
                    {'name': 'div', 'attrs': {'class': 'article-content'}},
                    {'name': 'div', 'attrs': {'class': 'post-content'}},
                    {'name': 'div', 'attrs': {'class': 'entry-content'}},
                    {'name': 'div', 'attrs': {'id': 'content'}},
                    {'name': 'main', 'attrs': {}},
                ]:
                    article = soup.find(selector['name'], selector['attrs'])
                    if article:
                        content_html = str(article)
                        break

        # Final fallback: whole body
        if not content_html:
            body = soup.find('body')
            content_html = str(body) if body else str(soup)
        return content_html

    def _extract_title(self, soup: BeautifulSoup, platform: str) -> str:
        """Extract article title."""
        h1 = soup.find('h1')
        if h1 and h1.get_text(strip=True):
            return h1.get_text(strip=True)
        title_tag = soup.find('title')
        if title_tag:
            return title_tag.get_text(strip=True)
        return "Untitled"

    def _extract_author(self, soup: BeautifulSoup, platform: str) -> str:
        """Extract article author."""
        for selector in [
            {'name': 'a', 'attrs': {'class': 'author'}},
            {'name': 'span', 'attrs': {'class': 'author'}},
            {'name': 'div', 'attrs': {'class': 'author'}},
        ]:
            tag = soup.find(selector['name'], selector['attrs'])
            if tag and tag.get_text(strip=True):
                return tag.get_text(strip=True)
        return ""

    def _extract_cover(self, soup: BeautifulSoup, platform: str) -> str:
        """Extract cover image."""
        og_img = soup.find('meta', property='og:image')
        if og_img:
            return og_img.get('content', '')
        return ""

    # CDNs that block hotlink requests without a proper Referer. URLs from these
    # hosts must be rewritten through /api/images/proxy (served with the right Referer).
    _HOTLINK_PROTECTED_CDNS = (
        'mmbiz.qpic.cn', 'mmbiz.qlogo.cn', 'mmecoa.qpic.cn',  # WeChat / 视频号
        'xhscdn.com',                                           # XHS
        'douyinpic.com', 'douyinvod.com',                       # Douyin
    )

    @classmethod
    def _proxy_url(cls, image_url: Optional[str]) -> Optional[str]:
        """Rewrite hotlink-protected image URLs through the backend proxy.

        The proxy endpoint (/api/images/proxy) adds the right Referer per CDN.
        Non-protected URLs are returned unchanged. None / empty pass through.
        """
        if not image_url:
            return image_url
        if image_url.startswith('/api/images/proxy'):
            return image_url  # already rewritten
        if any(d in image_url for d in cls._HOTLINK_PROTECTED_CDNS):
            from urllib.parse import quote
            return f"/api/images/proxy?url={quote(image_url, safe='')}"
        return image_url

    @classmethod
    def _proxy_imgs_in_html(cls, html: str) -> str:
        """Rewrite any <img src=...> referencing a hotlink-protected CDN through proxy."""
        if not html:
            return html
        soup = BeautifulSoup(html, 'lxml')
        for img in soup.find_all('img'):
            src = img.get('src') or ''
            new_src = cls._proxy_url(src)
            if new_src != src:
                img['src'] = new_src
        # BeautifulSoup with lxml wraps content in <html><body>; strip if added.
        body = soup.body
        if body:
            return body.decode_contents()
        return str(soup)

    async def _fetch_bilibili(self, url: str) -> Dict:
        """Bilibili: route by URL shape.
        - 专栏 (read/cv...) and opus (新版图文动态/笔记): HTML scrape
        - 视频 (video/BV...): official API + subtitle
        """
        is_article_like = (
            'read.bilibili.com' in url
            or '/read/cv' in url
            or '/read/mobile' in url
            or '/opus/' in url
            or '/dynamic/' in url
        )
        if is_article_like:
            return await self._fetch_bilibili_article(url)
        return await self._fetch_bilibili_video(url)

    async def _fetch_bilibili_article(self, url: str) -> Dict:
        """Bilibili 专栏 — anti-bot is mild, plain GET works."""
        headers = self._get_headers('bilibili', url)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, 'lxml')
        # Title
        title = ''
        if soup.find('h1'):
            title = soup.find('h1').get_text(strip=True)
        if not title:
            og = soup.find('meta', property='og:title')
            if og:
                title = og.get('content', '')
        # Author
        author = ''
        author_meta = soup.find('meta', attrs={'name': 'author'})
        if author_meta:
            author = author_meta.get('content', '')
        # Cover
        cover = ''
        og_img = soup.find('meta', property='og:image')
        if og_img:
            cover = og_img.get('content', '')
        # Main content
        main = (
            soup.find('div', class_='opus-module-content')
            or soup.find('div', class_='article-content')
            or soup.find('article')
        )
        raw_content = str(main) if main else html

        return {
            'title': title or 'Bilibili 专栏',
            'raw_html': html,
            'raw_content': raw_content,
            'platform': 'bilibili',
            'author': author,
            'cover_image': cover,
        }

    async def _fetch_bilibili_video(self, url: str) -> Dict:
        """Bilibili 视频 — pull metadata + subtitle via official API."""
        # b23.tv 短链 → 跳转拿到含 bvid 的真实 URL
        if 'b23.tv' in url:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                r = await client.get(url, headers=self._get_headers('bilibili', url))
                url = str(r.url)

        m = re.search(r'(BV[0-9A-Za-z]{10})', url)
        if not m:
            raise Exception(f"Cannot extract bvid from URL: {url}")
        bvid = m.group(1)

        from bilibili_api import video as bv
        v = bv.Video(bvid=bvid)
        info = await v.get_info()

        title = info.get('title', '')
        desc = info.get('desc', '') or ''
        cover = info.get('pic', '')
        owner = (info.get('owner') or {}).get('name', '')
        cid = info.get('cid')

        # Subtitle (best effort — many videos have none)
        subtitle_text = ''
        try:
            sub_info = await v.get_subtitle(cid=cid) if cid else None
            subtitles = (sub_info or {}).get('subtitles', [])
            if subtitles:
                sub_url = subtitles[0].get('subtitle_url', '')
                if sub_url and sub_url.startswith('//'):
                    sub_url = 'https:' + sub_url
                if sub_url:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        sub_resp = await client.get(sub_url)
                        sub_resp.raise_for_status()
                        sub_data = sub_resp.json()
                        body = sub_data.get('body', [])
                        subtitle_text = '\n'.join(
                            line.get('content', '') for line in body if line.get('content')
                        )
        except Exception as e:
            logger.warning(f"bilibili subtitle fetch failed for {bvid}: {e}")

        raw_md = f"# {title}\n\n**UP 主：** {owner}\n\n## 简介\n\n{desc}"
        if subtitle_text:
            raw_md += f"\n\n## 视频字幕\n\n{subtitle_text}"
        else:
            raw_md += "\n\n*（该视频未提供字幕）*"

        return {
            'title': title or f'B 站视频 {bvid}',
            'raw_html': '',
            'raw_content': raw_md,
            'platform': 'bilibili',
            'author': owner,
            'cover_image': cover,
        }

    async def _fetch_toutiao(self, url: str) -> Dict:
        """Fetch Toutiao article via mobile SSR endpoint (m.toutiao.com).
        
        Desktop www.toutiao.com returns a JS VM challenge (byted_acrawler) that
        appears as garbled text. The mobile site m.toutiao.com uses pure SSR with
        all article data in a <script id="RENDER_DATA"> JSON block — no anti-crawling.
        """
        # Convert desktop URL to mobile equivalent
        mobile_url = re.sub(
            r'https?://(?:www\.)?toutiao\.com',
            'https://m.toutiao.com',
            url
        )
        # If it's a short link, follow redirects to get the real URL
        # e.g., toutiao.com/article/xxx → same on mobile
        
        mobile_headers = {
            'User-Agent': (
                'Mozilla/5.0 (Linux; Android 13; Pixel 7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Mobile Safari/537.36'
            ),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(mobile_url, headers=mobile_headers)
            resp.raise_for_status()
            html = resp.text
        
        # Extract the RENDER_DATA JSON block
        # Format: <script id="RENDER_DATA" type="application/json">URL_ENCODED_JSON</script>
        match = re.search(
            r'<script[^>]*id="RENDER_DATA"[^>]*type="application/json"[^>]*>(.*?)</script>',
            html,
            re.DOTALL
        )
        
        if not match:
            raise ValueError(
                f"Could not find RENDER_DATA block in Toutiao page. "
                f"Page length: {len(html)} chars. "
                f"First 200 chars: {html[:200]}"
            )
        
        raw_json = match.group(1).strip()
        if not raw_json:
            raise ValueError("RENDER_DATA block is empty")
        
        try:
            data = json.loads(unquote(raw_json))
        except json.JSONDecodeError:
            # Sometimes the JSON is not URL-encoded
            data = json.loads(raw_json)
        
        # Navigate to article data — structure varies slightly
        article = None
        for path in [
            lambda d: d.get("articleInfo"),
            lambda d: (d.get("data") or {}).get("articleInfo"),
            lambda d: d.get("data", {}),
        ]:
            try:
                article = path(data)
                if article and isinstance(article, dict) and article.get("content"):
                    break
            except Exception:
                continue
        
        if not article or not isinstance(article, dict):
            raise ValueError(
                f"Could not extract article from RENDER_DATA. "
                f"Top-level keys: {list(data.keys())[:10]}"
            )
        
        # Extract fields — most string values are URL-encoded
        def safe_unquote(v):
            """Unquote a value, handling None and non-string types."""
            if v is None:
                return ""
            if isinstance(v, (int, float)):
                return str(v)
            result = unquote(v)
            # Sometimes values are double-encoded
            if '%' in result:
                try:
                    result = unquote(result)
                except Exception:
                    pass
            return result
        
        title = safe_unquote(article.get("title", ""))
        content_html = safe_unquote(article.get("content", ""))
        
        if not content_html:
            # Some articles are video-only or short-form
            detail = article.get("detailSource") or article.get("abstract") or ""
            if detail:
                content_html = f"<p>{safe_unquote(detail)}</p>"
            else:
                raise ValueError("Toutiao article has no text content (may be video-only)")
        
        # Author info
        media_user = article.get("mediaUser") or article.get("userInfo") or {}
        author_name = safe_unquote(media_user.get("screenName") or media_user.get("name") or "")
        
        # Cover image
        cover = ""
        if media_user.get("avatarUrl"):
            cover = safe_unquote(media_user.get("avatarUrl", ""))
        if article.get("coverImage") or article.get("cover"):
            cover = article.get("coverImage") or article.get("cover") or cover
        
        # Publish time (Unix timestamp in seconds)
        publish_time = article.get("publishTime") or article.get("createTime") or 0
        
        # Engagement stats for metadata
        comment_count = article.get("commentCount", 0)
        digg_count = article.get("diggCount", 0)
        
        # Build OG metadata for downstream consumers
        og_meta = {
            'title': title,
            'author': author_name,
            'image': cover,
            'description': safe_unquote(article.get("abstract") or article.get("detailSource") or ""),
            'published_time': str(publish_time),
            'site_name': '今日头条',
        }
        
        logger.info(
            f"Toutiao parse success: title='{title[:50]}', "
            f"content_len={len(content_html)}, author='{author_name}', "
            f"comments={comment_count}, likes={digg_count}"
        )
        
        return {
            'title': title,
            'raw_html': html,
            'raw_content': content_html,
            'platform': 'toutiao',
            'author': author_name or 'unknown',
            'cover_image': cover or None,
            'og_meta': og_meta,
        }
    
    async def _fetch_douyin(self, url: str) -> Dict:
        """Fetch Douyin content using Playwright — intercepts the internal API.
        
        Douyin is a React SPA. Instead of trying to generate X-Bogus signatures
        to call the API directly, we let Playwright load the page and intercept
        the /aweme/v1/web/aweme/detail/ XHR response that the SPA itself makes.
        This gives us the full structured JSON from Douyin's own API — no
        signature cracking required.
        
        Supports:
        - Share links: https://v.douyin.com/xxxxx/
        - Video pages: https://www.douyin.com/video/xxxxx
        - Note/articles: https://www.douyin.com/note/xxxxx (图文)
        """
        import asyncio
        import os
        
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ValueError(
                "playwright not installed. Add 'playwright>=1.40.0' to requirements.txt"
            )
        
        logger.info(f"Fetching Douyin via Playwright: {url}")
        
        # Capture the API response via request interception
        api_response = None
        
        async def on_response(response):
            nonlocal api_response
            if api_response is not None:
                return
            # Intercept the aweme detail API call.
            # Multiple endpoints — video shares hit /aweme/v1/web/aweme/detail/,
            # image-note shares (/share/note/...) hit different paths.
            req_url = response.request.url
            api_patterns = (
                '/aweme/v1/web/aweme/detail/',
                '/aweme/v1/web/aweme/iteminfo/',
                '/aweme/v1/web/aweme/post/',
                '/web/api/v2/aweme/iteminfo/',
                '/aweme/v1/web/note/',
            )
            if any(p in req_url for p in api_patterns):
                status = response.status
                if status == 200:
                    try:
                        body = await response.json()
                        if body.get('aweme_detail'):
                            api_response = body
                            logger.info(f"Douyin API intercepted: {len(str(body))} bytes")
                    except Exception:
                        pass
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                ]
            )
            
            context = await browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1920, 'height': 1080},
                locale='zh-CN',
            )
            
            page = await context.new_page()
            
            # Register response handler BEFORE navigation
            page.on('response', on_response)
            
            # Navigate to the douyin page
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                logger.warning(f"Page navigation error (may be ok): {e}")
            
            # Wait for the API response (polling)
            import time as time_mod
            wait_start = time_mod.time()
            while api_response is None and (time_mod.time() - wait_start) < 15:
                await asyncio.sleep(0.5)
            
            if api_response is None:
                logger.warning(
                    "Timeout waiting for Douyin API response. "
                    "Falling back to DOM extraction."
                )
            
            # If API interception failed, try the SSR data in the DOM
            if api_response is None:
                try:
                    # state='attached' — RENDER_DATA is a <script> tag, never "visible"
                    await page.wait_for_selector(
                        '#RENDER_DATA, [data-e2e="feed-active-video"], .video-info-detail',
                        state='attached',
                        timeout=8000,
                    )
                except Exception:
                    pass

                # Try extracting from rendered DOM
                html = await page.content()
                page_text = await page.evaluate("document.body.innerText")

                # Check for error pages
                if len(page_text) < 100 and '验证' in page_text:
                    raise ValueError(
                        "Douyin captcha/verification page detected. "
                        "Try again later or use a different IP."
                    )

                # First try: parse RENDER_DATA from HTML directly (works without JS execution)
                import re
                from urllib.parse import unquote
                m = re.search(
                    r'<script[^>]*id="RENDER_DATA"[^>]*>(.*?)</script>',
                    html, re.DOTALL,
                )
                if m:
                    raw = m.group(1).strip()
                    try:
                        data = json.loads(unquote(raw))
                        # douyin RENDER_DATA shape: data.app.videoDetail or data.<route>.aweme
                        for key_path in [
                            lambda d: d.get('app', {}).get('videoDetail'),
                            lambda d: (d.get('app') or {}).get('aweme'),
                            lambda d: next(iter(d.values())) if d else None,
                        ]:
                            try:
                                result = key_path(data)
                                if result and isinstance(result, dict) and (
                                    result.get('aweme_id') or result.get('awemeId') or result.get('aweme_detail')
                                ):
                                    api_response = result if 'aweme_detail' in result else {'aweme_detail': result}
                                    logger.info(f"Douyin RENDER_DATA parsed via regex fallback")
                                    break
                            except Exception:
                                continue
                    except Exception as parse_err:
                        logger.warning(f"Failed parsing RENDER_DATA: {parse_err}")

                # Second try: window._ROUTER_DATA via JS
                if api_response is None:
                    try:
                        router_data = await page.evaluate("window._ROUTER_DATA")
                        if router_data and isinstance(router_data, dict):
                            api_response = {'routerData': router_data}
                    except Exception:
                        pass

                # Capture resolved URL + OG meta now (we use them either as the
                # final fallback below or to enrich the result above).
                try:
                    resolved_url = await page.evaluate("location.href") or url
                except Exception:
                    resolved_url = url
                try:
                    og_meta_capture = await page.evaluate("""() => {
                        const get = (sel) => document.querySelector(sel)?.getAttribute('content') || '';
                        // Fall back to the first douyinpic / douyinvod image rendered on the page
                        // when og:image isn't present (note pages emit only meta name="description").
                        const cdnImg = Array.from(document.images || [])
                            .map(i => i.src || '')
                            .find(s => s && (s.includes('douyinpic.com') || s.includes('douyinvod.com')));
                        return {
                            title: get('meta[property=\\"og:title\\"]') || document.title || '',
                            description: get('meta[property=\\"og:description\\"]') || get('meta[name=\\"description\\"]') || '',
                            image: get('meta[property=\\"og:image\\"]') || cdnImg || '',
                        };
                    }""")
                except Exception:
                    og_meta_capture = {}

                # Third try: SSR JSON in a <script id="RENDER_DATA"> — already attempted
                # above, but for image-note pages it can also be inside an inline
                # script literal. As a last resort, scan the rendered HTML for any
                # plausible "aweme_detail" / "noteDetail" JSON blob.
                if api_response is None:
                    for needle in ('"aweme_detail":', '"noteDetail":', '"videoDetail":'):
                        idx = html.find(needle)
                        if idx < 0:
                            continue
                        # Walk back to the enclosing '{', then count braces forward.
                        start = html.rfind('{', 0, idx)
                        if start < 0:
                            continue
                        depth = 0
                        end = -1
                        for j in range(start, min(len(html), start + 2_000_000)):
                            c = html[j]
                            if c == '{':
                                depth += 1
                            elif c == '}':
                                depth -= 1
                                if depth == 0:
                                    end = j
                                    break
                        if end > start:
                            try:
                                blob = json.loads(html[start:end + 1])
                                api_response = {'fromHtmlBlob': blob}
                                logger.info(f"Douyin extracted via HTML blob scan ({needle})")
                                break
                            except Exception:
                                continue

                # Fourth try (last resort): OG meta fallback. For note pages where
                # all structured extraction fails, OG meta tags still contain
                # title / description / image — better than failing.
                if api_response is None and og_meta_capture and (
                    og_meta_capture.get('description') or og_meta_capture.get('title')
                ):
                    api_response = {'fromOgMeta': og_meta_capture, 'resolvedUrl': resolved_url}
                    logger.info(
                        f"Douyin extracted via OG meta fallback (note-style page). "
                        f"title head: {(og_meta_capture.get('title') or '')[:40]}"
                    )
            
            await browser.close()
        
        if api_response is None:
            raise ValueError(
                "Could not extract Douyin video data. "
                "The page may be blocked or the link may be invalid."
            )
        
        # ── Parse the API response ──
        # Sources we may have: aweme_detail (API), routerData (window._ROUTER_DATA),
        # fromHtmlBlob (HTML scan). Walk them to find the aweme detail dict.
        aweme = None
        if 'aweme_detail' in api_response:
            aweme = api_response['aweme_detail']
        elif 'routerData' in api_response:
            router = api_response['routerData']
            loader = (router.get('loaderData') or {}) if isinstance(router, dict) else {}
            # Walk every loaderData entry — covers video_(id)_0, note_(id)_0, and
            # any future variants without hardcoding the prefix.
            for v in loader.values():
                if not isinstance(v, dict):
                    continue
                # Common shapes seen in the wild
                candidates = [
                    v.get('aweme_detail'),
                    (v.get('aweme') or {}).get('detail'),
                    v.get('noteDetail'),
                    v.get('videoDetail'),
                ]
                for cand in candidates:
                    if isinstance(cand, dict) and (cand.get('aweme_id') or cand.get('awemeId')):
                        aweme = cand
                        break
                if aweme:
                    break
        elif 'fromHtmlBlob' in api_response:
            blob = api_response['fromHtmlBlob']
            for cand in (
                blob.get('aweme_detail') if isinstance(blob, dict) else None,
                blob.get('noteDetail') if isinstance(blob, dict) else None,
                blob.get('videoDetail') if isinstance(blob, dict) else None,
                blob if isinstance(blob, dict) and (blob.get('aweme_id') or blob.get('awemeId')) else None,
            ):
                if isinstance(cand, dict) and (cand.get('aweme_id') or cand.get('awemeId')):
                    aweme = cand
                    break
        elif 'fromOgMeta' in api_response:
            # OG meta is the last-resort path for SSR-only note pages.
            # The description format is, empirically:
            #   "<note content> - <author>于YYYYMMDD发布在抖音，已经收获了N个喜欢，..."
            og = api_response['fromOgMeta']
            resolved = api_response.get('resolvedUrl', '')
            desc_full = og.get('description') or og.get('title') or ''
            content_text = desc_full
            author_name = ''
            create_time = 0
            m = re.match(r'^(.*?) - (.+?)于(\d{8})发布在抖音', desc_full)
            if m:
                content_text = m.group(1).strip()
                author_name = m.group(2).strip()
                try:
                    from datetime import datetime
                    create_time = int(datetime.strptime(m.group(3), "%Y%m%d").timestamp())
                except Exception:
                    pass
            # aweme_id from resolved URL: /note/<id> or /video/<id>
            aweme_id_match = re.search(r'/(?:note|video)/(\d+)', resolved or '')
            aweme_id_val = aweme_id_match.group(1) if aweme_id_match else (resolved or url).rstrip('/').split('/')[-1]
            is_note = '/note/' in (resolved or '')
            aweme = {
                'aweme_id': aweme_id_val,
                'desc': content_text,
                'author': {'nickname': author_name} if author_name else {},
                'create_time': create_time,
                'aweme_type': 68 if is_note else 0,
                'cover': {'url_list': [og['image']]} if og.get('image') else {},
                'images': [{'url_list': [og['image']]}] if (is_note and og.get('image')) else [],
            }
        
        if aweme is None:
            raise ValueError(
                f"Could not find aweme data in API response. "
                f"Keys: {list(api_response.keys())[:10]}"
            )
        
        # ── Extract data from aweme object ──
        aweme_id = aweme.get('aweme_id', '')
        desc = aweme.get('desc', '')
        create_time = aweme.get('create_time', 0)
        aweme_type = aweme.get('aweme_type', 0)  # 0=video, 68=image/note
        
        # Author
        author_data = aweme.get('author', {})
        author_name = author_data.get('nickname', '')
        author_unique_id = author_data.get('unique_id', '')
        
        # Statistics
        stats = aweme.get('statistics', {})
        digg_count = stats.get('digg_count', 0) or stats.get('admire_count', 0)
        comment_count = stats.get('comment_count', 0)
        share_count = stats.get('share_count', 0)
        
        # Cover
        cover_data = aweme.get('video', {}).get('cover', {}) or aweme.get('cover', {})
        cover_url_list = cover_data.get('url_list', [])
        cover_url = cover_url_list[0] if cover_url_list else ''
        
        # Hashtags
        hashtags = []
        for tag in (aweme.get('text_extra', []) or []):
            if isinstance(tag, dict) and tag.get('hashtag_name'):
                hashtags.append(tag['hashtag_name'])
        
        # Music
        music_data = aweme.get('music', {})
        music_title = music_data.get('title', '')
        music_author = music_data.get('author', '')
        
        # ── Build content HTML ──
        content_parts = []
        
        if desc:
            content_parts.append(f"<p>{desc}</p>")
        
        if aweme_type == 68:
            # Image note / 图文
            images = aweme.get('images', [])
            if images:
                content_parts.append('<div class="douyin-images">')
                for img in images:
                    url_list = img.get('url_list', [])
                    if url_list:
                        content_parts.append(
                            f'<img src="{url_list[0]}" alt="douyin image" />'
                        )
                content_parts.append('</div>')
            type_label = 'image'
        else:
            # Video
            video_data = aweme.get('video', {})
            play_addr = video_data.get('play_addr', {})
            play_url_list = play_addr.get('url_list', [])

            nwm_url = ''
            if play_url_list:
                # Replace watermark URL with non-watermark equivalent
                nwm_url = play_url_list[0].replace('playwm', 'play')
                content_parts.append(
                    f'<div class="douyin-video">'
                    f'<p>📹 视频链接: <a href="{nwm_url}">播放</a></p>'
                    f'</div>'
                )

            # Download address if available
            download_addr = video_data.get('download_addr', {})
            dl_url_list = download_addr.get('url_list', [])
            if dl_url_list:
                content_parts.append(
                    f'<div class="douyin-download">'
                    f'<p>⬇️ <a href="{dl_url_list[0]}">下载视频</a></p>'
                    f'</div>'
                )

            type_label = 'video'

            # Whisper transcription. Best effort — works reliably from the agent
            # (home IP), and silently no-ops on prod where the douyinpic CDN
            # blocks server-side download.
            transcribe_src = (dl_url_list[0] if dl_url_list else nwm_url) or ''
            if transcribe_src:
                try:
                    from app.services.transcription_service import transcription_service
                    logger.info(f"Douyin: starting transcription")
                    transcript = await transcription_service.transcribe_url(
                        transcribe_src, referer='https://www.douyin.com/'
                    )
                    if transcript:
                        content_parts.append(
                            '<div class="douyin-transcript" style="margin-top:16px;'
                            'padding:12px;background:#f6f6f6;border-radius:8px;">'
                            '<div style="font-size:12px;color:#888;margin-bottom:6px;">📝 视频字幕（AI 转写）</div>'
                            f'<p style="white-space:pre-wrap;">{transcript}</p>'
                            '</div>'
                        )
                        logger.info(f"Douyin transcript appended ({len(transcript)} chars)")
                except Exception as e:
                    logger.warning(f"Douyin transcription failed (non-fatal): {e}")
        
        # Music metadata
        if music_title:
            content_parts.append(
                f'<div class="douyin-music">'
                f'🎵 {music_title}'
            )
            if music_author:
                content_parts.append(f' — {music_author}')
            content_parts.append('</div>')
        
        # Statistics
        content_parts.append(
            f'<div class="douyin-stats" style="color:#999;font-size:12px;">'
            f'👍 {digg_count} · 💬 {comment_count} · 🔄 {share_count}'
        )
        if hashtags:
            content_parts.append(
                f' · {" ".join("#"+t for t in hashtags)}'
            )
        content_parts.append('</div>')
        
        content_html = '\n'.join(content_parts)
        
        # OG metadata
        og_meta = {
            'title': desc[:100] if desc else f'抖音{type_label}_{aweme_id}',
            'author': author_name or author_unique_id,
            'image': cover_url,
            'description': desc[:500] if desc else '',
            'published_time': str(create_time),
            'site_name': '抖音',
        }
        
        author_display = author_name or author_unique_id or 'unknown'
        
        logger.info(
            f"Douyin parse SUCCESS: type={type_label}, id={aweme_id}, "
            f"desc='{desc[:50]}', author='{author_display}', "
            f"likes={digg_count}, comments={comment_count}"
        )
        
        # Rewrite hotlink-protected CDN URLs (douyinpic / douyinvod) through proxy
        content_html = self._proxy_imgs_in_html(content_html)
        cover_url_final = self._proxy_url(cover_url) if cover_url else None

        return {
            'title': desc[:200] if desc else f'抖音{type_label}_{aweme_id}',
            'raw_html': content_html,
            'raw_content': content_html,
            'platform': 'douyin',
            'author': author_display,
            'cover_image': cover_url_final,
            'og_meta': og_meta,
        }

    async def _fetch_xhs(self, url: str) -> Dict:
        """Fetch Xiaohongshu (小红书) note content.

        URL forms:
        - https://xhslink.com/<short>  (302 → discovery/item or explore)
        - https://www.xiaohongshu.com/explore/<note_id>
        - https://www.xiaohongshu.com/discovery/item/<note_id>

        Strategy:
        1. curl_cffi with mobile UA + Chrome impersonate (covers TLS fingerprint)
        2. Parse window.__INITIAL_STATE__ inline JSON for full note data
        3. Playwright fallback if HTML doesn't carry usable state
        4. OG meta last resort (title/cover/snippet)
        """
        logger.info(f"Fetching XHS: {url}")

        # XHS sec_server redirects iPhone-mobile UAs to /404/sec_xxx (anti-scraping).
        # Desktop Chrome Mac UA passes — the page resolves to /explore/<id> with
        # full noteDetailMap in __INITIAL_STATE__.
        desktop_ua = (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        )

        # Load web_session cookie if user configured one (Settings → 小红书).
        # Empty when unconfigured — request stays anonymous, identical to before.
        from app.config_manager import get_effective_config
        xhs_cookie = (get_effective_config('xhs') or {}).get('web_session', '').strip()

        html = ""
        final_url = url
        try:
            from curl_cffi import requests as curl_requests
            headers = {
                'User-Agent': desktop_ua,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Referer': 'https://www.xiaohongshu.com/',
            }
            if xhs_cookie:
                headers['Cookie'] = f'web_session={xhs_cookie}'
            resp = curl_requests.get(
                url,
                headers=headers,
                impersonate='chrome124',
                timeout=20,
                allow_redirects=True,
            )
            html = resp.text or ""
            final_url = str(resp.url) if hasattr(resp, 'url') else url
            # If we got bounced to a sec / 404 page, drop the body — Playwright
            # may still have a shot, and we avoid feeding 404 HTML to the state
            # extractor (which then mis-reports "success" with no note).
            from urllib.parse import urlparse as _urlparse
            _parsed = _urlparse(final_url)
            if (
                _parsed.path.startswith('/404')
                or 'xhs_sec_server' in final_url
                or 'errorCode=-510001' in final_url
            ):
                logger.info(f"XHS curl_cffi got sec/404 page, trying Playwright. final={final_url[:120]}")
                html = ""
            else:
                logger.info(f"XHS curl_cffi: {len(html)} chars, final={final_url[:120]}")
        except Exception as e:
            logger.warning(f"XHS curl_cffi failed: {e}")

        # Try __INITIAL_STATE__ / __INITIAL_SSR_STATE__ from HTML
        note = _extract_xhs_state(html) if html else None

        # Fallback to Playwright if state parsing failed
        if note is None:
            note, html_pw, final_url_pw = await _xhs_playwright_fetch(url, desktop_ua, xhs_cookie)
            if html_pw:
                html = html_pw
            if final_url_pw:
                final_url = final_url_pw

        # Build OG meta dict from whatever HTML we have, for last-resort fields
        og_meta = {}
        if html:
            try:
                soup = BeautifulSoup(html, 'lxml')
                og_meta = self._extract_og_metadata(soup)
            except Exception:
                pass

        if note is None and not og_meta.get('title'):
            raise ValueError(
                "小红书拒绝了访问，可能是分享链 token 过期或服务器 IP 被风控。"
                "若多次失败，请到「设置 → 小红书」配置 web_session cookie。"
            )

        # ── Extract fields ──
        if note:
            title = (note.get('title') or '').strip()
            desc = (note.get('desc') or '').strip()
            author = ''
            user_info = note.get('user') or note.get('author') or {}
            if isinstance(user_info, dict):
                author = (user_info.get('nickname') or user_info.get('name') or '').strip()
            images = []
            for img in (note.get('imageList') or note.get('images_list') or note.get('images') or []):
                if isinstance(img, dict):
                    src = img.get('url') or img.get('urlDefault') or img.get('url_default') or ''
                    if not src and isinstance(img.get('infoList'), list):
                        for info in img['infoList']:
                            if isinstance(info, dict) and info.get('url'):
                                src = info['url']
                                break
                    if src:
                        images.append(src)
                elif isinstance(img, str):
                    images.append(img)
            tags = []
            for tag in (note.get('tagList') or note.get('tag_list') or []):
                if isinstance(tag, dict) and tag.get('name'):
                    tags.append(tag['name'])
            note_type = note.get('type') or ('video' if note.get('video') else 'normal')
            # If video note: try to extract a playable URL and transcribe.
            xhs_video_url = _extract_xhs_video_url(note)
        else:
            xhs_video_url = None
            title = og_meta.get('title', '')
            desc = og_meta.get('description', '')
            author = og_meta.get('author', '')
            images = [og_meta['image']] if og_meta.get('image') else []
            tags = []
            note_type = 'normal'

        # Cover: first image, or OG image
        cover_url = (images[0] if images else '') or og_meta.get('image', '')

        # Build content HTML
        parts = []
        if title and title != desc:
            parts.append(f'<h1>{title}</h1>')
        if desc:
            parts.append(f'<p>{desc}</p>')
        if images:
            parts.append('<div class="xhs-images">')
            for img_url in images:
                parts.append(f'<img src="{img_url}" alt="xhs image" />')
            parts.append('</div>')
        if tags:
            parts.append(
                '<div class="xhs-tags" style="color:#888;font-size:12px;">'
                + ' '.join(f'#{t}' for t in tags)
                + '</div>'
            )

        # If this is a video note, try transcription (best effort, ~10-30s extra)
        if xhs_video_url:
            try:
                from app.services.transcription_service import transcription_service
                logger.info(f"XHS video: starting transcription from {xhs_video_url[:80]}")
                transcript = await transcription_service.transcribe_url(
                    xhs_video_url, referer='https://www.xiaohongshu.com/'
                )
                if transcript:
                    parts.append(
                        '<div class="xhs-transcript" style="margin-top:16px;'
                        'padding:12px;background:#f6f6f6;border-radius:8px;">'
                        '<div style="font-size:12px;color:#888;margin-bottom:6px;">📝 视频字幕（AI 转写）</div>'
                        f'<p style="white-space:pre-wrap;">{transcript}</p>'
                        '</div>'
                    )
                    logger.info(f"XHS video transcript appended ({len(transcript)} chars)")
            except Exception as e:
                logger.warning(f"XHS transcription failed (non-fatal): {e}")

        content_html = '\n'.join(parts)

        display_title = title or (desc[:80] if desc else '小红书笔记')

        # Rewrite hotlink-protected CDN URLs through /api/images/proxy
        content_html = self._proxy_imgs_in_html(content_html)
        cover_url_final = self._proxy_url(cover_url) if cover_url else None

        logger.info(
            f"XHS parse SUCCESS: type={note_type}, "
            f"title={display_title[:40]}, images={len(images)}"
        )

        return {
            'title': display_title[:200],
            'raw_html': content_html,
            'raw_content': content_html,
            'platform': 'xhs',
            'author': author or 'unknown',
            'cover_image': cover_url_final,
            'og_meta': og_meta,
        }

    def clean_to_markdown(self, html_content: str, platform: str = 'other') -> str:
        """Convert cleaned HTML to readable markdown."""
        if not html_content:
            return ""
        
        # For WeChat, rewrite mmbiz.qpic.cn image URLs to proxy endpoint
        # instead of deleting them, so images display correctly in the browser
        if platform == 'wechat':
            soup = BeautifulSoup(html_content, 'lxml')
            for img in soup.find_all('img'):
                src = img.get('data-src') or img.get('data-original') or img.get('src', '')
                if src and 'mmbiz.qpic.cn' in src:
                    from urllib.parse import quote
                    img['src'] = f"/api/images/proxy?url={quote(src, safe='')}"
                elif not img.get('src'):
                    img.decompose()
            html_content = str(soup)
        
        # markdownify: when using convert, don't set strip
        markdown = md_convert(
            html_content,
            heading_style='ATX',
            bullets='-',
        )
        
        # Clean up excessive whitespace
        markdown = re.sub(r'\n{4,}', '\n\n\n', markdown)
        markdown = markdown.strip()
        
        # If markdown is empty/very short, try extracting text directly
        if len(markdown) < 100:
            soup = BeautifulSoup(html_content, 'lxml')
            
            # Extract all text, preserving paragraph structure
            paragraphs = []
            for tag in soup.find_all(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote']):
                text = tag.get_text(strip=True)
                if text and len(text) > 5:
                    # Rewrite WeChat image URLs to proxy endpoint
                    imgs = tag.find_all('img') if platform == 'wechat' else []
                    for img in imgs:
                        src = img.get('data-src') or img.get('src', '')
                        if src and 'mmbiz.qpic.cn' in src:
                            from urllib.parse import quote
                            src = f"/api/images/proxy?url={quote(src, safe='')}"
                            paragraphs.append(f'![]({src})')
                    if not imgs or text:
                        if tag.name.startswith('h'):
                            paragraphs.append(f"\n## {text}")
                        elif tag.name == 'blockquote':
                            paragraphs.append(f"\n> {text}")
                        elif tag.name == 'li':
                            paragraphs.append(f"- {text}")
                        else:
                            paragraphs.append(text)
            
            if paragraphs:
                markdown = '\n\n'.join(paragraphs)
            else:
                # Straight text extraction
                text = soup.get_text(separator='\n', strip=True)
                lines = [l.strip() for l in text.split('\n') if l.strip()]
                markdown = '\n\n'.join(lines)
        
        return markdown
    
    def count_words(self, text: str) -> int:
        """Count words/characters in text."""
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        english_words = len(re.findall(r'[a-zA-Z]+', text))
        return chinese_chars + english_words
    
    def estimate_reading_time(self, word_count: int) -> int:
        """Estimate reading time in minutes."""
        return max(1, round(word_count / 300))


# ============================================================
#  Xiaohongshu (小红书) helpers
# ============================================================

_XHS_STATE_PATTERNS = (
    # Most common: a JS object literal assignment, possibly with unquoted keys.
    re.compile(r'window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>', re.DOTALL),
    re.compile(r'window\.__INITIAL_SSR_STATE__\s*=\s*({.*?})\s*</script>', re.DOTALL),
    re.compile(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', re.DOTALL),
)


def _extract_xhs_state(html: str) -> Optional[dict]:
    """Pull the note dict out of XHS' __INITIAL_STATE__ inline JSON.

    XHS sometimes emits a JS object literal (unquoted keys, `undefined` values,
    single quotes) rather than strict JSON. We try strict json.loads first,
    fall back to json_repair which handles these JS-isms.
    """
    if not html or '__INITIAL_STATE__' not in html and '__INITIAL_SSR_STATE__' not in html:
        return None

    blob: Optional[str] = None
    for pat in _XHS_STATE_PATTERNS:
        m = pat.search(html)
        if m:
            blob = m.group(1)
            break
    if not blob:
        return None

    state = None
    try:
        state = json.loads(blob)
    except Exception:
        try:
            from json_repair import repair_json
            state = json.loads(repair_json(blob, return_objects=False))
        except Exception as e:
            logger.warning(f"XHS state JSON repair failed: {e}")
            return None

    if not isinstance(state, dict):
        return None

    # Walk common shapes to find the note object.
    # Observed: state.note.noteDetailMap[<id>].note  OR  state.note.firstNoteId + map
    note_state = state.get('note') or state.get('noteData') or {}
    if isinstance(note_state, dict):
        nd_map = note_state.get('noteDetailMap') or note_state.get('note_detail_map') or {}
        if isinstance(nd_map, dict):
            for entry in nd_map.values():
                if isinstance(entry, dict):
                    n = entry.get('note') or entry.get('noteData') or entry
                    if isinstance(n, dict) and (n.get('noteId') or n.get('note_id') or n.get('title') or n.get('desc')):
                        return n
        # Sometimes the note is directly under state.note
        if note_state.get('noteId') or note_state.get('title') or note_state.get('desc'):
            return note_state
    return None


def _extract_xhs_video_url(note: dict) -> Optional[str]:
    """Find a playable video URL inside an XHS note dict (best effort).

    XHS video shape commonly looks like:
      note.video.media.stream.h264[0].master_url
      note.video.consumer.url_list[0]
      note.video.url
    Returns None for image notes or when extraction fails.
    """
    if not isinstance(note, dict):
        return None
    v = note.get('video') or {}
    if not isinstance(v, dict):
        return None
    # Try common paths in order
    media = v.get('media') or {}
    stream = (media.get('stream') or {}) if isinstance(media, dict) else {}
    for codec in ('h264', 'h265', 'av1'):
        arr = stream.get(codec) if isinstance(stream, dict) else None
        if isinstance(arr, list) and arr:
            entry = arr[0]
            url = (entry.get('master_url') if isinstance(entry, dict) else None) \
                  or (entry.get('backup_urls', [None])[0] if isinstance(entry, dict) else None)
            if url:
                return url
    # consumer.url_list
    consumer = v.get('consumer') or {}
    if isinstance(consumer, dict):
        urls = consumer.get('url_list') or []
        if urls:
            return urls[0]
    # plain url
    if isinstance(v.get('url'), str):
        return v['url']
    return None


async def _xhs_playwright_fetch(url: str, ua: str, cookie: Optional[str] = None):
    """Playwright fallback for XHS: renders the page with desktop UA, captures HTML.

    XHS reliably rejects iPhone-mobile UAs (sec_server redirect); desktop Chrome
    UA passes through to /explore/<id> with full noteDetailMap state.

    If `cookie` is provided, it's injected as web_session — same effect as a
    logged-in browser session and usually bypasses sec_server / -510001.

    Returns (note_dict_or_None, html, final_url).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None, "", url

    note = None
    html = ""
    final_url = url
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
            )
            ctx = await browser.new_context(
                user_agent=ua,
                viewport={'width': 1920, 'height': 1080},
                locale='zh-CN',
            )
            if cookie:
                await ctx.add_cookies([{
                    'name': 'web_session',
                    'value': cookie,
                    'domain': '.xiaohongshu.com',
                    'path': '/',
                }])
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                logger.warning(f"XHS playwright nav: {e}")
            # Give SSR/CSR a moment to populate __INITIAL_STATE__
            import asyncio as _a
            await _a.sleep(4)
            try:
                final_url = await page.evaluate("location.href") or url
            except Exception:
                pass
            html = await page.content()
            await browser.close()
    except Exception as e:
        logger.warning(f"XHS playwright failed: {e}")

    if html:
        note = _extract_xhs_state(html)
    return note, html, final_url


parser_service = ParserService()
