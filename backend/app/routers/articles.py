"""Article management API routes."""
import os
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, BackgroundTasks, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, desc, text, update
from typing import Optional, List
from uuid import UUID

from app.database import get_db
from app.dependencies import get_current_user
from app.models.article import Article, Tag, Folder, ArticleStatus, KnowledgeEdge, article_tags
from app.models.user import User
from app.schemas.article import (
    ArticleCreate, ArticleBatchCreate, ArticleManualCreate, ArticleUpdate, NoteCreate,
    ArticleResponse, ArticleDetailResponse, ArticleListResponse,
    AIProcessResponse, SearchRequest,
    SparkCreateRequest, SparkResponse, SparkSectionResponse,
    FileUploadResponse, ArticleTagsUpdate,
)
from app.services.parser_service import parser_service, extract_url_from_text
from app.services.ai_service import llm_service
from app.services.graph_service import graph_service
from app.services.spark_service import generate_article

router = APIRouter(prefix="/api/articles", tags=["articles"])


PROACTIVE_DISTANCE_THRESHOLD = 0.45  # bge-m3 cosine distance; lower = closer match
PROACTIVE_PUBLIC_BASE = os.getenv("TROVE_PUBLIC_BASE", "http://localhost")


def _content_hash(raw: Optional[str]) -> Optional[str]:
    """SHA256 of fetched raw_content (stripped). 去重「同内容不同链接」。空返回 None。"""
    s = (raw or "").strip()
    return hashlib.sha256(s.encode("utf-8")).hexdigest() if s else None


async def _detect_contradictions_and_push(db, article_id) -> bool:
    """两步入库 Step-2:把新文章对照已有知识找观点对立/事实冲突/结论矛盾,
    建 contradicts 边并微信提醒。返回是否推送了冲突提醒(供调用方决定是否再发相似提醒)。"""
    import logging
    from sqlalchemy import text as sql_text
    from app.models import WechatAccount
    from app.services.ai_service import llm_service, _parse_llm_json
    logger = logging.getLogger("trove.background")

    article = await db.get(Article, article_id)
    if not article or article.embedding is None or not (article.summary or article.title):
        return False

    emb_str = "[" + ",".join(str(v) for v in article.embedding) + "]"
    rows = (await db.execute(sql_text(f"""
        SELECT id, title, summary FROM articles
        WHERE embedding IS NOT NULL AND user_id = :uid AND id != :aid
        ORDER BY embedding <-> '{emb_str}'::vector
        LIMIT 5
    """), {"uid": article.user_id, "aid": article_id})).fetchall()
    if not rows:
        return False

    candidates = [{"id": str(r[0]), "title": r[1] or "", "summary": (r[2] or "")[:200]} for r in rows]
    cand_block = "\n".join(
        f'{i+1}. [id={c["id"]}] {c["title"]} — {c["summary"]}' for i, c in enumerate(candidates)
    )
    prompt = f"""新文章：
标题：{article.title}
摘要：{article.summary or ''}
要点：{article.key_points or []}

已有相关文章：
{cand_block}

判断新文章是否与某些已有文章存在**观点对立 / 事实冲突 / 结论矛盾**（仅主题相似不算）。
严格返回 JSON（不要 markdown 围栏）：{{"contradictions":[{{"id":"已有文章id","reason":"一句话冲突点"}}]}}
确无矛盾就返回 {{"contradictions":[]}}。"""
    try:
        raw = await llm_service._chat(
            [{"role": "system", "content": "你是严谨的知识库分析助手，只在确有矛盾时报告，宁缺毋滥。"},
             {"role": "user", "content": prompt}],
            temperature=0.2,
        )
    except Exception as e:
        logger.warning(f"contradiction LLM failed for {article_id}: {e}")
        return False

    parsed = _parse_llm_json(raw) or {}
    valid_ids = {c["id"] for c in candidates}
    conflicts = [
        c for c in (parsed.get("contradictions") or [])
        if isinstance(c, dict) and c.get("id") in valid_ids
    ][:3]
    if not conflicts:
        return False

    id_title = {c["id"]: c["title"] for c in candidates}
    for c in conflicts:
        try:
            tid = UUID(c["id"])
        except (ValueError, TypeError):
            continue
        exists = await db.execute(
            select(KnowledgeEdge).where(
                KnowledgeEdge.source_article_id == article_id,
                KnowledgeEdge.target_article_id == tid,
            )
        )
        if exists.scalar_one_or_none():
            continue
        db.add(KnowledgeEdge(
            source_article_id=article_id,
            target_article_id=tid,
            relation_type="contradicts",
            relation_desc=(c.get("reason") or "")[:500],
            weight=0.7,
            user_id=article.user_id,
        ))
    await db.commit()

    acct_r = await db.execute(
        select(WechatAccount).where(
            WechatAccount.user_id == article.user_id,
            WechatAccount.is_active.is_(True),
        )
    )
    acct = acct_r.scalar_one_or_none()
    if not acct:
        return False

    first = conflicts[0]
    deep_link = f"{PROACTIVE_PUBLIC_BASE}/read/{first['id']}"
    msg = (
        f"⚠️ 你新存的《{(article.title or '')[:30]}》跟之前的"
        f"《{id_title.get(first['id'], '')[:30]}》观点可能冲突：\n"
        f"{(first.get('reason') or '')[:60]}\n\n要不要打开对照看？{deep_link}"
    )
    import httpx
    from app.services.review_service import send_wechat
    async with httpx.AsyncClient(timeout=20.0) as client:
        await send_wechat(client, acct, msg)
    logger.info(f"contradiction: pushed to user={article.user_id} new={article_id} conflicts={len(conflicts)}")
    return True


