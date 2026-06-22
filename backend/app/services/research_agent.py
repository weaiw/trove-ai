"""Agentic RAG — multi-stage research pipeline over the user's knowledge base.

Stages:
  1. plan      → LLM breaks the query into 2-5 sub-questions
  2. retrieve  → for each sub-question, semantic search top-K articles
  3. synthesize→ LLM writes a structured answer citing evidence
  4. critique  → LLM finds gaps / counter-points in its own answer

The runner is an async generator yielding `ResearchEvent` dicts so callers
(SSE endpoint, wechat-bot) can stream progress messages to users.

See for prior agent-architecture lessons.
"""
import asyncio
import json
import logging
import re
from typing import AsyncIterator, Dict, List, Optional, TypedDict
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai_service import llm_service, _parse_llm_json

logger = logging.getLogger("trove.research")

# Tunables — keep modest so research finishes in ~20-30s for typical query
RETRIEVE_TOP_K = 3                # per sub-question
MAX_SUB_QUESTIONS = 5
CONTEXT_CHARS_PER_ARTICLE = 1200


class ResearchEvent(TypedDict, total=False):
    stage: str          # plan | retrieve | synthesize | critique | final | error
    message: str        # human-readable progress text
    data: dict          # structured payload (only for final)


def _emit(stage: str, message: str, data: Optional[dict] = None) -> ResearchEvent:
    out: ResearchEvent = {"stage": stage, "message": message}
    if data is not None:
        out["data"] = data
    return out


# ── Retrieval helper (semantic search, scoped to one user) ─────────────
async def _semantic_search(
    db: AsyncSession, query: str, user_id: UUID, top_k: int = 3
) -> List[Dict]:
    """Return list of {article_id, title, chunk, distance}."""
    emb = await llm_service.get_embedding(query, emb_type="query")
    emb_str = "[" + ",".join(str(v) for v in emb) + "]"
    sql = text(f"""
        SELECT id, title, clean_content, raw_content,
               (embedding <-> '{emb_str}'::vector) AS distance
        FROM articles
        WHERE embedding IS NOT NULL AND user_id = :user_id
        ORDER BY embedding <-> '{emb_str}'::vector
        LIMIT :top_k
    """)
    r = await db.execute(sql, {"top_k": top_k, "user_id": user_id})
    out = []
    for row in r.fetchall():
        article_id, title, clean_content, raw_content, distance = row
        content = (clean_content or raw_content or "").strip()
        if not content:
            continue
        # Truncate to budget
        if len(content) > CONTEXT_CHARS_PER_ARTICLE:
            content = content[:CONTEXT_CHARS_PER_ARTICLE] + "…"
        out.append({
            "article_id": str(article_id),
            "title": title or "Untitled",
            "chunk": content,
            "distance": float(distance),
        })
    return out


# ── Stage prompts ──────────────────────────────────────────────────────
async def _plan(query: str, kb_purpose: str = "") -> List[str]:
    """Decompose the user's question into 2-5 sub-questions."""
    from app.services.kb_purpose import purpose_clause
    prompt = f"""你是研究助理。用户提出一个问题，请把它拆成 2-5 个可以独立去知识库检索的子问题。
子问题之间应**互补**（覆盖不同侧面、时间、对比维度），不要重复。

用户问题：
{query}

严格按 JSON 格式输出（不要 markdown 围栏）：
{{
  "sub_questions": ["子问题 1", "子问题 2", ...]
}}"""
    raw = await llm_service._chat(
        [{"role": "system", "content": "你是严谨的研究规划助手。" + purpose_clause(kb_purpose)},
         {"role": "user", "content": prompt}],
        temperature=0.3,
    )
    parsed = _parse_llm_json(raw) or {}
    subs = parsed.get("sub_questions") or []
    subs = [s.strip() for s in subs if isinstance(s, str) and s.strip()]
    if not subs:
        subs = [query]  # graceful fallback
    return subs[:MAX_SUB_QUESTIONS]


async def _synthesize(query: str, evidence: List[Dict], kb_purpose: str = "") -> str:
    """Write a structured answer from gathered evidence."""
    from app.services.kb_purpose import purpose_clause
    blocks = []
    cite_idx = 0
    cite_map = []  # (idx, title, article_id)
    for ev in evidence:
        for art in ev["articles"]:
            cite_idx += 1
            cite_map.append((cite_idx, art["title"], art["article_id"]))
            blocks.append(f"[{cite_idx}] {art['title']}\n{art['chunk']}")
    context = "\n\n".join(blocks) if blocks else "（知识库中暂无相关材料）"

    prompt = f"""你是研究助理。基于以下知识库材料，回答用户问题。

要求：
1. **结构化**：开头一句话核心结论，然后分要点展开（每点 2-3 句）。
2. **必须引用**：每个论点后用 [n] 标注材料编号。不要编造材料以外的内容。
3. **诚实**：如果材料不足以回答某方面，明说"材料不足"。
4. 总长度 300-500 字，便于用户阅读。

--- 知识库材料 ---
{context}
--- 结束 ---

用户问题：{query}

请回答："""
    answer = await llm_service._chat(
        [{"role": "system", "content": "你是基于证据写作的研究助理。" + purpose_clause(kb_purpose)},
         {"role": "user", "content": prompt}],
        temperature=0.4,
    )
    return answer.strip()


