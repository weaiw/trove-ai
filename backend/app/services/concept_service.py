"""概念合成服务(Phase 7·E)。

把同一概念跨文章合成一页带溯源的"活百科"。关键:来源不是裸标签成员,而是
**语义聚合后的连贯文章集**;宽标签会被聚类拆成子概念(复用 networkx+Louvain),
避免"同标签不同类知识揉成一锅粥"。合成时把 contradicts 边带进「分歧」一节。

主入口:
- analyze(db, user_id, name, tag) → 探查来源 & 连贯性:{coherent|needs_split, ...}
- synthesize_and_save(db, user_id, name, seed_type, seed_tag, article_ids) → 落库 ConceptPage
"""
import logging
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, Tag, KnowledgeEdge, ConceptPage
from app.services.ai_service import llm_service
from app.services.kb_purpose import purpose_clause, get_kb_purpose

logger = logging.getLogger("trove.concept")

CANDIDATE_LIMIT = 40       # 候选池上限
SYNTH_SOURCE_CAP = 15      # 实际喂给 LLM 合成的来源上限(控 token)
MIN_FOR_SPLIT = 6          # 少于这个数不做拆簇
SEMANTIC_STALE_SIM = 0.55  # 新文章与概念质心的余弦 ≥ 此值即判为"相关新来源"


def _cosine(a: list, b: list) -> float:
    try:
        import numpy as np
        va, vb = np.array(a, dtype=float), np.array(b, dtype=float)
        na, nb = np.linalg.norm(va), np.linalg.norm(vb)
        if na == 0 or nb == 0:
            return 0.0
        return float(va @ vb / (na * nb))
    except Exception:
        return 0.0


def _centroid(arts: List[Article]) -> Optional[list]:
    """一组文章 embedding 的质心(均值),作为概念页的语义锚。"""
    embs = [e for e in (_emb_list(a) for a in arts) if e]
    if not embs:
        return None
    try:
        import numpy as np
        return np.mean(np.array(embs, dtype=float), axis=0).tolist()
    except Exception:
        return None


# ── 候选来源聚合 ───────────────────────────────────────────────────────────
async def _gather_by_tag(db: AsyncSession, user_id: UUID, tag: str) -> List[Article]:
    r = await db.execute(
        select(Article)
        .join(Article.tags)
        .where(
            func.lower(Tag.name) == tag.lower(),
            Tag.user_id == user_id,
            Article.user_id == user_id,
            Article.embedding.isnot(None),
        )
        .order_by(Article.created_at.desc())
        .limit(CANDIDATE_LIMIT)
    )
    # join 可能产生重复,去重
    seen, out = set(), []
    for a in r.scalars().all():
        if a.id not in seen:
            seen.add(a.id)
            out.append(a)
    return out


async def _gather_by_topic(db: AsyncSession, user_id: UUID, name: str) -> List[Article]:
    emb = await llm_service.get_embedding(name, emb_type="query")
    emb_str = "[" + ",".join(str(v) for v in emb) + "]"
    rows = (await db.execute(
        text(f"""
            SELECT id, (embedding <-> '{emb_str}'::vector) AS dist
            FROM articles
            WHERE embedding IS NOT NULL AND user_id = :uid
            ORDER BY embedding <-> '{emb_str}'::vector
            LIMIT :lim
        """),
        {"uid": user_id, "lim": 25},
    )).fetchall()
    if not rows:
        return []
    # 语义阈值:丢掉明显离群的(距离 > 最近的 1.6 倍且绝对值偏大)
    dists = [float(d) for _, d in rows]
    cutoff = max(dists[0] * 1.6, dists[0] + 2.0)
    keep_ids = [rid for (rid, d), dd in zip(rows, dists) if dd <= cutoff]
    if not keep_ids:
        keep_ids = [rows[0][0]]
    res = await db.execute(select(Article).where(Article.id.in_(keep_ids)))
    return list(res.scalars().all())


def _emb_list(a: Article) -> Optional[list]:
    e = a.embedding
    if e is None:
        return None
    try:
        return list(e)
    except TypeError:
        return None


