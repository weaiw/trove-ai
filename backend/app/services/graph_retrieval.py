"""图扩展检索(Phase 2·C) —— 借鉴 llm_wiki 的 graph-based expansion。

向量召回拿到种子文章后,沿 knowledge_edges 走一跳,把强关联的邻居也拉进 RAG 上下文。
向量只看"语义相似",图边补上"人/AI 判定的显式关联"(related/prerequisite/extends/
contradicts),两者互补。尤其 contradicts/prerequisite 这类语义不一定近但很该一起读。
"""
import logging
from typing import Dict, List
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("trove.graph_retrieval")


async def expand_via_graph(
    db: AsyncSession,
    seed_ids: List[str],
    user_id: UUID,
    max_extra: int = 3,
    min_weight: float = 0.3,
    chars: int = 1200,
) -> List[Dict]:
    """沿知识图谱边把种子文章的强关联邻居拉出来。

    返回 [{article_id, title, chunk, relation_type, weight}],已排除种子本身、按边权降序、
    去重、截断到 chars。种子为空或无邻居时返回 []。
    """
    if not seed_ids:
        return []
    seeds = set(seed_ids)

    # 一跳:任一端命中种子的边(双向),按权重降序
    rows = (await db.execute(
        text("""
            SELECT source_article_id, target_article_id, relation_type, weight
            FROM knowledge_edges
            WHERE user_id = :uid
              AND weight >= :minw
              AND (source_article_id = ANY(:seeds) OR target_article_id = ANY(:seeds))
            ORDER BY weight DESC
        """),
        {"uid": user_id, "minw": min_weight, "seeds": list(seeds)},
    )).fetchall()

    # 邻居 = 边上不在种子集里的那一端;保留最高权重 + 关系类型
    neighbor_meta: Dict[str, Dict] = {}
    for src, tgt, rel, w in rows:
        src, tgt = str(src), str(tgt)
        neighbor = tgt if src in seeds else (src if tgt in seeds else None)
        if not neighbor or neighbor in seeds:
            continue
        cur = neighbor_meta.get(neighbor)
        if cur is None or float(w) > cur["weight"]:
            neighbor_meta[neighbor] = {"relation_type": rel, "weight": float(w)}

    if not neighbor_meta:
        return []

    top = sorted(neighbor_meta.items(), key=lambda kv: kv[1]["weight"], reverse=True)[:max_extra]
    top_ids = [nid for nid, _ in top]

    arts = (await db.execute(
        text("""
            SELECT id, title, clean_content, raw_content
            FROM articles
            WHERE id = ANY(:ids) AND user_id = :uid
        """),
        {"ids": top_ids, "uid": user_id},
    )).fetchall()

    out: List[Dict] = []
    for aid, title, clean_content, raw_content in arts:
        content = (clean_content or raw_content or "").strip()
        if not content:
            continue
        if len(content) > chars:
            content = content[:chars] + "…"
        meta = neighbor_meta[str(aid)]
        out.append({
            "article_id": str(aid),
            "title": title or "Untitled",
            "chunk": content,
            "relation_type": meta["relation_type"],
            "weight": meta["weight"],
        })
    # 按权重还原顺序
    out.sort(key=lambda d: d["weight"], reverse=True)
    logger.info("graph expand: %d seeds → %d neighbors", len(seeds), len(out))
    return out
