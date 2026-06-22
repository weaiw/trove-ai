"""概念合成页 API(Phase 7·E)。"""
import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.article import Article, Tag, ConceptPage, article_tags
from app.services import concept_service

logger = logging.getLogger("trove.concepts")
router = APIRouter(prefix="/api/concepts", tags=["concepts"])


class ConceptSummary(BaseModel):
    id: str
    name: str
    seed_type: str
    seed_tag: Optional[str] = None
    source_count: int
    stale: bool
    new_source_count: int
    auto_update: bool = False
    updated_at: str


class ConceptDetail(ConceptSummary):
    content: str
    sources: List[dict] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    tag: Optional[str] = None


class CreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    seed_type: str = "topic"          # 'tag' | 'topic'
    seed_tag: Optional[str] = None
    article_ids: Optional[List[str]] = None   # 用户选定子簇时传


class UpdateConceptRequest(BaseModel):
    auto_update: Optional[bool] = None


def _summary(p: ConceptPage) -> ConceptSummary:
    return ConceptSummary(
        id=str(p.id), name=p.name, seed_type=p.seed_type, seed_tag=p.seed_tag,
        source_count=len(p.source_article_ids or []), stale=bool(p.stale),
        new_source_count=p.new_source_count or 0,
        auto_update=bool(p.auto_update),
        updated_at=p.updated_at.isoformat() if p.updated_at else "",
    )


@router.get("", response_model=List[ConceptSummary])
async def list_concepts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(ConceptPage).where(ConceptPage.user_id == current_user.id)
        .order_by(desc(ConceptPage.updated_at))
    )).scalars().all()
    return [_summary(p) for p in rows]


@router.get("/suggestions")
async def concept_suggestions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """按文章数排序的标签,作为候选概念种子;标注哪些已建页。"""
    rows = (await db.execute(
        select(Tag.name, func.count(article_tags.c.article_id).label("cnt"))
        .join(article_tags, Tag.id == article_tags.c.tag_id)
        .join(Article, (Article.id == article_tags.c.article_id) & (Article.user_id == current_user.id))
        .where(Tag.user_id == current_user.id)
        .group_by(Tag.name)
        .having(func.count(article_tags.c.article_id) >= 2)
        .order_by(desc("cnt"))
        .limit(30)
    )).all()
    existing = {
        (p.seed_tag or "").lower()
        for p in (await db.execute(
            select(ConceptPage).where(ConceptPage.user_id == current_user.id,
                                      ConceptPage.seed_type == "tag")
        )).scalars().all()
    }
    return [
        {"tag": name, "article_count": cnt, "has_page": name.lower() in existing}
        for name, cnt in rows
    ]


@router.post("/analyze")
async def analyze_concept(
    body: AnalyzeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """探查来源 & 连贯性:返回 coherent(可直接合成)或 needs_split(给出子簇供选)。"""
    return await concept_service.analyze(db, current_user.id, body.name.strip(), body.tag)


@router.post("", response_model=ConceptDetail, status_code=201)
async def create_concept(
    body: CreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        page = await concept_service.synthesize_and_save(
            db, current_user.id, body.name.strip(), body.seed_type,
            body.seed_tag, body.article_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await _detail(db, current_user.id, page)


@router.get("/{concept_id}", response_model=ConceptDetail)
async def get_concept(
    concept_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    page = await db.get(ConceptPage, concept_id)
    if not page or page.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Concept not found")
    return await _detail(db, current_user.id, page)


@router.post("/{concept_id}/regenerate", response_model=ConceptDetail)
async def regenerate_concept(
    concept_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    page = await db.get(ConceptPage, concept_id)
    if not page or page.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Concept not found")
    try:
        page = await concept_service.synthesize_and_save(
            db, current_user.id, page.name, page.seed_type, page.seed_tag, None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await _detail(db, current_user.id, page)


@router.patch("/{concept_id}", response_model=ConceptDetail)
async def update_concept(
    concept_id: UUID,
    body: UpdateConceptRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新概念页设置(目前:auto_update 自动合并开关)。"""
    page = await db.get(ConceptPage, concept_id)
    if not page or page.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Concept not found")
    if body.auto_update is not None:
        page.auto_update = body.auto_update
    await db.commit()
    await db.refresh(page)
    return await _detail(db, current_user.id, page)


@router.delete("/{concept_id}", status_code=204)
async def delete_concept(
    concept_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    page = await db.get(ConceptPage, concept_id)
    if not page or page.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Concept not found")
    await db.delete(page)
    await db.commit()


async def _detail(db: AsyncSession, user_id: UUID, page: ConceptPage) -> ConceptDetail:
    sources = []
    ids = page.source_article_ids or []
    if ids:
        uid_list = []
        for s in ids:
            try:
                uid_list.append(UUID(s))
            except (ValueError, TypeError):
                continue
        rows = (await db.execute(
            select(Article.id, Article.title).where(Article.id.in_(uid_list))
        )).all()
        title_map = {str(i): t for i, t in rows}
        # 保持合成时的顺序(引用编号对应)
        sources = [{"id": s, "title": title_map.get(s, "(已删除)")} for s in ids]
    base = _summary(page)
    return ConceptDetail(**base.model_dump(), content=page.content or "", sources=sources)