def _cluster(items: List[Article]) -> Optional[List[List[Article]]]:
    """对候选做连贯性聚类。返回 None=连贯(单一概念);返回多簇=异质,建议拆子概念。

    保守拆分:只在出现 2~4 个"像样"的子簇(各≥15% 且≥3 篇)且最大簇<60% 时才拆,
    避免把一个本来连贯的主题(如 RAG)切成一堆碎片。"""
    pool = [a for a in items if _emb_list(a) is not None]
    n = len(pool)
    if n < MIN_FOR_SPLIT:
        return None
    try:
        import numpy as np
        import networkx as nx
        import community as community_louvain
    except Exception as e:
        logger.warning(f"cluster deps missing, treat as coherent: {e}")
        return None

    embs = np.array([_emb_list(a) for a in pool], dtype=float)
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normed = embs / norms
    sim = normed @ normed.T

    G = nx.Graph()
    G.add_nodes_from(range(n))
    k = 5  # 更密的 kNN → 社区更粗,少碎片
    for i in range(n):
        order = np.argsort(-sim[i])
        cnt = 0
        for j in order:
            j = int(j)
            if j == i:
                continue
            w = float(sim[i][j])
            if w <= 0:
                continue
            G.add_edge(i, j, weight=w)
            cnt += 1
            if cnt >= k:
                break
    if G.number_of_edges() == 0:
        return None

    # resolution<1 → 倾向更大的社区(更不容易过度拆分)
    part = community_louvain.best_partition(G, weight="weight", resolution=0.8, random_state=42)
    buckets: Dict[int, List[int]] = {}
    for node, c in part.items():
        buckets.setdefault(c, []).append(node)

    min_size = max(3, int(0.15 * n))
    sizable = sorted((b for b in buckets.values() if len(b) >= min_size), key=len, reverse=True)
    # 只在 2~4 个像样子簇、且最大簇 < 60% 时判为异质
    if 2 <= len(sizable) <= 4 and len(sizable[0]) < 0.6 * n:
        return [[pool[i] for i in b] for b in sizable]
    return None


def _cluster_label(cluster: List[Article], pool: List[Article], exclude_tag: Optional[str]) -> str:
    """子簇标签:挑"区分度"最高的 tag —— 在本簇频繁但全局不普遍(cluster_freq/sqrt(pool_freq))。
    避免多个簇都叫同一个泛标签。无 tag 时退回代表标题。"""
    from collections import Counter
    pool_freq: Counter = Counter()
    for a in pool:
        for t in (a.tags or []):
            pool_freq[t.name] += 1
    cl_freq: Counter = Counter()
    for a in cluster:
        for t in (a.tags or []):
            if exclude_tag and t.name.lower() == exclude_tag.lower():
                continue
            cl_freq[t.name] += 1
    if cl_freq:
        best = max(cl_freq, key=lambda t: cl_freq[t] / (pool_freq.get(t, 1) ** 0.5))
        return best
    title = (cluster[0].title or "未命名").strip()
    return title[:16]


# ── analyze:探查来源 & 连贯性 ─────────────────────────────────────────────
async def analyze(db: AsyncSession, user_id: UUID, name: str, tag: Optional[str]) -> dict:
    """返回 {coherent, source_count, sources[]} 或 {needs_split, clusters[]}。"""
    if tag:
        candidates = await _gather_by_tag(db, user_id, tag)
    else:
        candidates = await _gather_by_topic(db, user_id, name)

    if not candidates:
        return {"coherent": True, "needs_split": False, "source_count": 0, "sources": []}

    clusters = _cluster(candidates)
    if clusters and len(clusters) >= 2:
        return {
            "needs_split": True,
            "clusters": [
                {
                    "label": _cluster_label(cl, candidates, tag),
                    "article_ids": [str(a.id) for a in cl],
                    "sample_titles": [a.title or "Untitled" for a in cl[:4]],
                    "size": len(cl),
                }
                for cl in clusters
            ],
        }
    return {
        "coherent": True,
        "needs_split": False,
        "source_count": len(candidates),
        "sources": [{"id": str(a.id), "title": a.title or "Untitled"} for a in candidates],
    }


# ── 合成 ───────────────────────────────────────────────────────────────────
async def _synthesize(db: AsyncSession, user_id: UUID, name: str,
                      articles: List[Article]) -> Tuple[str, List[str]]:
    """LLM 合成 markdown。返回 (content, used_article_ids)。"""
    used = articles[:SYNTH_SOURCE_CAP]
    id_to_idx = {str(a.id): i + 1 for i, a in enumerate(used)}

    blocks = []
    for i, a in enumerate(used, 1):
        kp = a.key_points or []
        kp_str = ("；".join(kp[:5])) if kp else ""
        snippet = (a.summary or (a.clean_content or "")[:300] or "").strip().replace("\n", " ")
        blocks.append(f"[{i}] 《{a.title or 'Untitled'}》\n摘要:{snippet[:280]}" + (f"\n要点:{kp_str}" if kp_str else ""))
    sources_block = "\n\n".join(blocks)

    # contradicts 边 → 分歧线索
    used_ids = [a.id for a in used]
    contra = (await db.execute(
        select(KnowledgeEdge).where(
            KnowledgeEdge.relation_type == "contradicts",
            KnowledgeEdge.source_article_id.in_(used_ids),
            KnowledgeEdge.target_article_id.in_(used_ids),
            KnowledgeEdge.user_id == user_id,
        )
    )).scalars().all()
    contra_lines = []
    for e in contra:
        si = id_to_idx.get(str(e.source_article_id))
        ti = id_to_idx.get(str(e.target_article_id))
        if si and ti:
            contra_lines.append(f"[{si}] 与 [{ti}] 存在分歧:{e.relation_desc or '观点对立'}")
    contra_block = ("\n".join(contra_lines)) if contra_lines else "(未检测到明显分歧)"

    kb = await get_kb_purpose(db, user_id)
    system = "你是知识合成专家,把多篇来源融合成一篇连贯、准确、带引用的概念词条。" + purpose_clause(kb)
    prompt = f"""请围绕概念「{name}」,把下面这些来源融合成一篇结构化的"概念词条"(markdown)。

要求:
1. 结构分节:## 概述(一段话定义/全貌)、## 关键要点(跨来源归纳,分条)、## 分歧与争议(若有)、## 延伸与相关。
2. **每个论断后用 [n] 标注来源编号**(n 对应下方来源序号),可多引 [1][3]。不要编造来源里没有的内容。
3. 客观、信息密度高,不灌水;若来源在某方面信息不足,直说"来源未充分覆盖"。
4. 总长 500-900 字。直接输出 markdown 正文,不要代码围栏。

已知分歧线索(用于「分歧与争议」一节):
{contra_block}

--- 来源 ---
{sources_block}
--- 结束 ---

请输出概念「{name}」的词条:"""

    content = await llm_service._chat(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        temperature=0.4,
    )
    return content.strip(), [str(a.id) for a in used]