async def _maybe_push_proactive_relation(db, article_id):
    """If the just-added article has a strongly-related sibling in the same
    user's library, push a brief WeChat message: 「你刚存的《X》跟之前的《Y》
    主题相似，要不要对照看？」+ deep link to /read/<sibling_id>.

    Hooks bot push only when user has an active wechat binding.
    """
    from sqlalchemy import text as sql_text
    from app.models import WechatAccount
    article = await db.get(Article, article_id)
    if not article or article.embedding is None:
        return

    emb_str = "[" + ",".join(str(v) for v in article.embedding) + "]"
    sim_sql = sql_text(f"""
        SELECT id, title, (embedding <-> '{emb_str}'::vector) AS distance
        FROM articles
        WHERE embedding IS NOT NULL
          AND user_id = :uid
          AND id != :aid
        ORDER BY embedding <-> '{emb_str}'::vector
        LIMIT 1
    """)
    r = await db.execute(sim_sql, {"uid": article.user_id, "aid": article_id})
    row = r.first()
    if not row:
        return  # no other articles in library yet
    sibling_id, sibling_title, distance = row
    if float(distance) >= PROACTIVE_DISTANCE_THRESHOLD:
        return  # not close enough; skip silently

    # Find user's bound wechat account
    acct_r = await db.execute(
        select(WechatAccount).where(
            WechatAccount.user_id == article.user_id,
            WechatAccount.is_active.is_(True),
        )
    )
    acct = acct_r.scalar_one_or_none()
    if not acct:
        return  # no bot to push through

    new_title = (article.title or "Untitled").strip()
    old_title = (sibling_title or "Untitled").strip()
    deep_link = f"{PROACTIVE_PUBLIC_BASE}/read/{sibling_id}"
    msg = (
        f"📌 你刚存的《{new_title[:30]}》跟之前的《{old_title[:30]}》"
        f"主题很相似（距离 {float(distance):.2f}）。\n\n"
        f"要不要打开对照看？{deep_link}"
    )
    import httpx
    from app.services.review_service import send_wechat
    async with httpx.AsyncClient(timeout=20.0) as client:
        await send_wechat(client, acct, msg)
    logger.info(
        f"proactive: pushed relation to user={article.user_id} "
        f"new={article_id} sibling={sibling_id} dist={float(distance):.3f}"
    )


