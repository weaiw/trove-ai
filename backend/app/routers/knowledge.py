"""Knowledge graph & folder/tag management API routes."""
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import Optional, List
from uuid import UUID

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.article import Tag, Folder, Article, KnowledgeEdge, ArticleStatus, LearningPath, article_tags
from app.schemas.article import (
    TagResponse, TagUpdate, TagWithCount,
    MergeTagsRequest, BatchDeleteTagsRequest,
    FolderResponse, GraphDataResponse,
)
from app.services.graph_service import graph_service
from app.services.ai_service import llm_service

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


# ---- Tags ----
async def _resolve_target_user_id(db: AsyncSession, current_user: User, username_query: Optional[str]) -> UUID:
    """超管默认只看自己;传 ?username= 才查指定用户(与文章/图谱一致)。普通用户恒为自己。"""
    if current_user.is_super_admin and username_query:
        ur = await db.execute(select(User).where(User.username == username_query))
        tu = ur.scalar_one_or_none()
        if tu:
            return tu.id
    return current_user.id


@router.get("/tags", response_model=List[TagResponse])
async def list_tags(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    username_query: Optional[str] = Query(None, alias="username", description="Superadmin: filter by username"),
):
    """List tags(默认只看自己;超管可 ?username= 看指定用户)。"""
    target_user_id = await _resolve_target_user_id(db, current_user, username_query)
    query = select(Tag).where(Tag.user_id == target_user_id).order_by(Tag.name)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/tags", response_model=TagResponse, status_code=201)