async def _critique(query: str, synthesis: str) -> str:
    """Critic agent — find gaps / counter-arguments in the synthesis."""
    prompt = f"""你是审稿人。下面是另一个 AI 针对用户问题给出的回答，请简短指出：
1. 哪些点**论据不足**或可能不准确
2. 是否有**反方视角**没考虑
3. 用户后续可以补充查阅的方向

要求：80 字以内，直接说要点，不要客套。

用户原问题：{query}

回答：
{synthesis}

你的审稿："""
    text_ = await llm_service._chat(
        [{"role": "system", "content": "你是简短直白的审稿助手。"},
         {"role": "user", "content": prompt}],
        temperature=0.4,
    )
    return text_.strip()


# ── Main agent loop ────────────────────────────────────────────────────
async def run_research(
    db: AsyncSession,
    query: str,
    user_id: UUID,
    kb_purpose: str = "",
) -> AsyncIterator[ResearchEvent]:
    """Yield progress events through the full research pipeline."""
    try:
        # Stage 1: Plan
        yield _emit("plan", "正在拆解问题…")
        sub_questions = await _plan(query, kb_purpose)
        yield _emit("plan", f"拆出 {len(sub_questions)} 个子问题：" +
                    "、".join(f"「{s[:18]}」" for s in sub_questions[:3]))

        # Stage 2: Retrieve (parallel across sub-questions for speed)
        yield _emit("retrieve", f"在你的知识库中检索 {len(sub_questions)} 个角度的素材…")
        retrieval_tasks = [
            _semantic_search(db, sq, user_id, RETRIEVE_TOP_K) for sq in sub_questions
        ]
        retrieval_results = await asyncio.gather(*retrieval_tasks, return_exceptions=True)
        evidence: List[Dict] = []
        total_articles = 0
        for sq, res in zip(sub_questions, retrieval_results):
            if isinstance(res, Exception):
                logger.warning(f"retrieve failed for sub-question {sq!r}: {res}")
                arts = []
            else:
                arts = res
            evidence.append({"question": sq, "articles": arts})
            total_articles += len(arts)
        # Dedup articles across sub-questions (keep first occurrence per id)
        seen_ids = set()
        for ev in evidence:
            unique = []
            for a in ev["articles"]:
                if a["article_id"] in seen_ids:
                    continue
                seen_ids.add(a["article_id"])
                unique.append(a)
            ev["articles"] = unique
        unique_total = len(seen_ids)
        yield _emit(
            "retrieve",
            f"找到 {unique_total} 篇相关文章" + (
                "（去重后）" if total_articles > unique_total else ""
            ),
        )

        # Stage 2.5: 图扩展——沿知识图谱边补充强关联文章
        try:
            from app.services.graph_retrieval import expand_via_graph
            extra = await expand_via_graph(db, list(seen_ids), user_id, max_extra=3)
            extra = [e for e in extra if e["article_id"] not in seen_ids]
            if extra:
                evidence.append({"question": "图谱关联", "articles": [
                    {"article_id": e["article_id"], "title": e["title"],
                     "chunk": e["chunk"], "distance": 0.0}
                    for e in extra
                ]})
                for e in extra:
                    seen_ids.add(e["article_id"])
                unique_total = len(seen_ids)
                yield _emit("retrieve", f"图谱补充 {len(extra)} 篇强关联文章")
        except Exception as ex:
            logger.warning(f"graph expansion failed: {ex}")

        # Stage 3: Synthesize
        yield _emit("synthesize", "正在综合写作…")
        synthesis = await _synthesize(query, evidence, kb_purpose)
        yield _emit("synthesize", "初稿完成")

        # Stage 4: Critique
        yield _emit("critique", "正在自我审查、找漏洞…")
        critique = await _critique(query, synthesis)

        # Build citations list for the final result
        citations = []
        for ev in evidence:
            for art in ev["articles"]:
                citations.append({
                    "article_id": art["article_id"],
                    "title": art["title"],
                    "distance": art["distance"],
                })

        yield _emit("final", "完成", data={
            "answer": synthesis,
            "critique": critique,
            "sub_questions": sub_questions,
            "citations": citations,
            "article_count": unique_total,
        })
    except Exception as e:
        logger.exception(f"research_agent failed: {e}")
        yield _emit("error", f"研究助理出错：{type(e).__name__}: {e}")