async def process_article_background(article_id: UUID, raw_content: str, raw_html: str, url: str, db_session_factory):
    """Background task: AI process a newly added article."""
    import logging
    logger = logging.getLogger("trove.background")
    from app.database import async_session
    
    async with async_session() as db:
        try:
            article = await db.get(Article, article_id)
            if not article:
                return
            
            # Clean content to markdown (skip for spark - already generated markdown)
            platform = article.source_platform or parser_service.detect_platform(url)
            # Skip HTML-cleanup when raw_content is already markdown:
            # - spark: AI-generated markdown
            # - bilibili videos / other API-based fetchers: empty raw_html signals "already markdown"
            if platform == "spark" or not raw_html:
                clean_md = raw_content
            else:
                clean_md = parser_service.clean_to_markdown(raw_content, platform)
            plain_text = clean_md  # For search purposes
            
            article.clean_content = clean_md
            article.plain_text = plain_text
            # 内容哈希(基于抓取到的 raw_content),供「同内容不同链接」去重。
            if not article.content_hash:
                article.content_hash = _content_hash(raw_content)

            # AI parse — pass raw_html for richer context when plain_text is thin
            ai_result = await llm_service.parse_article(plain_text, url, raw_html)
            
            article.title = ai_result.get('title', article.title)
            article.summary = ai_result.get('summary', '')
            article.key_points = ai_result.get('key_points', [])
            # source_platform priority: URL domain (most accurate) > existing > AI guess > 'other'.
            # Don't let AI overwrite a domain-detected platform (AI prompt's enum is incomplete).
            detected = parser_service.detect_platform(url) if url else 'other'
            if article.source_platform in (None, '', 'other') and detected == 'other':
                article.source_platform = ai_result.get('source_platform', 'other')
            elif article.source_platform in (None, '',):
                article.source_platform = detected
            # Author: parser-extracted value (from platform metadata) wins over AI guess.
            # AI often returns "unknown" for short-text platforms (douyin/xhs) where the
            # author isn't in the textual content but IS in the structured metadata.
            ai_author = (ai_result.get('author') or '').strip()
            if not article.author and ai_author and ai_author.lower() != 'unknown':
                article.author = ai_author
            if not article.author:
                article.author = 'unknown'
            article.reading_time = ai_result.get('estimated_reading_minutes', 5)
            article.word_count = parser_service.count_words(plain_text)
            
            # Process tags
            ai_tags = ai_result.get('tags', [])
            for tag_name in ai_tags:
                # Find or create tag — 按用户隔离:只在该用户自己的标签里找
                result = await db.execute(
                    select(Tag).where(
                        func.lower(Tag.name) == tag_name.lower(),
                        Tag.user_id == article.user_id,
                    )
                )
                tag = result.scalar_one_or_none()
                if not tag:
                    tag = Tag(name=tag_name, is_ai_generated=True, user_id=article.user_id)
                    db.add(tag)
                    await db.flush()
                article.tags.append(tag)

            await db.commit()
            
            # Generate knowledge graph connections
            try:
                await graph_service.generate_graph(db, article_id)
                await db.commit()
            except Exception as graph_err:
                logger.warning(f"Graph generation error for {article_id}: {graph_err}")
            
            # Generate embedding for semantic search
            try:
                content_for_embedding = f"{article.title}. {article.summary or ''}. {plain_text[:2000]}"
                embedding = await llm_service.get_embedding(content_for_embedding)
                article.embedding = embedding
                await db.commit()
            except Exception as embed_err:
                logger.warning(f"Embedding generation error for {article_id}: {embed_err} (will be retried by auto-backfill)")

            # 概念页 stale / auto-update(需 embedding,故放在向量生成之后)。
            try:
                from app.services.concept_service import process_new_article
                await process_new_article(db, article.user_id, article_id, ai_tags)
            except Exception as e:
                logger.warning(f"concept hook failed for {article_id}: {e}")

            # Step-2 KB-grounded 分析 + 主动推送:先矛盾检测(建 contradicts 边 + 冲突提醒),
            # 有冲突推冲突、无冲突才退回普通相似提醒(择一,避免双推)。
            try:
                pushed = await _detect_contradictions_and_push(db, article_id)
                if not pushed:
                    await _maybe_push_proactive_relation(db, article_id)
            except Exception as e:
                logger.warning(f"proactive/contradiction push failed for {article_id}: {e}")

        except Exception as e:
            import traceback
            logger.error(f"Background processing FATAL for {article_id}: {e}")
            logger.error(traceback.format_exc())
            await db.rollback()


