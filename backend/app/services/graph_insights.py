"""Graph Insights(Phase 1·A) —— 把图算法跑在用户的知识图谱上,产出可读洞察。

借鉴 llm_wiki 的 Graph Insights:主动发现「意外连接」(共享大量邻居却没连上的文章对)
与「知识缺口」(孤岛文章 / 过小主题),再加社区聚类和枢纽节点。

- compute_insights(db, user_id) → 结构化洞察(供 /api/knowledge/insights 与前端)
- pick_digest_line(insights) → 一句话洞察(供微信周期复习 digest 追加)
"""
import logging
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, KnowledgeEdge
from app.services import graph_algorithms as ga

logger = logging.getLogger("trove.insights")

MAX_NODES = 200  # 与 get_graph 一致,控制算法规模


async def _load_user_graph(db: AsyncSession, user_id: UUID) -> Tuple[List[dict], List[dict]]:
    """装载某用户的图(节点=文章,边=knowledge_edges),与 get_graph 同口径。"""
    r = await db.execute(
        select(Article)
        .where(Article.user_id == user_id)
        .order_by(Article.created_at.desc())
        .limit(MAX_NODES)
    )
    articles = r.scalars().all()
    ids = {a.id for a in articles}
    ids_str = {str(a.id) for a in articles}

    nodes = [
        {"id": str(a.id), "title": a.title, "tags": [t.name for t in (a.tags or [])]}
        for a in articles
    ]

    edges: List[dict] = []
    if ids:
        er = await db.execute(
            select(KnowledgeEdge).where(
                KnowledgeEdge.source_article_id.in_(ids),
                KnowledgeEdge.target_article_id.in_(ids),
            )
        )
        for e in er.scalars().all():
            s, t = str(e.source_article_id), str(e.target_article_id)
            if s in ids_str and t in ids_str:
                edges.append({
                    "source": s, "target": t,
                    "weight": e.weight, "relation_type": e.relation_type,
                })
    return nodes, edges


async def compute_insights(db: AsyncSession, user_id: UUID) -> dict:
    """跑全套图算法,返回结构化洞察。空库/极小库优雅返回 empty=True。"""
    nodes, edges = await _load_user_graph(db, user_id)

    if len(nodes) < 3 or len(edges) < 1:
        return {
            "empty": True,
            "stats": {"articles": len(nodes), "edges": len(edges),
                      "communities": 0, "orphans": 0},
            "communities": [], "hubs": [], "surprising_links": [],
            "gaps": {"orphans": [], "orphan_count": 0, "small_topics": []},
        }

    G = ga.build_graph(nodes, edges)
    communities = ga.detect_communities(G)
    nc_map = ga.node_community_map(G, communities)
    hubs = ga.central_hubs(G)
    surprising = ga.surprising_links(G, nc_map)
    gaps = ga.knowledge_gaps(G, communities)

    return {
        "empty": False,
        "stats": {
            "articles": G.number_of_nodes(),
            "edges": G.number_of_edges(),
            "communities": len(communities),
            "orphans": gaps["orphan_count"],
        },
        # members 列表对前端太重,只回 sample_titles + size + label
        "communities": [
            {"id": c["id"], "label": c["label"], "size": c["size"],
             "sample_titles": c["sample_titles"]}
            for c in communities
        ],
        "hubs": hubs,
        "surprising_links": surprising,
        "gaps": gaps,
    }


def pick_digest_line(insights: dict) -> Optional[str]:
    """从洞察里挑「最有意思的一条」,渲染成一句话,用于微信复习 digest 追加。

    优先级:跨主题的意外连接 > 任意意外连接 > 知识孤岛提醒 > 枢纽提示。
    """
    if not insights or insights.get("empty"):
        return None

    sl = insights.get("surprising_links") or []
    cross = [s for s in sl if s.get("cross_community")]
    pick = (cross[0] if cross else (sl[0] if sl else None))
    if pick:
        a = pick["source"]["title"][:24]
        b = pick["target"]["title"][:24]
        tag = "跨主题" if pick.get("cross_community") else "潜在"
        return f"🔗 {tag}关联:《{a}》和《{b}》看似不同,其实高度相关,建议对照看。"

    gaps = insights.get("gaps") or {}
    oc = gaps.get("orphan_count") or 0
    if oc >= 3:
        sample = (gaps.get("orphans") or [{}])[0].get("title", "")[:24]
        return f"🧩 你有 {oc} 篇孤岛文章还没和其他内容建立关联(如《{sample}》),可以补点相关阅读。"

    hubs = insights.get("hubs") or []
    if hubs:
        return f"⭐ 你知识库的核心枢纽是《{hubs[0]['title'][:24]}》,关联了 {hubs[0]['degree']} 篇内容。"
    return None
