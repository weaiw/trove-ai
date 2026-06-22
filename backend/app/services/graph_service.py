"""Knowledge graph service - manage article relationships."""
import logging
import random
from typing import List, Dict, Any, Set
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from app.models.article import KnowledgeEdge, Article
from app.services.ai_service import llm_service

logger = logging.getLogger(__name__)


def _get_keywords(text: str) -> Set[str]:
    """Extract significant lowercase words of 3+ chars from text.

    Used for keyword-overlap filtering when articles lack tags.
    Matches sequences of word characters (letters/digits/underscore)
    that are at least 3 characters long.
    """
    import re
    if not text:
        return set()
    words = re.findall(r"\b\w{3,}\b", text.lower())
    return set(words)


class GraphService:
    """Build and manage knowledge graph."""

    async def get_graph_data(self, db: AsyncSession) -> Dict[str, Any]:
        """Get full knowledge graph data with nodes and edges."""
        # Get all articles
        result = await db.execute(
            select(Article).order_by(Article.created_at.desc()).limit(200)
        )
        articles = result.scalars().all()

        # Get all edges
        result = await db.execute(select(KnowledgeEdge))
        edges = result.scalars().all()

        # Build node data
        nodes = []
        article_ids = set()
        for i, article in enumerate(articles):
            article_ids.add(str(article.id))
            nodes.append({
                "id": str(article.id),
                "title": article.title,
                "summary": (article.summary or "")[:100],
                "tags": [t.name for t in (article.tags or [])],
                "source_platform": article.source_platform,
            })

        # Build edge data (only edges where both articles exist)
        edge_data = []
        for edge in edges:
            sid = str(edge.source_article_id)
            tid = str(edge.target_article_id)
            if sid in article_ids and tid in article_ids:
                edge_data.append({
                    "id": str(edge.id),
                    "source": sid,
                    "target": tid,
                    "relation_type": edge.relation_type,
                    "relation_desc": edge.relation_desc,
                    "weight": edge.weight,
                })

        return {"nodes": nodes, "edges": edge_data}

    async def generate_graph(self, db: AsyncSession, article_id: UUID):
        """Generate knowledge graph edges for a newly added article.

        Before sending to AI, filters existing articles to only those that
        share at least one tag with the new article. Falls back to keyword
        overlap when neither article has tags. Caps articles sent to AI at 20.
        """
        # Get the new article first — we need its owner to scope candidates.
        result = await db.execute(
            select(Article).where(Article.id == article_id)
        )
        new_article = result.scalar_one_or_none()
        if not new_article:
            return

        # Candidate articles MUST be scoped to the same user — otherwise edge
        # generation would leak other users' summaries to the LLM and could
        # create cross-user edges. Multi-tenant isolation.
        result = await db.execute(
            select(Article).where(
                Article.id != article_id,
                Article.user_id == new_article.user_id,
                Article.summary.isnot(None),
            )
        )
        existing = result.scalars().all()

        if len(existing) < 2:
            return

        # --- Filter existing articles by tag overlap (or keyword fallback) ---
        new_tags: Set[str] = {t.name.lower() for t in (new_article.tags or [])}
        new_keywords: Set[str] = set()

        if not new_tags:
            # No tags on new article — fall back to keyword extraction
            new_keywords = _get_keywords(
                (new_article.title or "") + " " + (new_article.summary or "")
            )
            logger.debug(
                "Article %s has no tags; extracted %d keywords for overlap filtering",
                article_id, len(new_keywords),
            )

        filtered_existing = []
        for a in existing:
            existing_tags: Set[str] = {t.name.lower() for t in (a.tags or [])}

            if new_tags and existing_tags:
                # Both have tags — require at least 1 shared tag
                if new_tags & existing_tags:
                    filtered_existing.append(a)
                else:
                    logger.debug(
                        "Skipping article %s (no shared tags with %s)", a.id, article_id
                    )
            elif new_keywords and not existing_tags:
                # Neither has tags — fall back to keyword overlap
                existing_text = (a.title or "") + " " + (a.summary or "")
                existing_keywords = _get_keywords(existing_text)
                if new_keywords & existing_keywords:
                    filtered_existing.append(a)
                else:
                    logger.debug(
                        "Skipping article %s (no keyword overlap with %s)", a.id, article_id
                    )
            else:
                # Mixed case (one has tags, other doesn't) — include as low-confidence
                logger.debug(
                    "Article %s included despite tag mismatch (mixed tag availability)", a.id
                )
                filtered_existing.append(a)

        logger.info(
            "generate_graph for %s: %d total existing, %d after tag/keyword filtering",
            article_id, len(existing), len(filtered_existing),
        )

        # Cap at 11 candidates (+1 new article = 12 total) — LLM 4096-token output
        # gets truncated above ~15 articles per call, silently dropping all edges.
        if len(filtered_existing) > 11:
            filtered_existing = random.sample(filtered_existing, 11)
            logger.info("generate_graph: capped filtered articles to 11 (random sample)")

        if len(filtered_existing) < 2:
            return

        # Build article list for AI
        articles_for_ai = [
            {
                "id": str(new_article.id),
                "title": new_article.title,
                "summary": new_article.summary or "",
            }
        ]
        for a in filtered_existing:
            articles_for_ai.append({
                "id": str(a.id),
                "title": a.title,
                "summary": a.summary or "",
            })

        # Generate edges via AI
        graph_data = await llm_service.generate_knowledge_graph(articles_for_ai)

        # Validate AI output against IDs we actually sent — LLMs sometimes hallucinate UUIDs
        # that don't exist, which would cause FK violations and roll back the entire commit.
        valid_ids = {str(new_article.id)} | {str(a.id) for a in filtered_existing}
        skipped_hallucinated = 0

        for edge in graph_data.get("edges", []):
            try:
                source_str = edge["source"]
                target_str = edge["target"]
                if source_str not in valid_ids or target_str not in valid_ids:
                    skipped_hallucinated += 1
                    continue
                if source_str == target_str:
                    continue
                source_id = UUID(source_str)
                target_id = UUID(target_str)

                # Check if edge already exists
                check = await db.execute(
                    select(KnowledgeEdge).where(
                        KnowledgeEdge.source_article_id == source_id,
                        KnowledgeEdge.target_article_id == target_id,
                    )
                )
                if check.scalar_one_or_none():
                    continue

                new_edge = KnowledgeEdge(
                    source_article_id=source_id,
                    target_article_id=target_id,
                    relation_type=edge.get("relation_type", "related"),
                    relation_desc=edge.get("relation_desc", ""),
                    weight=edge.get("weight", 0.5),
                    user_id=new_article.user_id,
                )
                db.add(new_edge)
            except (ValueError, KeyError):
                continue

        if skipped_hallucinated:
            logger.warning(
                "generate_graph for %s: skipped %d edge(s) with hallucinated IDs",
                article_id, skipped_hallucinated,
            )

        await db.flush()

    async def regenerate_all(self, db: AsyncSession, user_id: UUID = None):
        """Regenerate all knowledge graph edges.

        Caps articles sent to AI at 50 (random sample if more) to avoid
        overwhelming the AI service with large payloads.
        """
        # Clear existing edges
        await db.execute(delete(KnowledgeEdge))

        # Get all articles with summaries
        result = await db.execute(
            select(Article).where(Article.summary.isnot(None)).order_by(Article.created_at.desc())
        )
        articles = result.scalars().all()

        if len(articles) < 2:
            return

        # Process in batches of 12 — LLM 4096-token output truncates above ~15
        BATCH_SIZE = 12
        total_added = 0
        total_hallucinated = 0
        seen = set()

        for i in range(0, len(articles), BATCH_SIZE):
            batch = articles[i:i + BATCH_SIZE]
            if len(batch) < 2:
                continue
            batch_data = [
                {"id": str(a.id), "title": a.title, "summary": a.summary or ""}
                for a in batch
            ]
            graph_data = await llm_service.generate_knowledge_graph(batch_data)
            valid_ids = {str(a.id) for a in batch}

            for edge in graph_data.get("edges", []):
                try:
                    source_str = edge["source"]
                    target_str = edge["target"]
                    if source_str not in valid_ids or target_str not in valid_ids:
                        total_hallucinated += 1
                        continue
                    if source_str == target_str:
                        continue
                    key = (source_str, target_str)
                    if key in seen:
                        continue
                    seen.add(key)
                    db.add(KnowledgeEdge(
                        source_article_id=UUID(source_str),
                        target_article_id=UUID(target_str),
                        relation_type=edge.get("relation_type", "related"),
                        relation_desc=edge.get("relation_desc", ""),
                        weight=edge.get("weight", 0.5),
                        user_id=user_id,
                    ))
                    total_added += 1
                except (ValueError, KeyError):
                    continue

        logger.info(
            "regenerate_all: %d articles → %d edges added (%d hallucinated IDs skipped)",
            len(articles), total_added, total_hallucinated,
        )

        await db.flush()


graph_service = GraphService()
