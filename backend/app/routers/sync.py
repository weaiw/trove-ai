"""Sync API — endpoints used by local Obsidian-sync agent.

Two endpoints:
- POST /api/sync/issue-token  (JWT auth) — mint a long-lived (1y) JWT bound to
  current user, for the local sync agent to use.
- GET  /api/sync/articles     (JWT auth) — paginated list of THIS USER'S AI-
  processed articles. Local agent writes each to ~/Obsidian/<vault>/Trove AI/.

Design: docs/obsidian-sync-design.md
"""
import io
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, create_long_lived_token
from app.models import Article, KnowledgeEdge, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sync", tags=["sync"])


# ============================================================
#  Request / response models
# ============================================================

class IssueTokenResponse(BaseModel):
    token: str
    purpose: str = "obsidian-sync"
    expires_in_days: int = 365
    user_id: str
    username: str
    token_version: int


class RevokeResponse(BaseModel):
    revoked: bool
    new_token_version: int
    message: str


class SyncArticleOut(BaseModel):
    id: str
    title: str
    summary: Optional[str] = None
    key_points: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    author: Optional[str] = None
    source_url: Optional[str] = None
    source_platform: Optional[str] = None
    content_type: str = "article"
    clean_content: str = ""
    folder_path: Optional[str] = None
    related_article_ids: List[str] = Field(default_factory=list)
    reading_time: int = 0
    created_at: str
    updated_at: str


class SyncArticlesResponse(BaseModel):
    articles: List[SyncArticleOut]
    next_cursor: Optional[str] = None
    server_time: str


class SyncUpdateRequest(BaseModel):
    """Obsidian 插件回写本地编辑(双向同步)。"""
    title: Optional[str] = None
    clean_content: Optional[str] = None
    base_updated_at: Optional[datetime] = None  # 上次拉取看到的 server updated_at,冲突检测用


class SyncUpdateResponse(BaseModel):
    id: str
    updated_at: str
    changed: bool = False
    reembedded: bool = False


class SyncStatsResponse(BaseModel):
    total_articles: int
    eligible_articles: int  # AI-processed, sync-ready


# Rewrite relative `/api/images/proxy?url=...` references in markdown into
# absolute URLs based on the request's host. Without this, Obsidian renders
# them as broken local-file links. With this, the .md files in local vault
# can fetch images directly from the Trove AI server while it's reachable.
# Only matches inside markdown link/image parens — `(/api/...)` — so it
# never rewrites already-absolute URLs (which are `(http://host/api/...)`).
_RELATIVE_IMG_RE = re.compile(r"\((/api/images/proxy\?[^)\s\"']+)\)")


def _absolutize_image_urls(markdown: str, base: str) -> str:
    if not markdown or "/api/images/proxy" not in markdown:
        return markdown
    return _RELATIVE_IMG_RE.sub(lambda m: f"({base}{m.group(1)})", markdown)


# ============================================================
#  Endpoints
# ============================================================

@router.post("/issue-token", response_model=IssueTokenResponse)
async def issue_sync_token(
    current_user: User = Depends(get_current_user),
):
    """Mint a long-lived (365d) JWT for THIS user's local sync agent.
    Returned plaintext is shown once — store it securely on the local machine.

    The token embeds the user's current `sync_token_version`. Calling
    POST /api/sync/revoke-all-tokens bumps that counter and invalidates this
    (and every other previously-issued) sync token immediately.
    """
    current_version = current_user.sync_token_version or 0
    token = create_long_lived_token(
        user_id=current_user.id,
        username=current_user.username,
        is_super_admin=current_user.is_super_admin,
        purpose="obsidian-sync",
        expires_days=365,
        token_version=current_version,
    )
    return IssueTokenResponse(
        token=token,
        user_id=str(current_user.id),
        username=current_user.username,
        token_version=current_version,
    )