async def create_tag(
    name: str = Query(...),
    color: str = Query("#007aff"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new tag(按用户隔离:同名仅在本用户内查重)。"""
    existing = await db.execute(
        select(Tag).where(
            func.lower(Tag.name) == name.lower(),
            Tag.user_id == current_user.id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Tag already exists")

    tag = Tag(name=name, color=color, is_ai_generated=False, user_id=current_user.id)
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag


@router.patch("/tags/{tag_id}", response_model=TagResponse)
async def update_tag(
    tag_id: UUID,
    body: TagUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update tag name, color, or description."""
    tag = await db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    # Data isolation: only superadmins can edit others' tags
    if not current_user.is_super_admin and tag.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Tag not found")

    if body.name is not None:
        tag.name = body.name
    if body.color is not None:
        tag.color = body.color
    if body.description is not None:
        tag.description = body.description

    await db.commit()
    await db.refresh(tag)
    return tag


@router.get("/tags/stats", response_model=List[TagWithCount])
async def get_tag_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    username_query: Optional[str] = Query(None, alias="username", description="Superadmin: filter by username"),
):
    """Tags with article counts(默认只看自己;超管可 ?username=)。计数只统计目标用户自己的文章。"""
    target_user_id = await _resolve_target_user_id(db, current_user, username_query)
    query = (
        select(
            Tag,
            func.count(Article.id).label("article_count"),
        )
        .outerjoin(article_tags, Tag.id == article_tags.c.tag_id)
        .outerjoin(
            Article,
            (Article.id == article_tags.c.article_id) & (Article.user_id == target_user_id),
        )
        .where(Tag.user_id == target_user_id)
        .group_by(Tag.id)
        .order_by(Tag.name)
    )
    result = await db.execute(query)
    rows = result.all()
    return [
        TagWithCount(
            id=tag.id,
            name=tag.name,
            color=tag.color,
            is_ai_generated=tag.is_ai_generated,
            description=tag.description,
            article_count=count,
        )
        for tag, count in rows
    ]


@router.post("/tags/merge", response_model=TagResponse)
async def merge_tags(
    body: MergeTagsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Merge source tag into target tag. All articles from source move to target,
    then source is deleted. Returns the merged target tag."""
    if body.source_tag_id == body.target_tag_id:
        raise HTTPException(status_code=400, detail="Source and target tags must be different")

    source = await db.get(Tag, body.source_tag_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source tag not found")

    target = await db.get(Tag, body.target_tag_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target tag not found")

    # Data isolation: both tags must belong to current user (or superadmin)
    if not current_user.is_super_admin:
        if source.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Source tag not found")
        if target.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Target tag not found")

    # Find article_ids already associated with target to avoid duplicates
    existing = await db.execute(
        select(article_tags.c.article_id).where(
            article_tags.c.tag_id == body.target_tag_id
        )
    )
    existing_ids = {row[0] for row in existing.all()}

    # Find source article associations
    source_rows = await db.execute(
        select(article_tags.c.article_id).where(
            article_tags.c.tag_id == body.source_tag_id
        )
    )
    source_ids = [row[0] for row in source_rows.all()]

    # Insert only articles that are not already associated with target
    new_article_ids = [aid for aid in source_ids if aid not in existing_ids]
    if new_article_ids:
        from sqlalchemy import insert
        await db.execute(
            insert(article_tags),
            [{"article_id": aid, "tag_id": body.target_tag_id} for aid in new_article_ids],
        )

    # Delete source tag (CASCADE removes remaining article_tags entries)
    await db.delete(source)
    await db.commit()
    await db.refresh(target)
    return target


@router.post("/tags/batch-delete", status_code=204)
async def batch_delete_tags(
    body: BatchDeleteTagsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Batch delete multiple tags by their IDs."""
    # Fetch tags to confirm they exist and count what will be deleted
    query = select(Tag).where(Tag.id.in_(body.tag_ids))
    if not current_user.is_super_admin:
        query = query.where(Tag.user_id == current_user.id)
    result = await db.execute(query)
    tags = result.scalars().all()

    if not tags:
        raise HTTPException(status_code=404, detail="No matching tags found")

    await db.execute(
        delete(Tag).where(Tag.id.in_(body.tag_ids))
    )
    await db.commit()


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(
    tag_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a tag."""
    tag = await db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    # Data isolation
    if not current_user.is_super_admin and tag.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Tag not found")
    await db.delete(tag)
    await db.commit()


# ---- Folders ----
@router.get("/folders", response_model=List[FolderResponse])
async def list_folders(
    parent_id: Optional[UUID] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    username_query: Optional[str] = Query(None, alias="username", description="Superadmin: filter by username"),
):
    """List folders(默认只看自己;超管可 ?username= 看指定用户)。"""
    target_user_id = await _resolve_target_user_id(db, current_user, username_query)
    if parent_id:
        query = select(Folder).where(Folder.parent_id == parent_id).order_by(Folder.name)
    else:
        query = select(Folder).where(Folder.parent_id.is_(None)).order_by(Folder.name)
    query = query.where(Folder.user_id == target_user_id)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/folders", response_model=FolderResponse, status_code=201)
async def create_folder(
    name: str = Query(...),
    parent_id: Optional[UUID] = None,
    color: str = Query("#007aff"),
    icon: str = Query("folder"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new folder."""
    folder = Folder(name=name, parent_id=parent_id, color=color, icon=icon, user_id=current_user.id)
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return folder


@router.patch("/folders/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: UUID,
    name: Optional[str] = Query(None),
    color: Optional[str] = Query(None),
    icon: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update folder name / color / icon."""
    folder = await db.get(Folder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    if not current_user.is_super_admin and folder.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Folder not found")

    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Folder name cannot be empty")
        folder.name = name
    if color is not None:
        folder.color = color
    if icon is not None:
        folder.icon = icon

    await db.commit()
    await db.refresh(folder)
    return folder


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a folder and move articles to root."""
    folder = await db.get(Folder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    # Data isolation
    if not current_user.is_super_admin and folder.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Folder not found")
    
    # Move articles to root
    result = await db.execute(
        select(Article).where(Article.folder_id == folder_id)
    )
    for article in result.scalars().all():
        article.folder_id = None
    
    await db.delete(folder)
    await db.commit()


# ---- Knowledge Graph ----
@router.get("/graph", response_model=GraphDataResponse)
async def get_graph(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    username_query: str | None = Query(None, alias="username", description="Superadmin: filter by username"),
):
    """Get full knowledge graph data."""
    # User isolation: superadmin sees own by default, or specific user via ?username=xxx
    target_user_id = current_user.id
    if current_user.is_super_admin and username_query:
        user_result = await db.execute(select(User).where(User.username == username_query))
        target_user = user_result.scalar_one_or_none()
        target_user_id = target_user.id if target_user else current_user.id
    article_query = select(Article).where(Article.user_id == target_user_id).order_by(Article.created_at.desc()).limit(200)
    result = await db.execute(article_query)
    articles = result.scalars().all()

    # Collect article IDs for edge filtering
    article_ids = {a.id for a in articles}
    article_ids_str = {str(aid) for aid in article_ids}

    # Get edges — only edges between current user's articles
    if article_ids:
        result = await db.execute(
            select(KnowledgeEdge).where(
                KnowledgeEdge.source_article_id.in_(article_ids),
                KnowledgeEdge.target_article_id.in_(article_ids),
            )
        )
    else:
        result = await db.execute(select(KnowledgeEdge).where(False))
    edges = result.scalars().all()

    # Build node data
    nodes = [
        {
            "id": str(a.id),
            "title": a.title,
            "summary": (a.summary or "")[:100],
            "tags": [t.name for t in (a.tags or [])],
            "source_platform": a.source_platform,
        }
        for a in articles
    ]

    # Build edge data (only edges where both articles exist in the node set)
    edge_data = []
    for edge in edges:
        sid = str(edge.source_article_id)
        tid = str(edge.target_article_id)
        if sid in article_ids_str and tid in article_ids_str:
            edge_data.append({
                "id": str(edge.id),
                "source": sid,
                "target": tid,
                "relation_type": edge.relation_type,
                "relation_desc": edge.relation_desc,
                "weight": edge.weight,
            })

    return {"nodes": nodes, "edges": edge_data}


@router.get("/insights")
async def get_insights(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    username_query: str | None = Query(None, alias="username", description="Superadmin: filter by username"),
):
    """Graph Insights:社区聚类 / 枢纽 / 意外连接 / 知识缺口(Louvain + Adamic-Adar)。"""
    from app.services.graph_insights import compute_insights
    target_user_id = current_user.id
    if current_user.is_super_admin and username_query:
        user_result = await db.execute(select(User).where(User.username == username_query))
        target_user = user_result.scalar_one_or_none()
        target_user_id = target_user.id if target_user else current_user.id
    return await compute_insights(db, target_user_id)


async def _regenerate_graph_background(target_user_id: UUID):
    """Background regenerate task — runs after request returns.

    Process articles in batches of 12 (LLM 4096-token response truncates above ~15).
    Uses ON CONFLICT DO NOTHING so concurrent regenerate clicks don't blow up on
    the (source, target) unique constraint.
    """
    import logging
    from app.database import async_session
    logger = logging.getLogger(__name__)

    async with async_session() as db:
        result = await db.execute(
            select(Article)
            .where(Article.user_id == target_user_id, Article.summary.isnot(None))
            .order_by(Article.created_at.desc())
        )
        articles = result.scalars().all()
        article_ids = {a.id for a in articles}

        if article_ids:
            await db.execute(
                delete(KnowledgeEdge).where(
                    or_(
                        KnowledgeEdge.source_article_id.in_(article_ids),
                        KnowledgeEdge.target_article_id.in_(article_ids),
                    )
                )
            )
            await db.commit()

        BATCH_SIZE = 12
        total_added = 0
        total_hallucinated = 0
        seen_edge_keys = set()

        for batch_start in range(0, len(articles), BATCH_SIZE):
            batch = articles[batch_start:batch_start + BATCH_SIZE]
            if len(batch) < 2:
                continue
            batch_data = [
                {"id": str(a.id), "title": a.title, "summary": a.summary or ""}
                for a in batch
            ]
            try:
                graph_data = await llm_service.generate_knowledge_graph(batch_data)
            except Exception as e:
                logger.warning(f"LLM call failed for batch {batch_start}: {e}")
                continue

            valid_ids = {str(a.id) for a in batch}
            rows = []
            for edge in graph_data.get("edges", []):
                try:
                    s, t = edge["source"], edge["target"]
                    if s not in valid_ids or t not in valid_ids:
                        total_hallucinated += 1
                        continue
                    if s == t:
                        continue
                    if (s, t) in seen_edge_keys:
                        continue
                    seen_edge_keys.add((s, t))
                    rows.append({
                        "source_article_id": UUID(s),
                        "target_article_id": UUID(t),
                        "relation_type": edge.get("relation_type", "related"),
                        "relation_desc": edge.get("relation_desc", ""),
                        "weight": edge.get("weight", 0.5),
                        "user_id": target_user_id,
                    })
                except (ValueError, KeyError):
                    continue

            if rows:
                # ON CONFLICT DO NOTHING — survive concurrent regenerate clicks
                stmt = pg_insert(KnowledgeEdge).values(rows).on_conflict_do_nothing(
                    index_elements=["source_article_id", "target_article_id"]
                )
                await db.execute(stmt)
                await db.commit()
                total_added += len(rows)

        logger.info(
            "regenerate_graph(bg): %d articles → %d edges added (%d hallucinated skipped)",
            len(articles), total_added, total_hallucinated,
        )


@router.post("/graph/regenerate", status_code=202)
async def regenerate_graph(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    username_query: str | None = Query(None, alias="username", description="Superadmin: filter by username"),
):
    """Kick off knowledge graph regeneration in the background.

    LLM batch processing of 70+ articles takes >60s, exceeding most idle-connection
    timeouts (Tencent Cloud security group, intermediate proxies). Returning 202
    immediately and processing in the background lets the user poll /api/knowledge/graph
    when ready.
    """
    target_user_id = current_user.id
    if current_user.is_super_admin and username_query:
        user_result = await db.execute(select(User).where(User.username == username_query))
        target_user = user_result.scalar_one_or_none()
        target_user_id = target_user.id if target_user else current_user.id

    background_tasks.add_task(_regenerate_graph_background, target_user_id)
    return {"message": "Regenerate started — refresh in 30-60s to see updated graph", "status": "processing"}


@router.get("/graph/article/{article_id}")
async def get_article_graph(
    article_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get knowledge graph centered on a specific article."""
    article = await db.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    # Data isolation
    if not current_user.is_super_admin and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")
    
    # Get connected edges
    edge_query = select(KnowledgeEdge).where(
        (KnowledgeEdge.source_article_id == article_id) |
        (KnowledgeEdge.target_article_id == article_id)
    )
    result = await db.execute(edge_query)
    edges = result.scalars().all()
    
    # Collect all connected article IDs
    connected_ids = {article_id}
    for edge in edges:
        connected_ids.add(edge.source_article_id)
        connected_ids.add(edge.target_article_id)
    
    # Get article data (filtered by user for non-superadmin)
    article_query = select(Article).where(Article.id.in_(connected_ids))
    if not current_user.is_super_admin:
        article_query = article_query.where(Article.user_id == current_user.id)
    result = await db.execute(article_query)
    articles = result.scalars().all()
    
    nodes = [
        {
            "id": str(a.id),
            "title": a.title,
            "summary": (a.summary or "")[:100],
            "tags": [t.name for t in (a.tags or [])],
        }
        for a in articles
    ]
    
    edge_data = [
        {
            "id": str(e.id),
            "source": str(e.source_article_id),
            "target": str(e.target_article_id),
            "relation_type": e.relation_type,
            "relation_desc": e.relation_desc,
            "weight": e.weight,
        }
        for e in edges
    ]
    
    return {"nodes": nodes, "edges": edge_data, "center_id": str(article_id)}


# ---- Stats ----
@router.get("/stats")
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    username_query: str | None = Query(None, alias="username", description="Superadmin: filter by username"),
):
    """Get library statistics."""
    # User isolation: superadmin sees own by default, or specific user via ?username=xxx
    target_user_id = current_user.id
    if current_user.is_super_admin and username_query:
        user_result = await db.execute(select(User).where(User.username == username_query))
        target_user = user_result.scalar_one_or_none()
        target_user_id = target_user.id if target_user else current_user.id
    
    # Helper to build filtered article count queries
    def _filter_articles(query):
        return query.where(Article.user_id == target_user_id)

    def _filter_tags(query):
        return query.where(Tag.user_id == target_user_id)

    total = await db.execute(_filter_articles(select(func.count(Article.id))))
    unread = await db.execute(
        _filter_articles(select(func.count(Article.id)).where(Article.status == 'unread'))
    )
    completed = await db.execute(
        _filter_articles(select(func.count(Article.id)).where(Article.status == 'completed'))
    )
    favorites = await db.execute(
        _filter_articles(select(func.count(Article.id)).where(Article.is_favorited == True))
    )
    total_tags = await db.execute(_filter_tags(select(func.count(Tag.id))))
    total_edges = await db.execute(
        select(func.count(KnowledgeEdge.id))
    )
    total_paths = await db.execute(select(func.count(LearningPath.id)))
    
    return {
        "total_articles": total.scalar(),
        "unread": unread.scalar(),
        "completed": completed.scalar(),
        "favorites": favorites.scalar(),
        "total_tags": total_tags.scalar(),
        "total_edges": total_edges.scalar(),
        "total_paths": total_paths.scalar(),
    }


@router.get("/mindmap/{article_id}")
async def get_cached_mindmap(
    article_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get cached mind map for an article (no AI generation)."""
    result = await db.execute(select(Article).where(Article.id == article_id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    # Data isolation
    if not current_user.is_super_admin and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")
    
    if article.mindmap_data:
        return {"mindmap_data": article.mindmap_data, "cached": True}
    return {"mindmap_data": None, "cached": False}


@router.post("/mindmap/{article_id}")
async def generate_mindmap(
    article_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate an AI-powered mind map for an article."""
    article = await db.get(Article, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    # Data isolation
    if not current_user.is_super_admin and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")

    # Return cached if exists (regeneration requires ?force=1)
    if article.mindmap_data:
        return {"mindmap_data": article.mindmap_data, "cached": True}

    content = article.clean_content or article.raw_content
    if not content:
        raise HTTPException(status_code=400, detail="Article has no content to process")

    mindmap = await llm_service.generate_mindmap(content, article.title)
    
    # Save to cache
    root_node = mindmap.get("root") or mindmap.get("mindmap_data")
    if root_node:
        article.mindmap_data = root_node
        await db.commit()
    
    return {"mindmap_data": root_node, "cached": False}


@router.delete("/mindmap/{article_id}")
async def delete_mindmap_cache(
    article_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete cached mindmap for an article, forcing regeneration on next request."""
    result = await db.execute(select(Article).where(Article.id == article_id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    # Data isolation
    if not current_user.is_super_admin and article.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Article not found")
    
    article.mindmap_data = None
    await db.commit()
    return {"ok": True, "message": "Mindmap cache deleted"}
