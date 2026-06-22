"""Trove AI — read-later + AI knowledge base for the Chinese internet"""
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from urllib.parse import urlparse
import asyncio
import logging
import httpx

from app.config import get_settings
from app.database import init_db
from .routers import articles, knowledge, learning, system, assistant, auth, users, wechat, review, research, sync, mcp, concepts

settings = get_settings()
logger = logging.getLogger("trove.auto_backfill")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False  # Don't duplicate to parent loggers


async def auto_backfill_embeddings():
    """Background task: periodically check for articles without embeddings and backfill them."""
    # Wait for everything to be fully initialized before first run
    await asyncio.sleep(60)
    
    while True:
        try:
            from app.database import async_session
            from sqlalchemy import select
            from app.models.article import Article
            from app.services.ai_service import llm_service
            
            async with async_session() as db:
                result = await db.execute(select(Article).where(Article.embedding.is_(None)))
                articles = result.scalars().all()
                
                if articles:
                    logger.info(f"Auto-backfill: found {len(articles)} articles without embeddings")
                    count = 0
                    for article in articles:
                        try:
                            content = f"{article.title or ''}. {article.summary or ''}. {(article.plain_text or article.raw_content or '')[:2000]}"
                            embedding = await llm_service.get_embedding(content)
                            article.embedding = embedding
                            count += 1
                            logger.info(f"Auto-backfill: generated embedding for article {article.id} ({article.title[:40]})")
                        except Exception as e:
                            logger.error(f"Auto-backfill: embedding failed for {article.id}: {e}")
                    
                    await db.commit()
                    logger.info(f"Auto-backfill: completed {count}/{len(articles)} embeddings")
                else:
                    # No missing embeddings, sleep longer
                    pass
        except Exception as e:
            logger.error(f"Auto-backfill scan error: {e}", exc_info=True)
        
        # Scan every 5 minutes
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    await init_db()

    # Start auto-backfill background task
    backfill_task = asyncio.create_task(auto_backfill_embeddings())
    logger.info("Auto-backfill background task started (scans every 5 minutes)")

    # Start periodic review cron loop
    from app.services.review_service import review_cron_loop
    review_task = asyncio.create_task(review_cron_loop())
    logger.info("Review cron loop started (60s tick)")

    print(f"🚀 Trove AI started successfully")
    yield
    # Shutdown
    backfill_task.cancel()
    review_task.cancel()
    print(f"👋 Trove AI shutting down")


app = FastAPI(
    title="Trove AI",
    description="Trove AI — read-later + AI knowledge base for the Chinese internet",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:80"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Handle OPTIONS preflight for ALL routes (fixes CORS preflight 405)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

class OptionsHandler(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return Response(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": "http://localhost:3000",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type, *",
                    "Access-Control-Max-Age": "86400",
                }
            )
        return await call_next(request)

app.add_middleware(OptionsHandler)

# Routes
app.include_router(articles.router)
app.include_router(knowledge.router)
app.include_router(learning.router)
app.include_router(system.router)
app.include_router(assistant.router, prefix="/api")

# Auth & User management
app.include_router(auth.router)
app.include_router(users.router)

# WeChat bot binding API
app.include_router(wechat.router)

# Review schedule API
app.include_router(review.router)

# Agentic research (SSE stream)
app.include_router(research.router)

# Obsidian sync API (Obsidian plugin pulls articles to local vault)
app.include_router(sync.router)

# MCP server — expose knowledge base to external AI agents
app.include_router(mcp.router)

# Concept synthesis pages
app.include_router(concepts.router)

# Allowed image proxy domains (anti-SSRF protection)
ALLOWED_IMAGE_DOMAINS = {
    'mmbiz.qpic.cn',       # WeChat MP CDN
    'mmbiz.qlogo.cn',      # WeChat MP logo CDN
    'mmecoa.qpic.cn',      # WeChat alternate CDN
    'xhscdn.com',          # XHS image CDN (subdomains via endswith match)
    'douyinpic.com',       # Douyin image CDN
    'douyinvod.com',       # Douyin video/thumb CDN
}


@app.get("/api/images/proxy")
async def proxy_image(url: str = Query(..., description="Original image URL to proxy")):
    """Proxy images from blocked CDNs (e.g. WeChat mmbiz) with proper Referer headers."""
    # Validate domain
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    domain = parsed.hostname or ""
    # Allow any subdomain of the trusted domains
    trusted = any(
        domain == allowed or domain.endswith('.' + allowed)
        for allowed in ALLOWED_IMAGE_DOMAINS
    )
    if not trusted:
        raise HTTPException(status_code=403, detail=f"Domain not allowed: {domain}")

    # Determine correct Referer based on domain
    if 'qpic.cn' in domain or 'qlogo.cn' in domain:
        referer = 'https://mp.weixin.qq.com/'
    elif 'xhscdn.com' in domain:
        referer = 'https://www.xiaohongshu.com/'
    elif 'douyinpic.com' in domain or 'douyinvod.com' in domain:
        referer = 'https://www.douyin.com/'
    else:
        referer = 'https://mp.weixin.qq.com/'

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            proxy_resp = await client.get(
                url,
                headers={
                    'User-Agent': (
                        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/120.0.0.0 Safari/537.36'
                    ),
                    'Referer': referer,
                    'Accept': 'image/webp,image/avif,image/*,*/*;q=0.8',
                },
            )
            proxy_resp.raise_for_status()

        content_type = proxy_resp.headers.get('content-type', 'image/jpeg')
        return StreamingResponse(
            content=proxy_resp.aiter_bytes(),
            media_type=content_type,
            headers={
                'Cache-Control': 'public, max-age=86400',  # 1 day
            },
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream error")
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Failed to fetch image")


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "app": "Trove AI"}