@router.post("/revoke-all-tokens", response_model=RevokeResponse)
async def revoke_all_sync_tokens(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bump sync_token_version → every previously-issued sync token for this
    user is rejected immediately. Useful when a token leaks or you simply
    want to rotate. Login JWTs (24h) are unaffected — only sync purpose
    tokens get gated."""
    current_user.sync_token_version = (current_user.sync_token_version or 0) + 1
    await db.commit()
    return RevokeResponse(
        revoked=True,
        new_token_version=current_user.sync_token_version,
        message="所有同步 Token 已撤销，请重新生成。",
    )


@router.get("/articles", response_model=SyncArticlesResponse)
async def list_sync_articles(
    request: Request,
    since: Optional[datetime] = Query(
        None,
        description="ISO8601 — only articles with updated_at > since are returned.",
    ),
    cursor: Optional[str] = Query(
        None,
        description="updated_at|id pair from previous page's last article.",
    ),
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Paginated articles ready for local Obsidian sync.

    Filter: only articles that have completed AI processing
    (summary IS NOT NULL AND embedding IS NOT NULL) — avoids leaking placeholder
    titles when an article is mid-fetch.

    Pagination: stable ORDER BY (updated_at, id). `cursor=<ts>|<id>` skips ahead.
    """
    conditions = [
        Article.user_id == current_user.id,
        Article.summary.is_not(None),
        Article.embedding.is_not(None),
    ]
    if since:
        conditions.append(Article.updated_at > since)
    if cursor:
        try:
            ts_str, id_str = cursor.split("|", 1)
            cur_ts = datetime.fromisoformat(ts_str)
            cur_id = UUID(id_str)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cursor format")
        # (updated_at, id) > (cur_ts, cur_id) — keyset pagination
        conditions.append(
            (Article.updated_at > cur_ts)
            | and_(Article.updated_at == cur_ts, Article.id > cur_id)
        )

    stmt = (
        select(Article)
        .where(*conditions)
        .order_by(Article.updated_at, Article.id)
        .limit(limit)
    )
    result = await db.execute(stmt)
    articles = result.scalars().all()

    if not articles:
        return SyncArticlesResponse(
            articles=[],
            next_cursor=None,
            server_time=datetime.now(timezone.utc).isoformat(),
        )

    # Fetch related_article_ids in one go (knowledge graph edges)
    article_ids = [a.id for a in articles]
    edge_stmt = select(KnowledgeEdge).where(
        KnowledgeEdge.source_article_id.in_(article_ids),
        KnowledgeEdge.user_id == current_user.id,
    )
    edge_result = await db.execute(edge_stmt)
    edges = edge_result.scalars().all()
    related_map: dict[UUID, list[UUID]] = {}
    for e in edges:
        related_map.setdefault(e.source_article_id, []).append(e.target_article_id)

    # Use X-Forwarded-* if present (we're behind nginx), else request.url.
    fwd_proto = request.headers.get("X-Forwarded-Proto")
    fwd_host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host")
    if fwd_proto and fwd_host:
        public_base = f"{fwd_proto}://{fwd_host}"
    else:
        public_base = f"{request.url.scheme}://{request.url.netloc}"

    out: List[SyncArticleOut] = []
    for a in articles:
        raw_md = a.clean_content or a.raw_content or ""
        clean_md = _absolutize_image_urls(raw_md, public_base)
        out.append(
            SyncArticleOut(
                id=str(a.id),
                title=a.title or "Untitled",
                summary=a.summary,
                key_points=a.key_points or [],
                tags=[t.name for t in (a.tags or [])],
                author=a.author,
                source_url=a.url,
                source_platform=a.source_platform,
                content_type=a.content_type or "article",
                clean_content=clean_md,
                folder_path=(a.folder.name if a.folder else None),
                related_article_ids=[str(rid) for rid in related_map.get(a.id, [])],
                reading_time=a.reading_time or 0,
                created_at=a.created_at.isoformat() if a.created_at else "",
                updated_at=a.updated_at.isoformat() if a.updated_at else "",
            )
        )

    # Build next cursor from the last article if we hit the limit
    next_cursor = None
    if len(articles) == limit:
        last = articles[-1]
        next_cursor = f"{last.updated_at.isoformat()}|{last.id}"

    return SyncArticlesResponse(
        articles=out,
        next_cursor=next_cursor,
        server_time=datetime.now(timezone.utc).isoformat(),
    )


@router.patch("/articles/{article_id}", response_model=SyncUpdateResponse)
async def update_sync_article(
    article_id: str,
    body: SyncUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """回写 Obsidian 本地编辑(双向同步)。冲突=LWW+时间戳护栏:插件给 base_updated_at,
    若服务端 updated_at 已更新(>2s)→ 409 不覆盖。改 clean_content 会重算 embedding。"""
    from datetime import timedelta
    try:
        aid = UUID(article_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid article id")

    article = await db.get(Article, aid)
    if not article or article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")

    if body.base_updated_at and article.updated_at:
        srv = article.updated_at
        base = body.base_updated_at
        if srv.tzinfo is None:
            srv = srv.replace(tzinfo=timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        if srv > base + timedelta(seconds=2):
            raise HTTPException(
                status_code=409,
                detail=f"conflict: server newer than base ({srv.isoformat()})",
            )

    changed = False
    reembedded = False
    if body.title is not None:
        new_title = body.title.strip()
        if new_title and new_title != article.title:
            article.title = new_title[:500]
            changed = True
    if body.clean_content is not None and body.clean_content != (article.clean_content or ""):
        article.clean_content = body.clean_content
        article.plain_text = body.clean_content
        from app.services.parser_service import parser_service
        article.word_count = parser_service.count_words(body.clean_content)
        if article.content_type == "note":
            article.raw_content = body.clean_content
        changed = True
        try:
            from app.services.ai_service import llm_service
            emb = await llm_service.get_embedding(
                f"{article.title}. {article.summary or ''}. {body.clean_content[:2000]}"
            )
            article.embedding = emb
            reembedded = True
        except Exception as e:
            logger.warning(f"re-embed after sync writeback failed for {aid}: {e}")

    if changed:
        await db.commit()
        await db.refresh(article)

    return SyncUpdateResponse(
        id=str(article.id),
        updated_at=article.updated_at.isoformat() if article.updated_at else "",
        changed=changed,
        reembedded=reembedded,
    )


# Plugin artifacts live under backend/app/static/obsidian-plugin/ — they're the
# build output of the trove-sync-obsidian repo, copied into the backend image
# at build time so the running backend can serve them.
PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "obsidian-plugin")
PLUGIN_FILES = ["main.js", "manifest.json", "styles.css"]


@router.get("/plugin-download")
async def download_plugin():
    """Stream a zip containing the Trove AI Sync Obsidian plugin. Public
    (no auth) — the plugin source is meant to be installable by anyone."""
    missing = [f for f in PLUGIN_FILES if not os.path.exists(os.path.join(PLUGIN_DIR, f))]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Plugin artifacts not bundled in image: {missing}",
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in PLUGIN_FILES:
            src = os.path.join(PLUGIN_DIR, name)
            # Nest one folder deep so unzipping creates a `trove-sync/` dir
            z.write(src, arcname=f"trove-sync/{name}")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="trove-sync-obsidian-plugin.zip"'
        },
    )


@router.get("/stats", response_model=SyncStatsResponse)
async def sync_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stats for the frontend's Obsidian-backup card."""
    total = await db.scalar(
        select(func.count()).select_from(Article).where(Article.user_id == current_user.id)
    )
    eligible = await db.scalar(
        select(func.count())
        .select_from(Article)
        .where(
            Article.user_id == current_user.id,
            Article.summary.is_not(None),
            Article.embedding.is_not(None),
        )
    )
    return SyncStatsResponse(
        total_articles=total or 0,
        eligible_articles=eligible or 0,
    )