async def synthesize_and_save(
    db: AsyncSession, user_id: UUID, name: str, seed_type: str,
    seed_tag: Optional[str], article_ids: Optional[List[str]],
) -> ConceptPage:
    """合成并 upsert 一个概念页。article_ids 给定则用之(如用户选了某子簇),否则按 name/tag 聚合。"""
    if article_ids:
        uid_list = []
        for s in article_ids:
            try:
                uid_list.append(UUID(s))
            except (ValueError, TypeError):
                continue
        res = await db.execute(
            select(Article).where(Article.id.in_(uid_list), Article.user_id == user_id)
        )
        articles = list(res.scalars().all())
    elif seed_type == "tag" and seed_tag:
        articles = await _gather_by_tag(db, user_id, seed_tag)
    else:
        articles = await _gather_by_topic(db, user_id, name)

    if len(articles) < 2:
        raise ValueError("来源不足(至少需要 2 篇相关文章才能合成)。")

    content, used_ids = await _synthesize(db, user_id, name, articles)
    used_set = set(used_ids)
    centroid = _centroid([a for a in articles if str(a.id) in used_set])

    # upsert by (user_id, name)
    existing = (await db.execute(
        select(ConceptPage).where(ConceptPage.user_id == user_id, ConceptPage.name == name)
    )).scalar_one_or_none()
    if existing:
        existing.content = content
        existing.source_article_ids = used_ids
        existing.centroid = centroid
        existing.seed_type = seed_type
        existing.seed_tag = seed_tag
        existing.stale = False
        existing.new_source_count = 0
        page = existing
    else:
        page = ConceptPage(
            user_id=user_id, name=name, seed_type=seed_type, seed_tag=seed_tag,
            content=content, source_article_ids=used_ids, centroid=centroid,
            stale=False, new_source_count=0,
        )
        db.add(page)
    await db.commit()
    await db.refresh(page)
    return page


async def process_new_article(db: AsyncSession, user_id: UUID, article_id, tag_names: List[str]) -> None:
    """入库钩子(在 embedding 生成后调用):

    命中的概念页打 stale —— 标签页按 seed_tag 精确命中,主题/子分类页按 embedding 与
    概念质心(centroid)的余弦相似度命中。auto_update 开启的页面进一步后台自动重合成。
    """
    art = await db.get(Article, article_id)
    art_emb = _emb_list(art) if art else None
    lowered = {t.lower() for t in (tag_names or [])}

    pages = (await db.execute(
        select(ConceptPage).where(ConceptPage.user_id == user_id)
    )).scalars().all()

    to_regen = []
    changed = False
    for p in pages:
        # 已经是这页的来源 → 不重复触发
        if str(article_id) in (p.source_article_ids or []):
            continue
        hit = False
        if p.seed_type == "tag" and p.seed_tag and p.seed_tag.lower() in lowered:
            hit = True
        elif art_emb is not None and p.centroid is not None:
            if _cosine(art_emb, list(p.centroid)) >= SEMANTIC_STALE_SIM:
                hit = True
        if not hit:
            continue
        p.stale = True
        p.new_source_count = (p.new_source_count or 0) + 1
        changed = True
        if p.auto_update:
            to_regen.append(p)

    if changed:
        await db.commit()

    # auto_update:后台自动重合成(每页独立 try,失败不影响其它)
    for p in to_regen:
        try:
            await synthesize_and_save(db, user_id, p.name, p.seed_type, p.seed_tag, None)
            logger.info(f"concept auto-updated: {p.name}")
        except Exception as e:
            logger.warning(f"auto_update regen failed for concept {p.id}: {e}")