@router.post("", response_model=ArticleResponse, status_code=201)
async def create_article(
    data: ArticleCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a new article by URL. Background AI processing will be triggered."""

    # Normalize: pull the URL out of share text (抖音/头条 share blob, etc.)
    clean_url = extract_url_from_text(data.url) or data.url.strip()
    if not clean_url.startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail="Could not find a valid URL in input")

    # Check for duplicate (per-user)
    result = await db.execute(
        select(Article).where(Article.url == clean_url, Article.user_id == current_user.id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Article already exists")

    # Fetch and parse content
    try:
        content_data = await parser_service.fetch_content(clean_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {str(e)}")

    # Create article
    article = Article(
        url=clean_url,
        title=content_data['title'] or data.title or 'Untitled',
        raw_content=content_data['raw_content'],
        source_platform=content_data['platform'],
        author=content_data['author'],
        cover_image=content_data['cover_image'],
        folder_id=data.folder_id,
        user_id=current_user.id,
    )

    db.add(article)
    await db.commit()
    await db.refresh(article)

    # Trigger background AI processing
    background_tasks.add_task(
        process_article_background,
        article.id,
        content_data['raw_content'],
        content_data.get('raw_html', ''),
        clean_url,
        None
    )

    return article


@router.post("/batch", response_model=List[ArticleResponse], status_code=201)
async def batch_create_articles(
    data: ArticleBatchCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Batch add articles by URLs."""
    articles = []
    for raw in data.urls:
        url = extract_url_from_text(raw) or raw.strip()
        if not url.startswith(('http://', 'https://')):
            continue
        # Skip duplicates (per-user)
        result = await db.execute(
            select(Article).where(Article.url == url, Article.user_id == current_user.id)
        )
        if result.scalar_one_or_none():
            continue

        try:
            content_data = await parser_service.fetch_content(url)
        except Exception:
            continue

        article = Article(
            url=url,
            title=content_data['title'],
            raw_content=content_data['raw_content'],
            source_platform=content_data['platform'],
            author=content_data['author'],
            cover_image=content_data['cover_image'],
            user_id=current_user.id,
        )
        db.add(article)
        await db.flush()

        background_tasks.add_task(
            process_article_background, article.id,
            content_data['raw_content'], content_data.get('raw_html', ''), url, None
        )
        articles.append(article)
    
    await db.commit()
    for a in articles:
        await db.refresh(a)
    
    return articles


@router.patch("/batch-move", status_code=200)
async def batch_move_articles(
    article_ids: list[UUID] = Body(...),
    folder_id: Optional[UUID] = Body(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Batch move articles to a folder. folder_id=null moves to root."""
    # User isolation: superadmins can move any, normal users only own articles
    stmt = update(Article).where(Article.id.in_(article_ids))
    if not current_user.is_super_admin:
        stmt = stmt.where(Article.user_id == current_user.id)
    stmt = stmt.values(folder_id=folder_id)
    result = await db.execute(stmt)
    await db.commit()
    
    count = result.rowcount
    return {"message": f"Moved {count} articles", "count": count}


@router.post("/manual", response_model=ArticleResponse, status_code=201)
async def create_article_manual(
    data: ArticleManualCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create an article from manually pasted content (for platforms that block scraping)."""
    # Check for duplicate (per-user)
    result = await db.execute(
        select(Article).where(Article.url == data.url, Article.user_id == current_user.id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Article already exists")
    
    # Parse the pasted content - treat as HTML
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(data.content, 'lxml')
    extracted = soup.get_text(separator='\n', strip=True)
    
    # Create raw_content HTML from the pasted content
    raw_html = f"<h1>{data.title}</h1>\n{data.content}"
    raw_content = raw_html
    
    article = Article(
        url=data.url,
        title=data.title,
        raw_content=raw_content,
        source_platform=data.source_platform or parser_service.detect_platform(data.url),
        folder_id=data.folder_id,
        user_id=current_user.id,
    )
    
    db.add(article)
    await db.commit()
    await db.refresh(article)
    
    # Trigger background AI processing
    background_tasks.add_task(
        process_article_background,
        article.id,
        raw_content,
        raw_html,
        data.url,
        None
    )
    
    return article


@router.post("/notes", response_model=ArticleResponse, status_code=201)
async def create_note(
    data: NoteCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a Markdown note. Triggers full AI processing
    (summary / key_points / tags / knowledge graph / embedding)
    so notes are consistent with other ingest paths (URL / paste / upload)."""
    article = Article(
        url=None,
        content_type='note',
        title=data.title,
        clean_content=data.content,
        raw_content=data.content,
        source_platform='note',
        status='reading',
        word_count=len(data.content),
        folder_id=data.folder_id,
        user_id=current_user.id,
    )
    db.add(article)
    await db.commit()
    await db.refresh(article)

    # Trigger background AI processing. raw_html="" signals "already markdown"
    # so process_article_background skips HTML cleanup. note:// URL is synthetic.
    background_tasks.add_task(
        process_article_background,
        article.id,
        data.content,            # raw_content (markdown)
        "",                      # raw_html empty → treat as markdown
        f"note://{article.id}",  # synthetic url for AI context
        None,
    )
    return article


# ---- Spark: 一句话→文章生成 ----
@router.post("/spark", response_model=SparkResponse, status_code=201)
async def spark_create_article(
    data: SparkCreateRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate a full article from a single sentence using AI pipeline."""
    import logging
    logger = logging.getLogger(__name__)

    try:
        result = await generate_article(data.sentence)
    except Exception as e:
        logger.error(f"Spark pipeline failed: {e}")
        raise HTTPException(status_code=500, detail=f"Article generation failed: {str(e)}")

    # Use the sentence as URL (unique identifier for spark-generated articles)
    import time
    spark_url = f"spark://{data.sentence[:50]}-{int(time.time())}"

    article = Article(
        url=spark_url,
        title=result["title"],
        raw_content=result["content"],
        clean_content=result["content"],  # Already markdown, ready for reader
        plain_text=result["content"],
        source_platform="spark",
        author="Trove AI AI",
        word_count=len(result["content"]),
        user_id=current_user.id,
    )

    db.add(article)
    await db.commit()
    await db.refresh(article)

    # Trigger background AI processing for tags/summary/key_points
    background_tasks.add_task(
        process_article_background,
        article.id,
        result["content"],      # raw_content (markdown)
        result["content"],      # raw_html (pass markdown as fallback for AI context)
        spark_url,
        None,
    )

    return SparkResponse(
        id=str(article.id),
        title=result["title"],
        content=result["content"],
        sections=[
            SparkSectionResponse(
                heading=s["heading"],
                key_points=s["key_points"],
                content=s["content"],
            )
            for s in result.get("sections", [])
        ],
        steps_completed=result.get("steps_completed", []),
        status=result.get("status", "completed"),
    )


# ---- File Upload: MarkItDown conversion ----
ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".png", ".jpg", ".jpeg", ".txt", ".html",
    ".epub", ".csv", ".md",
}

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "image/png",
    "image/jpeg",
    "text/plain",
    "text/html",
    "application/epub+zip",
    "text/csv",
    "text/markdown",
    "application/zip",
}

# Extended mime types that should still be accepted
ADDITIONAL_MIME_TYPES = {
    "application/vnd.ms-excel",        # .xls
    "application/vnd.ms-powerpoint",   # .ppt
    "application/msword",              # .doc
    "text/x-markdown",
    "text/x-csv",
}


@router.post("/upload", response_model=FileUploadResponse, status_code=201)
async def upload_article_file(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    file: UploadFile = None,
):
    """Upload a file and convert it to an article using MarkItDown."""
    import logging
    import os
    import tempfile
    import time

    logger = logging.getLogger(__name__)

    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    # Validate extension
    _, ext = os.path.splitext(file.filename.lower())
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format: {ext}. Supported formats: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    # Read file content
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    # Image upload size cap: 2 MB
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    if ext in IMAGE_EXTS and len(file_bytes) > 2 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"图片不能超过 2MB（当前 {len(file_bytes) / 1024 / 1024:.2f}MB）",
        )

    logger.info(f"Upload received: {file.filename} ({len(file_bytes)} bytes, ext={ext})")

    # Save to temp file for conversion
    suffix = ext if ext else ".bin"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        # Try MarkItDown first
        markdown_content = None
        try:
            from markitdown import MarkItDown
            md_converter = MarkItDown()
            convert_result = md_converter.convert(tmp_path)
            markdown_content = convert_result.text_content
            logger.info(f"MarkItDown conversion successful: {len(markdown_content)} chars")
        except (ImportError, AttributeError, TypeError) as e:
            logger.warning(f"MarkItDown not available or not functional ({e}), using fallback")
        except Exception as e:
            logger.warning(f"MarkItDown conversion failed ({e}), using fallback")

        # Fallback: basic text extraction for text-based formats
        if not markdown_content or not markdown_content.strip():
            fallback_content = _fallback_file_read(tmp_path, ext, file_bytes)
            if fallback_content:
                markdown_content = fallback_content
                logger.info(f"Fallback conversion used: {len(markdown_content)} chars")

        if not markdown_content or not markdown_content.strip():
            raise HTTPException(status_code=400, detail="File conversion produced empty content")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"File conversion failed: {e}")
        raise HTTPException(status_code=422, detail=f"File conversion failed: {str(e)}")
    finally:
        # Clean up temp file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # Generate a URL-like identifier for the uploaded article
    safe_name = file.filename.replace(" ", "_")[:80]
    upload_url = f"upload://{safe_name}-{int(time.time())}"

    # Detect source platform from extension
    ext_platform_map = {
        ".pdf": "pdf",
        ".docx": "word",
        ".xlsx": "excel",
        ".csv": "excel",
        ".pptx": "powerpoint",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".txt": "text",
        ".html": "web",
        ".epub": "epub",
        ".md": "markdown",
    }
    source_platform = ext_platform_map.get(ext, "upload")

    # Use filename without extension as title
    title = os.path.splitext(file.filename)[0][:500] or "Untitled"

    article = Article(
        url=upload_url,
        title=title,
        raw_content=markdown_content,
        source_platform=source_platform,
        author="unknown",
        word_count=len(markdown_content),
        user_id=current_user.id,
    )

    db.add(article)
    await db.commit()
    await db.refresh(article)

    # Trigger background AI processing for tags/summary/key_points
    background_tasks.add_task(
        process_article_background,
        article.id,
        markdown_content,       # raw_content
        markdown_content,       # raw_html (pass markdown as fallback)
        upload_url,
        None,
    )

    return article


def _fallback_file_read(path: str, ext: str, file_bytes: bytes) -> str:
    """Basic text extraction fallback when MarkItDown is not available."""
    import os

    text_extensions = {".txt", ".md", ".csv", ".html", ".htm"}
    if ext in text_extensions:
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return file_bytes.decode("latin-1")
            except Exception:
                pass

    # For HTML, wrap in markdown-style
    if ext == ".html":
        try:
            content = file_bytes.decode("utf-8")
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, "lxml")
            text = soup.get_text(separator="\n", strip=True)
            title = soup.title.string if soup.title else ""
            md_parts = []
            if title:
                md_parts.append(f"# {title}\n")
            md_parts.append(text)
            return "\n".join(md_parts)
        except Exception:
            pass

    # For CSV, format as markdown table
    if ext == ".csv":
        try:
            import csv
            import io
            content = file_bytes.decode("utf-8")
            reader = csv.reader(io.StringIO(content))
            rows = list(reader)
            if rows:
                lines = []
                # Header
                lines.append("| " + " | ".join(rows[0]) + " |")
                lines.append("| " + " | ".join(["---"] * len(rows[0])) + " |")
                # Data rows
                for row in rows[1:]:
                    # Pad row to match header length
                    padded = row + [""] * (len(rows[0]) - len(row))
                    lines.append("| " + " | ".join(padded[:len(rows[0])]) + " |")
                return "\n".join(lines)
        except Exception:
            pass

    # Unsupported binary format without MarkItDown
    raise HTTPException(
        status_code=422,
        detail=f"MarkItDown is required to convert {ext} files. Please install markitdown[all] in the container.",
    )


@router.get("", response_model=ArticleListResponse)
async def list_articles(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    folder_id: Optional[UUID] = None,
    tag: Optional[str] = None,
    search: Optional[str] = None,
    source_platform: Optional[str] = None,
    search_mode: str = Query("semantic", regex="^(semantic|keyword)$"),
    sort: str = Query("created_at", regex="^(created_at|updated_at|title)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    username_query: str | None = Query(None, alias="username", description="Superadmin: filter by username"),
):
    """List articles with filtering, search, and pagination."""
    
    query = select(Article)
    count_query = select(func.count(Article.id))
    
    # User isolation: superadmin sees own by default, or specific user via ?username=xxx
    target_user_id = current_user.id
    if current_user.is_super_admin and username_query:
        user_result = await db.execute(select(User).where(User.username == username_query))
        target_user = user_result.scalar_one_or_none()
        target_user_id = target_user.id if target_user else current_user.id
    query = query.where(Article.user_id == target_user_id)
    count_query = count_query.where(Article.user_id == target_user_id)
    
    # Filters
    if status:
        if status == "favorite":
            query = query.where(Article.is_favorited == True)
            count_query = count_query.where(Article.is_favorited == True)
        else:
            query = query.where(Article.status == status)
            count_query = count_query.where(Article.status == status)
    
    if folder_id:
        query = query.where(Article.folder_id == folder_id)
        count_query = count_query.where(Article.folder_id == folder_id)
    
    if tag:
        query = query.join(article_tags).join(Tag).where(
            func.lower(Tag.name) == tag.lower()
        )
        count_query = count_query.join(article_tags).join(Tag).where(
            func.lower(Tag.name) == tag.lower()
        )
    
    # Search — semantic by default, keyword fallback
    if search:
        if search_mode == "semantic":
            try:
                query_embedding = await llm_service.get_embedding(search, emb_type="query")
                query = query.where(Article.embedding.isnot(None))
                query = query.order_by(Article.embedding.cosine_distance(query_embedding))
                count_query = count_query.where(Article.embedding.isnot(None))
            except Exception:
                # Fallback to keyword search on embedding failure
                search_filter = or_(
                    Article.title.ilike(f"%{search}%"),
                    Article.plain_text.ilike(f"%{search}%"),
                    Article.summary.ilike(f"%{search}%"),
                )
                query = query.where(search_filter).order_by(desc(Article.created_at))
                count_query = count_query.where(search_filter)
        else:
            search_filter = or_(
                Article.title.ilike(f"%{search}%"),
                Article.plain_text.ilike(f"%{search}%"),
                Article.summary.ilike(f"%{search}%"),
            )
            query = query.where(search_filter).order_by(desc(Article.created_at))
            count_query = count_query.where(search_filter)
    else:
        sort_col = getattr(Article, sort)
        query = query.order_by(desc(sort_col))

    # Source platform filter (case-insensitive)
    if source_platform:
        platform_filter = func.lower(Article.source_platform) == source_platform.lower()
        query = query.where(platform_filter)
        count_query = count_query.where(platform_filter)
    
    # Count
    total_result = await db.execute(count_query)
    total = total_result.scalar()
    
    # Sort (only when no search — search modes handle their own ordering)
    if not search:
        sort_col = getattr(Article, sort)
        query = query.order_by(desc(sort_col))
    
    # Paginate
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)
    
    result = await db.execute(query)
    articles = result.scalars().all()
    
    return ArticleListResponse(
        items=articles,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{article_id}", response_model=ArticleDetailResponse)
async def get_article(
    article_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get article detail with full content."""
    article = await db.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    
    # User isolation: normal users can only access their own articles
    if not current_user.is_super_admin and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")
    
    return article


@router.patch("/{article_id}", response_model=ArticleResponse)
async def update_article(
    article_id: UUID,
    data: ArticleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update article metadata."""
    article = await db.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    
    # User isolation: normal users can only update their own articles
    if not current_user.is_super_admin and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")
    
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(article, key, value)

    # For notes, clean_content IS the source of truth — mirror it into raw_content
    # so reprocess / AI extraction has something to work with.
    if 'clean_content' in update_data and article.content_type == 'note':
        article.raw_content = update_data['clean_content'] or ''
        article.word_count = len(update_data['clean_content'] or '')

    await db.commit()
    await db.refresh(article)
    return article


@router.delete("/{article_id}", status_code=204)
async def delete_article(
    article_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete an article."""
    article = await db.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    
    # User isolation: normal users can only delete their own articles
    if not current_user.is_super_admin and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")
    
    await db.delete(article)
    await db.commit()


@router.patch("/{article_id}/tags", response_model=ArticleResponse)
async def update_article_tags(
    article_id: UUID,
    data: ArticleTagsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Replace all tags on an article with the given tag IDs."""
    article = await db.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    # User isolation: normal users can only modify their own articles
    if not current_user.is_super_admin and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")

    # Clear existing tags
    article.tags.clear()

    # Look up each tag_id and add valid ones
    for tag_id in data.tag_ids:
        tag = await db.get(Tag, tag_id)
        if tag:
            article.tags.append(tag)

    await db.commit()
    await db.refresh(article)
    return article


@router.post("/{article_id}/reprocess", response_model=AIProcessResponse)
async def reprocess_article(
    article_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-run AI processing on an article."""
    article = await db.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    
    # User isolation: normal users can only reprocess their own articles
    if not current_user.is_super_admin and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")
    
    # Notes use clean_content as source of truth; backfill raw if empty.
    if article.content_type == 'note' and not article.raw_content:
        article.raw_content = article.clean_content or ''

    if not article.raw_content:
        raise HTTPException(status_code=400, detail="No raw content to process")

    # Re-clean with platform-aware conversion (skip for spark/note - already markdown)
    platform = article.source_platform or (parser_service.detect_platform(article.url) if article.url else 'other')
    if platform in ("spark", "note") or article.content_type == 'note':
        clean_md = article.raw_content  # Already markdown, don't re-process
    else:
        clean_md = parser_service.clean_to_markdown(article.raw_content, platform)
    article.clean_content = clean_md
    article.plain_text = clean_md
    
    # Re-parse — pass raw_content as raw_html fallback for thin content
    ai_result = await llm_service.parse_article(clean_md, article.url, article.raw_content or "")
    
    article.title = ai_result.get('title', article.title)
    article.summary = ai_result.get('summary', '')
    article.key_points = ai_result.get('key_points', [])
    article.reading_time = ai_result.get('estimated_reading_minutes', 5)
    article.word_count = parser_service.count_words(clean_md)
    
    # Tags
    article.tags.clear()
    for tag_name in ai_result.get('tags', []):
        result = await db.execute(
            select(Tag).where(func.lower(Tag.name) == tag_name.lower())
        )
        tag = result.scalar_one_or_none()
        if not tag:
            tag = Tag(name=tag_name, is_ai_generated=True, user_id=article.user_id)
            db.add(tag)
            await db.flush()
        article.tags.append(tag)
    
    await db.commit()
    await db.refresh(article)
    
    # Generate knowledge graph connections
    try:
        await graph_service.generate_graph(db, article_id)
    except Exception as graph_err:
        logger.warning(f"Graph generation error for {article_id}: {graph_err}")
    
    return AIProcessResponse(
        article_id=article.id,
        title=article.title,
        summary=article.summary or "",
        key_points=article.key_points or [],
        tags=article.tags,
        reading_time=article.reading_time or 5,
        word_count=article.word_count or 0,
        source_platform=article.source_platform or "other",
        author=article.author or "unknown",
    )


@router.get("/{article_id}/related")
async def get_related_articles(
    article_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    username_query: str | None = Query(None, alias="username", description="Superadmin: filter by username"),
):
    """Get related articles grouped by relation type."""
    article = await db.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    # User isolation: normal users can only access their own articles
    if not current_user.is_super_admin and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")

    # Query all edges connected to this article
    result = await db.execute(
        select(KnowledgeEdge).where(
            (KnowledgeEdge.source_article_id == article_id) |
            (KnowledgeEdge.target_article_id == article_id)
        )
    )
    edges = result.scalars().all()

    if not edges:
        return {"article_id": str(article_id), "groups": []}

    # Collect connected article IDs (excluding self-references)
    connected_ids = set()
    for edge in edges:
        connected_id = edge.target_article_id if edge.source_article_id == article_id else edge.source_article_id
        if connected_id != article_id:
            connected_ids.add(connected_id)

    # Fetch connected articles with user isolation
    articles_map = {}
    if connected_ids:
        query = select(Article).where(Article.id.in_(connected_ids))
        # User isolation for related articles (superadmin uses ?username=xxx to view others)
        target_user_id = current_user.id
        if current_user.is_super_admin and username_query:
            user_result = await db.execute(select(User).where(User.username == username_query))
            target_user = user_result.scalar_one_or_none()
            target_user_id = target_user.id if target_user else current_user.id
        query = query.where(Article.user_id == target_user_id)
        result = await db.execute(query)
        for a in result.scalars().all():
            articles_map[a.id] = a

    # Group by relation_type
    relation_labels = {
        "related": "相关文章",
        "prerequisite": "前置知识",
        "extends": "延伸阅读",
        "contradicts": "观点对立",
    }

    groups_dict = {}
    for edge in edges:
        connected_id = edge.target_article_id if edge.source_article_id == article_id else edge.source_article_id
        if connected_id == article_id:
            continue
        article_data = articles_map.get(connected_id)
        if not article_data:
            continue
        rt = edge.relation_type or "related"
        if rt not in groups_dict:
            groups_dict[rt] = {
                "relation_type": rt,
                "relation_label": relation_labels.get(rt, rt),
                "articles": [],
            }
        groups_dict[rt]["articles"].append({
            "id": str(article_data.id),
            "title": article_data.title,
            "summary": (article_data.summary or "")[:80],
            "relation_desc": edge.relation_desc or "",
        })

    groups = list(groups_dict.values())

    return {"article_id": str(article_id), "groups": groups}


@router.post("/backfill-embeddings")
async def backfill_embeddings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    username_query: str | None = Query(None, alias="username", description="Superadmin: filter by username"),
):
    """Backfill embeddings for articles that don't have them yet."""
    import logging, traceback
    logger = logging.getLogger(__name__)
    
    query = select(Article).where(Article.embedding.is_(None))
    
    # User isolation: superadmin sees own by default, or specific user via ?username=xxx
    target_user_id = current_user.id
    if current_user.is_super_admin and username_query:
        user_result = await db.execute(select(User).where(User.username == username_query))
        target_user = user_result.scalar_one_or_none()
        target_user_id = target_user.id if target_user else current_user.id
    query = query.where(Article.user_id == target_user_id)
    
    result = await db.execute(query)
    articles = result.scalars().all()
    
    count = 0
    for article in articles:
        try:
            content = f"{article.title}. {article.summary or ''}. {(article.plain_text or '')[:2000]}"
            logger.info(f"Generating embedding for article {article.id}: title={article.title[:30]}")
            embedding = await llm_service.get_embedding(content)
            logger.info(f"Got embedding dims={len(embedding)}")
            article.embedding = embedding
            count += 1
        except Exception as e:
            logger.error(f"Backfill embedding error for {article.id}: {e}")
            logger.error(traceback.format_exc())
    
    await db.commit()
    return {"status": "ok", "backfilled": count, "total_articles": len(articles)}
