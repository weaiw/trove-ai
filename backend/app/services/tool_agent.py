"""Tool-using research agent — LLM with function calling drives a ReAct loop
over a small set of library tools (search_library / read_article / list_recent).

Compared to [[research_agent.py]] (fixed 4-stage sequential pipeline), this
agent **decides itself** which tool to call next based on what it has learned
so far. The user sees the model's tool selection — the "agent thinking" — not
just stage names. This is the structural pattern behind ChatGPT/Claude Code
tool use.

LLM: hardcoded SiliconFlow DeepSeek-V3 (function-calling capable) to decouple
from whatever the user configured as their primary LLM (which may be a code
model not great at tool use).
"""
import asyncio
import json
import logging
import os
import re
from typing import AsyncIterator, Dict, List, Optional, TypedDict
from uuid import UUID

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai_service import llm_service

logger = logging.getLogger("trove.tool-agent")

MAX_STEPS = 8                  # hard cap on tool-loop iterations
MAX_TOOL_RESULT_CHARS = 4000   # truncate large tool outputs before feeding back to LLM
LIBRARY_TOP_K = 5
_BACKGROUND_TASKS: set[asyncio.Task] = set()

# Use DeepSeek-V3 via SiliconFlow for reliable function calling.
SF_MODEL = "deepseek-ai/DeepSeek-V3"
SF_BASE = "https://api.siliconflow.cn/v1"
SF_RETRY_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
SF_RETRY_DELAYS = (1.0, 2.0)


class LLMServiceBusyError(RuntimeError):
    """Raised when the upstream LLM provider is temporarily unavailable."""


class AgentEvent(TypedDict, total=False):
    stage: str       # start / token / tool_call / tool_result / confirm / final / error
    message: str
    data: dict


def _emit(stage: str, message: str, data: Optional[dict] = None) -> AgentEvent:
    out: AgentEvent = {"stage": stage, "message": message}
    if data is not None:
        out["data"] = data
    return out


# ── Tool schemas (OpenAI-compatible function-calling format) ──────────
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_library",
            "description": (
                "Semantic search over the user's personal knowledge library. Use when "
                "you need to find articles by topic/concept. Returns top-K most "
                "relevant articles with their snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query in natural Chinese or English",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "How many articles to return",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_article",
            "description": (
                "Fetch the FULL content of one specific article by its id. Use AFTER "
                "search_library when a snippet looks relevant but you need the whole "
                "article to draw a conclusion."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "article_id": {
                        "type": "string",
                        "description": "UUID returned by search_library",
                    },
                },
                "required": ["article_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_articles",
            "description": (
                "List the user's most recently saved articles (titles + summaries). "
                "Use when the user asks about their reading habits, what they have been "
                "into lately, or for a broad overview not driven by a specific topic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 30},
                    "limit": {"type": "integer", "default": 15},
                },
            },
        },
    },
    # ── 写工具（会改变用户数据，调用前需确认）────────────────────────
    {
        "type": "function",
        "function": {
            "name": "tag_articles",
            "description": (
                "给一批文章打标签（追加，不覆盖已有标签）。这是会改变用户数据的"
                "写操作：调用前你必须已向用户说明将给哪些文章打哪些标签、为什么。"
                "article_ids 用 search_library / list_recent_articles 返回的 id。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "article_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要打标签的文章 id 列表（UUID）",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要打的标签名列表，如 ['AI-Agent', 'RAG']",
                    },
                    "reason": {
                        "type": "string",
                        "description": "为什么打这些标签，一句话，给用户看",
                    },
                },
                "required": ["article_ids", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_to_folder",
            "description": (
                "把一批文章归类到某个文件夹（文件夹不存在会自动新建）。这是会改变"
                "用户数据的写操作，调用前须已向用户说明。article_ids 用搜索结果里的 id。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "article_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要归类的文章 id 列表（UUID）",
                    },
                    "folder_name": {
                        "type": "string",
                        "description": "目标文件夹名，如 'AI Agent'、'待读'",
                    },
                    "reason": {
                        "type": "string",
                        "description": "为什么这样归类，一句话，给用户看",
                    },
                },
                "required": ["article_ids", "folder_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_url_to_folder",
            "description": (
                "把一个网页链接采集入知识库并归类到指定文件夹；文件夹不存在会自动新建，"
                "链接已入库则直接移动到目标文件夹。适用于『把这个链接入库到某文件夹』或"
                "『创建某文件夹并把链接放进去』。这是写操作，调用前须向用户展示 URL 和目标文件夹。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要采集的完整 http/https URL",
                    },
                    "folder_name": {
                        "type": "string",
                        "description": "目标文件夹名，如 'AI Agent'、'产品资料'",
                    },
                    "reason": {
                        "type": "string",
                        "description": "为什么保存到这个文件夹，一句话，给用户看",
                    },
                },
                "required": ["url", "folder_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "link_articles",
            "description": (
                "在两篇文章之间建立一条知识图谱关系（有方向：from source 到 target）。"
                "写操作，调用前须已向用户说明。两个 id 都用搜索结果里的 id。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_article_id": {
                        "type": "string",
                        "description": "起点文章 id（UUID）",
                    },
                    "target_article_id": {
                        "type": "string",
                        "description": "终点文章 id（UUID）",
                    },
                    "relation_type": {
                        "type": "string",
                        "enum": ["related", "prerequisite", "extends", "contradicts"],
                        "description": (
                            "关系类型：related=相关 / prerequisite=source 是 target 的前置 / "
                            "extends=source 延伸了 target / contradicts=两者观点对立"
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "这条关系的说明，给用户看，也存进图谱",
                    },
                },
                "required": ["source_article_id", "target_article_id", "relation_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synthesize_concept",
            "description": (
                "把同一概念跨多篇文章合成一篇『概念页/活词条』并存库（带溯源引用）。"
                "库里至少要有 2 篇相关文章才能合成。会调用大模型，较慢。写操作，调用前须已向用户说明。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "concept_name": {
                        "type": "string",
                        "description": "概念名，如 'RAG'、'AI Agent 记忆'。会作为概念页标题",
                    },
                    "article_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：指定参与合成的文章 id；不填则系统按概念名自动聚合相关文章",
                    },
                    "reason": {
                        "type": "string",
                        "description": "为什么合成这个概念页，一句话，给用户看",
                    },
                },
                "required": ["concept_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "configure_review",
            "description": (
                "配置用户的『定期复习简报』——开启/关闭、调整推送频率与时间。"
                "注意：这是【用户级】周期复习（通过已绑定的微信账号推送），"
                "不是针对某几篇文章排程。写操作，调用前须已向用户说明。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "description": "是否开启定期复习简报",
                        "default": True,
                    },
                    "frequency_days": {
                        "type": "integer",
                        "description": "每几天推送一次（1-90）",
                        "default": 7,
                    },
                    "time_of_day": {
                        "type": "string",
                        "description": "每天几点推送，24 小时制 HH:MM，如 '09:00'",
                        "default": "09:00",
                    },
                    "reason": {
                        "type": "string",
                        "description": "为什么这样配置，一句话，给用户看",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_duplicates",
            "description": (
                "找出用户库里疑似重复的文章——(1) 正文完全相同（同文不同链接/转载）、"
                "(2) 语义高度相似。这是【只读】工具：只列出可疑配对供用户判断，"
                "绝不删除任何东西。无需确认，可直接调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "threshold": {
                        "type": "number",
                        "description": "语义距离阈值（越小越严格，默认 0.12，只挑非常接近的）",
                        "default": 0.12,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回多少对疑似重复",
                        "default": 20,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索外部信息（Tavily）。当用户问的内容库里没有、需要最新/外部信息、"
                "或要做『库内 vs 库外』对比时使用。返回若干网页结果（标题+摘要+URL）。"
                "只读，无需确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索词，自然语言即可",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "返回几条结果",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_read",
            "description": (
                "抓取并清洗某个网页 URL 的正文。通常在 web_search 拿到 URL 后，"
                "对最相关的一两条调用它读全文。只读，无需确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的网页 URL（http/https）",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "把一条关于用户的【稳定、长期】事实记进长期记忆，以后每次对话都会知道。"
                "只记值得长期记住的：身份、长期偏好、反复确认的口径。"
                "不要记一次性的、会变的、本轮才相关的内容。无需确认，可直接调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的一句话，如『用户是产品经理』『偏好结论先行』",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["profile", "preference", "fact"],
                        "description": "类型：profile=身份画像 / preference=偏好 / fact=事实",
                    },
                },
                "required": ["content"],
            },
        },
    },
]

# 写工具名单：在此集合中的工具会被"确认闸"拦截（见 _execute_tool）。
# 注意 find_duplicates 是【只读】，不在此名单——它只找不删，无需确认。
WRITE_TOOLS = {
    "tag_articles", "move_to_folder", "save_url_to_folder", "link_articles",
    "synthesize_concept", "configure_review",
}


# ── Tool implementations ─────────────────────────────────────────────
async def _tool_search_library(
    db: AsyncSession, user_id: UUID, query: str, top_k: int = LIBRARY_TOP_K
) -> dict:
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
    results = []
    for row in r.fetchall():
        article_id, title, clean, raw, distance = row
        content = (clean or raw or "").strip()
        snippet = content[:600] + ("…" if len(content) > 600 else "")
        results.append({
            "article_id": str(article_id),
            "title": title or "Untitled",
            "snippet": snippet,
            "distance": round(float(distance), 4),
        })
    return {"count": len(results), "results": results}


async def _tool_read_article(db: AsyncSession, user_id: UUID, article_id: str) -> dict:
    from app.models import Article
    try:
        uid = UUID(article_id)
    except ValueError:
        return {"error": "invalid article_id (not a UUID)"}
    r = await db.execute(
        select(Article).where(Article.id == uid, Article.user_id == user_id)
    )
    article = r.scalar_one_or_none()
    if not article:
        return {"error": "article not found in this user's library"}
    content = (article.clean_content or article.raw_content or "").strip()
    if len(content) > MAX_TOOL_RESULT_CHARS:
        content = content[:MAX_TOOL_RESULT_CHARS] + "…"
    return {
        "article_id": str(article.id),
        "title": article.title or "Untitled",
        "author": article.author or "",
        "source_platform": article.source_platform or "",
        "summary": article.summary or "",
        "content": content,
    }


async def _tool_list_recent(
    db: AsyncSession, user_id: UUID, days: int = 30, limit: int = 15
) -> dict:
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(days=days)
    from app.models import Article
    r = await db.execute(
        select(Article)
        .where(Article.user_id == user_id, Article.created_at >= since)
        .order_by(Article.created_at.desc())
        .limit(limit)
    )
    arts = list(r.scalars().all())
    out = []
    for a in arts:
        out.append({
            "article_id": str(a.id),
            "title": a.title or "Untitled",
            "source_platform": a.source_platform or "",
            "summary": (a.summary or "")[:150],
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })
    return {"count": len(out), "days": days, "results": out}


async def _tool_tag_articles(
    db: AsyncSession,
    user_id: UUID,
    article_ids: list,
    tags: list,
    reason: str = "",
) -> dict:
    """写工具：给一批文章追加标签。复用 mcp.py 的"按名找标签/没有就建"内核，
    但语义是【追加】而非覆盖，且对每篇文章幂等（已有的标签不重复加）。
    全程强制 user_id 隔离——只能动这个用户自己的文章和标签。"""
    from app.models import Article, Tag

    tag_names = [str(t).strip() for t in (tags or []) if str(t).strip()]
    if not tag_names:
        return {"error": "no tags given"}
    if not article_ids:
        return {"error": "no article_ids given"}

    report = []
    for aid_str in article_ids:
        try:
            aid = UUID(str(aid_str))
        except (ValueError, TypeError):
            report.append({"article_id": aid_str, "status": "invalid id"})
            continue
        r = await db.execute(
            select(Article).where(Article.id == aid, Article.user_id == user_id)
        )
        article = r.scalar_one_or_none()
        if not article:
            report.append({"article_id": aid_str, "status": "not found in your library"})
            continue
        existing = {t.name.lower() for t in article.tags}  # 幂等：记下已有标签
        added = []
        for tn in tag_names:
            if tn.lower() in existing:
                continue  # 已有这个标签，跳过，不重复加
            tag = (await db.execute(
                select(Tag).where(
                    func.lower(Tag.name) == tn.lower(), Tag.user_id == user_id
                )
            )).scalar_one_or_none()
            if not tag:  # 该用户名下没有这个标签 → 新建（标记为 AI 生成）
                tag = Tag(name=tn, is_ai_generated=True, user_id=user_id)
                db.add(tag)
                await db.flush()
            article.tags.append(tag)
            existing.add(tn.lower())
            added.append(tn)
        report.append({
            "article_id": aid_str,
            "title": article.title or "Untitled",
            "added": added,
            "status": "ok" if added else "already had all tags",
        })
    await db.commit()
    return {
        "action": "tag_articles",
        "tags": tag_names,
        "reason": reason,
        "ok_count": sum(1 for a in report if a.get("status") == "ok"),
        "articles": report,
    }


async def _tool_move_to_folder(
    db: AsyncSession,
    user_id: UUID,
    article_ids: list,
    folder_name: str,
    reason: str = "",
) -> dict:
    """写工具：把一批文章归类到某文件夹（不存在则新建）。复用 knowledge.py 的
    建文件夹 + articles.py batch-move 的两套现成写法，全程 user_id 隔离。"""
    from app.models import Article, Folder

    fname = (folder_name or "").strip()
    if not fname:
        return {"error": "folder_name is empty"}
    if not article_ids:
        return {"error": "no article_ids given"}

    # 找或建文件夹（按 user_id 隔离，幂等：同名不重复建）
    folder = (await db.execute(
        select(Folder).where(
            func.lower(Folder.name) == fname.lower(), Folder.user_id == user_id
        )
    )).scalar_one_or_none()
    created_folder = False
    if not folder:
        folder = Folder(name=fname, user_id=user_id)
        db.add(folder)
        await db.flush()
        created_folder = True

    moved, skipped = [], []
    for aid_str in article_ids:
        try:
            aid = UUID(str(aid_str))
        except (ValueError, TypeError):
            skipped.append({"article_id": aid_str, "status": "invalid id"})
            continue
        article = (await db.execute(
            select(Article).where(Article.id == aid, Article.user_id == user_id)
        )).scalar_one_or_none()
        if not article:
            skipped.append({"article_id": aid_str, "status": "not found in your library"})
            continue
        article.folder_id = folder.id
        moved.append({"article_id": aid_str, "title": article.title or "Untitled"})
    await db.commit()
    return {
        "action": "move_to_folder",
        "folder": fname,
        "folder_created": created_folder,
        "reason": reason,
        "moved_count": len(moved),
        "moved": moved,
        "skipped": skipped,
    }


async def _tool_save_url_to_folder(
    db: AsyncSession,
    user_id: UUID,
    url: str,
    folder_name: str,
    reason: str = "",
) -> dict:
    """采集一个 URL 并归类；已存在的文章只移动，不重复抓取。"""
    from app.models import Article, Folder
    from app.services.parser_service import extract_url_from_text, parser_service

    clean_url = extract_url_from_text(url or "") or (url or "").strip()
    fname = (folder_name or "").strip()
    if not clean_url.startswith(("http://", "https://")):
        return {"error": "url must be a valid http/https URL"}
    if not fname:
        return {"error": "folder_name is empty"}

    folder = (await db.execute(
        select(Folder).where(
            func.lower(Folder.name) == fname.lower(), Folder.user_id == user_id
        )
    )).scalar_one_or_none()
    folder_created = False
    if not folder:
        folder = Folder(name=fname, user_id=user_id)
        db.add(folder)
        await db.flush()
        folder_created = True

    article = (await db.execute(
        select(Article).where(Article.url == clean_url, Article.user_id == user_id)
    )).scalar_one_or_none()
    if article:
        article.folder_id = folder.id
        await db.commit()
        return {
            "action": "save_url_to_folder",
            "url": clean_url,
            "folder": fname,
            "folder_created": folder_created,
            "article_id": str(article.id),
            "title": article.title or "Untitled",
            "article_created": False,
            "reason": reason,
        }

    try:
        content_data = await parser_service.fetch_content(clean_url)
    except Exception as exc:
        await db.rollback()
        return {"error": f"failed to fetch URL: {exc}"}

    article = Article(
        url=clean_url,
        title=content_data.get("title") or "Untitled",
        raw_content=content_data.get("raw_content") or "",
        source_platform=content_data.get("platform"),
        author=content_data.get("author"),
        cover_image=content_data.get("cover_image"),
        folder_id=folder.id,
        user_id=user_id,
    )
    db.add(article)
    await db.commit()
    await db.refresh(article)

    # 与普通 URL 入库复用同一后台处理链：清洗、摘要、标签、向量和关联分析。
    from app.routers.articles import process_article_background
    task = asyncio.create_task(process_article_background(
        article.id,
        content_data.get("raw_content") or "",
        content_data.get("raw_html") or "",
        clean_url,
        None,
    ))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {
        "action": "save_url_to_folder",
        "url": clean_url,
        "folder": fname,
        "folder_created": folder_created,
        "article_id": str(article.id),
        "title": article.title or "Untitled",
        "article_created": True,
        "reason": reason,
    }


async def _tool_link_articles(
    db: AsyncSession,
    user_id: UUID,
    source_article_id: str,
    target_article_id: str,
    relation_type: str = "related",
    reason: str = "",
) -> dict:
    """写工具：在两篇文章间建一条有向知识图谱边。复用 KnowledgeEdge 模型 +
    graph_service 的防重逻辑（同一对 source→target 已存在则跳过），user_id 隔离。"""
    from app.models import Article, KnowledgeEdge

    valid = {"related", "prerequisite", "extends", "contradicts"}
    rtype = relation_type if relation_type in valid else "related"
    try:
        sid = UUID(str(source_article_id))
        tid = UUID(str(target_article_id))
    except (ValueError, TypeError):
        return {"error": "invalid article id (not a UUID)"}
    if sid == tid:
        return {"error": "source 和 target 不能是同一篇文章"}

    # 两篇都必须属于该用户
    rows = (await db.execute(
        select(Article.id, Article.title).where(
            Article.id.in_([sid, tid]), Article.user_id == user_id
        )
    )).all()
    found = {r[0]: r[1] for r in rows}
    if sid not in found or tid not in found:
        return {"error": "source 或 target 不在你的库里"}

    # 防重：同一对 (source, target) 已有边则跳过（与现有 graph_service 一致）
    existing = (await db.execute(
        select(KnowledgeEdge).where(
            KnowledgeEdge.source_article_id == sid,
            KnowledgeEdge.target_article_id == tid,
        )
    )).scalar_one_or_none()
    if existing:
        return {
            "action": "link_articles",
            "status": "already linked",
            "relation_type": existing.relation_type,
            "source_title": found[sid],
            "target_title": found[tid],
        }

    edge = KnowledgeEdge(
        source_article_id=sid,
        target_article_id=tid,
        relation_type=rtype,
        relation_desc=(reason or "")[:500],
        weight=0.6,
        user_id=user_id,
    )
    db.add(edge)
    await db.commit()
    return {
        "action": "link_articles",
        "status": "ok",
        "relation_type": rtype,
        "source_title": found[sid],
        "target_title": found[tid],
        "reason": reason,
    }


async def _tool_synthesize_concept(
    db: AsyncSession,
    user_id: UUID,
    concept_name: str,
    article_ids: Optional[list] = None,
    reason: str = "",
) -> dict:
    """写工具：把同一概念跨多篇合成一页『活词条』。直接复用现成的
    concept_service.synthesize_and_save（它内部调 LLM 合成 + upsert 落库）。"""
    from app.services.concept_service import synthesize_and_save

    name = (concept_name or "").strip()
    if not name:
        return {"error": "concept_name is empty"}
    try:
        page = await synthesize_and_save(
            db=db,
            user_id=user_id,
            name=name,
            seed_type="topic",
            seed_tag=None,
            article_ids=article_ids or None,
        )
    except ValueError as e:
        # 最常见：来源不足（库里相关文章 < 2 篇）
        return {"error": str(e)}
    content = (page.content or "")
    return {
        "action": "synthesize_concept",
        "concept_id": str(page.id),
        "name": page.name,
        "source_count": len(page.source_article_ids or []),
        "reason": reason,
        "content_preview": content[:300] + ("…" if len(content) > 300 else ""),
    }


async def _tool_configure_review(
    db: AsyncSession,
    user_id: UUID,
    enabled: bool = True,
    frequency_days: int = 7,
    time_of_day: str = "09:00",
    reason: str = "",
) -> dict:
    """写工具：配置用户级『定期复习简报』。复用 review_service.compute_next_send_at +
    review.py PUT /schedule 的 upsert 写法。注意是用户级周期推送，非 per-article。"""
    from app.models import ReviewSchedule
    from app.services.review_service import compute_next_send_at

    try:
        fd = max(1, min(90, int(frequency_days)))
    except (ValueError, TypeError):
        fd = 7
    tod = (time_of_day or "09:00").strip()
    if not re.match(r"^\d{1,2}:\d{2}$", tod):
        tod = "09:00"

    s = (await db.execute(
        select(ReviewSchedule).where(ReviewSchedule.user_id == user_id)
    )).scalar_one_or_none()
    next_at = compute_next_send_at(fd, tod) if enabled else None
    if s:
        s.enabled = bool(enabled)
        s.frequency_days = fd
        s.time_of_day = tod
        s.next_send_at = next_at
    else:
        s = ReviewSchedule(
            user_id=user_id, enabled=bool(enabled),
            frequency_days=fd, time_of_day=tod, next_send_at=next_at,
        )
        db.add(s)
    await db.commit()
    return {
        "action": "configure_review",
        "enabled": bool(enabled),
        "frequency_days": fd,
        "time_of_day": tod,
        "next_send_at": next_at.isoformat() if next_at else None,
        "reason": reason,
        "note": "已配置用户级定期复习简报；需已绑定微信账号才会真正推送。",
    }


async def _tool_find_duplicates(
    db: AsyncSession,
    user_id: UUID,
    threshold: float = 0.12,
    limit: int = 20,
) -> dict:
    """只读工具：找疑似重复文章。两路——(1) content_hash 完全相同（转载/同文不同链接）；
    (2) embedding 语义距离 < threshold 的配对。只列出，绝不删除。user_id 隔离。"""
    try:
        th = float(threshold)
    except (ValueError, TypeError):
        th = 0.12
    try:
        lim = max(1, min(100, int(limit)))
    except (ValueError, TypeError):
        lim = 20

    # (1) 正文完全相同（content_hash 分组）
    exact_sql = text("""
        SELECT array_agg(id::text) AS ids, array_agg(title) AS titles
        FROM articles
        WHERE user_id = :uid AND content_hash IS NOT NULL
        GROUP BY content_hash
        HAVING count(*) > 1
        LIMIT :lim
    """)
    exact_rows = (await db.execute(exact_sql, {"uid": user_id, "lim": lim})).fetchall()
    exact_dups = [
        {"article_ids": r[0], "titles": [t or "Untitled" for t in r[1]]}
        for r in exact_rows
    ]

    # (2) 语义近似配对（自连接，a.id<b.id 去掉自配与重复方向；个人库规模可接受）
    sem_sql = text("""
        SELECT a.id::text, a.title, b.id::text, b.title,
               (a.embedding <-> b.embedding) AS dist
        FROM articles a JOIN articles b
          ON a.id < b.id
        WHERE a.user_id = :uid AND b.user_id = :uid
          AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
          AND (a.embedding <-> b.embedding) < :th
        ORDER BY dist ASC
        LIMIT :lim
    """)
    sem_rows = (await db.execute(
        sem_sql, {"uid": user_id, "th": th, "lim": lim}
    )).fetchall()
    semantic_dups = [
        {
            "pair": [
                {"article_id": r[0], "title": r[1] or "Untitled"},
                {"article_id": r[2], "title": r[3] or "Untitled"},
            ],
            "distance": round(float(r[4]), 4),
        }
        for r in sem_rows
    ]

    return {
        "action": "find_duplicates",
        "threshold": th,
        "exact_duplicate_groups": exact_dups,
        "semantic_duplicate_pairs": semantic_dups,
        "note": "只读结果，未删除任何文章。是否处理由用户决定。",
    }


# ── 联网工具（只读，对应能力 E）────────────────────────────────────
async def _tool_web_search(query: str, max_results: int = 5) -> dict:
    """只读工具：Tavily 联网搜索。key 从 get_effective_config('search') 读，
    即系统设置→联网搜索里配的那项（可在界面改）。"""
    from app.config_manager import get_effective_config

    if not (query or "").strip():
        return {"error": "query is empty"}
    cfg = get_effective_config("search")
    api_key = cfg.get("api_key", "")
    api_base = (cfg.get("api_base") or "https://api.tavily.com").rstrip("/")
    if not api_key:
        return {"error": "联网搜索未配置：请在『系统设置 → 联网搜索 (Tavily)』里填 API Key。"}
    try:
        mr = max(1, min(10, int(max_results)))
    except (ValueError, TypeError):
        mr = 5
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{api_base}/search",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"query": query, "max_results": mr, "search_depth": "basic"},
            )
        if resp.status_code != 200:
            return {"error": f"Tavily {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": (r.get("content", "") or "")[:500],
            }
            for r in (data.get("results") or [])
        ]
        return {
            "action": "web_search",
            "count": len(results),
            "answer": data.get("answer"),  # Tavily 可能直接给一句综合答案
            "results": results,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def _tool_web_read(url: str) -> dict:
    """只读工具：抓取某 URL 正文。直接复用现成的 parser_service（项目抓各平台
    正文的核心），喂 URL 出清洗后的正文。"""
    from app.services.parser_service import parser_service

    if not url or not str(url).startswith("http"):
        return {"error": "invalid url（需以 http/https 开头）"}
    try:
        data = await parser_service.fetch_content(url)
    except Exception as e:
        return {"error": f"抓取失败: {type(e).__name__}: {e}"}
    raw = data.get("raw_content") or ""
    try:
        content = parser_service.clean_to_markdown(raw, platform=data.get("platform", "")) if raw else ""
    except Exception:
        content = raw
    content = (content or raw or "").strip()
    if len(content) > MAX_TOOL_RESULT_CHARS:
        content = content[:MAX_TOOL_RESULT_CHARS] + "…"
    return {
        "action": "web_read",
        "title": data.get("title", ""),
        "url": url,
        "platform": data.get("platform", ""),
        "content": content,
    }


async def _tool_remember(
    db: AsyncSession, user_id: UUID, content: str, kind: str = "fact"
) -> dict:
    """工具：写一条长期记忆。复用 agent_memory.add_user_memory（幂等去重）。"""
    from app.services.agent_memory import add_user_memory
    return await add_user_memory(db, user_id, content, kind=kind, source="agent")


async def _execute_tool(
    name: str, args: dict, db: AsyncSession, user_id: UUID, confirmed: bool = False
) -> dict:
    # ── 确认闸：写工具未经确认时，不执行，只返回"预演"让模型去请用户确认 ──
    if name in WRITE_TOOLS and not confirmed:
        return {
            "status": "pending_confirmation",
            "tool": name,
            "preview": args,
            "note": (
                "未执行。这是写操作的预演。请停止调用工具，用自然语言把"
                "「将对哪些文章做什么、为什么」清楚告诉用户，并请其确认后再执行。"
            ),
        }
    try:
        if name == "search_library":
            return await _tool_search_library(
                db, user_id, args["query"], args.get("top_k", LIBRARY_TOP_K)
            )
        if name == "read_article":
            return await _tool_read_article(db, user_id, args["article_id"])
        if name == "list_recent_articles":
            return await _tool_list_recent(
                db, user_id, args.get("days", 30), args.get("limit", 15)
            )
        if name == "tag_articles":
            return await _tool_tag_articles(
                db, user_id,
                args.get("article_ids", []), args.get("tags", []),
                args.get("reason", ""),
            )
        if name == "move_to_folder":
            return await _tool_move_to_folder(
                db, user_id,
                args.get("article_ids", []), args.get("folder_name", ""),
                args.get("reason", ""),
            )
        if name == "save_url_to_folder":
            return await _tool_save_url_to_folder(
                db, user_id,
                args.get("url", ""), args.get("folder_name", ""),
                args.get("reason", ""),
            )
        if name == "link_articles":
            return await _tool_link_articles(
                db, user_id,
                args.get("source_article_id", ""), args.get("target_article_id", ""),
                args.get("relation_type", "related"), args.get("reason", ""),
            )
        if name == "synthesize_concept":
            return await _tool_synthesize_concept(
                db, user_id,
                args.get("concept_name", ""), args.get("article_ids"),
                args.get("reason", ""),
            )
        if name == "configure_review":
            return await _tool_configure_review(
                db, user_id,
                args.get("enabled", True), args.get("frequency_days", 7),
                args.get("time_of_day", "09:00"), args.get("reason", ""),
            )
        if name == "find_duplicates":
            return await _tool_find_duplicates(
                db, user_id,
                args.get("threshold", 0.12), args.get("limit", 20),
            )
        if name == "web_search":
            return await _tool_web_search(
                args.get("query", ""), args.get("max_results", 5),
            )
        if name == "web_read":
            return await _tool_web_read(args.get("url", ""))
        if name == "remember":
            return await _tool_remember(
                db, user_id, args.get("content", ""), args.get("kind", "fact"),
            )
        return {"error": f"unknown tool: {name}"}
    except KeyError as e:
        return {"error": f"missing required argument: {e}"}
    except Exception as e:
        logger.exception(f"tool {name} crashed: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


# ── LLM call helper (function calling) ───────────────────────────────
async def _call_llm_with_tools(
    messages: list, tools: list
) -> dict:
    """One round-trip to the LLM. Returns the raw `message` object."""
    from app.config_manager import get_effective_config
    cfg = get_effective_config("embedding")  # same SF account
    api_key = cfg.get("api_key", "") or os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key:
        raise RuntimeError("SiliconFlow api_key missing (embedding group)")

    payload = {
        "model": SF_MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.3,
        "max_tokens": 2048,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        last_error = ""
        for attempt in range(len(SF_RETRY_DELAYS) + 1):
            resp = await client.post(
                f"{SF_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                return choice.get("message") or {}
            last_error = resp.text[:400]
            if resp.status_code in SF_RETRY_STATUS_CODES and attempt < len(SF_RETRY_DELAYS):
                delay = SF_RETRY_DELAYS[attempt]
                logger.warning(
                    "SF chat busy status=%s attempt=%s retry_in=%.1fs body=%s",
                    resp.status_code,
                    attempt + 1,
                    delay,
                    last_error,
                )
                await asyncio.sleep(delay)
                continue
            if resp.status_code in SF_RETRY_STATUS_CODES:
                raise LLMServiceBusyError(
                    f"SF chat temporarily unavailable ({resp.status_code}): {last_error}"
                )
            raise RuntimeError(
                f"SF chat HTTP {resp.status_code}: {last_error}"
            )
    raise LLMServiceBusyError(f"SF chat temporarily unavailable: {last_error}")


async def _stream_llm_with_tools(messages: list, tools: list, tool_choice: str = "auto"):
    """流式版 LLM 调用（async generator）。

    yield ("token", delta_str)  —— 模型生成的正文增量，实时往外吐
    yield ("done", {"content": 全文, "tool_calls": [...]}) —— 本轮结束，给出累积结果

    tool_choice="required" 时强制模型必须调用工具（用于第 0 步，杜绝"光预告不动手"）。
    工具调用的 arguments 在流里是一片片来的（按 index 累积），最后拼成完整 tool_calls。
    """
    from app.config_manager import get_effective_config
    cfg = get_effective_config("embedding")  # same SF account
    api_key = cfg.get("api_key", "") or os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key:
        raise RuntimeError("SiliconFlow api_key missing (embedding group)")

    payload = {
        "model": SF_MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "temperature": 0.3,
        "max_tokens": 2048,
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        last_error = ""
        for attempt in range(len(SF_RETRY_DELAYS) + 1):
            content_parts: List[str] = []
            tc_acc: Dict[int, dict] = {}   # index -> {"id","name","args"}
            async with client.stream(
                "POST",
                f"{SF_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "ignore")[:400]
                    last_error = body
                    if resp.status_code in SF_RETRY_STATUS_CODES and attempt < len(SF_RETRY_DELAYS):
                        delay = SF_RETRY_DELAYS[attempt]
                        logger.warning(
                            "SF stream chat busy status=%s attempt=%s retry_in=%.1fs body=%s",
                            resp.status_code,
                            attempt + 1,
                            delay,
                            body,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if resp.status_code in SF_RETRY_STATUS_CODES:
                        raise LLMServiceBusyError(
                            f"SF chat temporarily unavailable ({resp.status_code}): {body}"
                        )
                    raise RuntimeError(f"SF chat HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    dc = delta.get("content")
                    if dc:
                        content_parts.append(dc)
                        yield ("token", dc)
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        acc = tc_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["args"] += fn["arguments"]
                break
        else:
            raise LLMServiceBusyError(f"SF chat temporarily unavailable: {last_error}")

    tool_calls = [
        {
            "id": v["id"] or f"call_{k}",
            "type": "function",
            "function": {"name": v["name"], "arguments": v["args"]},
        }
        for k, v in sorted(tc_acc.items())
        if v["name"]
    ]
    yield ("done", {"content": "".join(content_parts), "tool_calls": tool_calls})


# ── Main agent loop ──────────────────────────────────────────────────
SYSTEM_PROMPT = """你是用户的个人研究助理 Agent。你可以调用工具，在用户的个人知识库里搜索、阅读文章，并对工具返回的证据做综合推理。

工具使用原则：
0. **直接行动，绝不预告** —— 当你要用工具时，**直接发起工具调用**。**严禁**先说"我将要搜索…""请稍等…""让我先查一下…""接下来我会…"然后就停下——这种只预告不执行会让任务彻底卡死。每一轮要么真的调用工具，要么给出最终答案，**不允许只用文字描述你即将做的动作却不做**。
1. **不要凭记忆回答** —— 用户问的问题必须基于他们自己库里的内容（或联网）。**先调 search_library / list_recent_articles / web_search 等工具**。
2. **多步推理** —— search_library 给你 snippet 不够时，对最相关的几条调 read_article 拿全文。
3. **聚合不重复** —— 不要对同一查询反复调用同一工具。每次调用应该探索**新角度**或**深入某一篇**。
4. **达到充分信息后**直接给最终答案；不要无意义凑步数。

写操作纪律（直接调用，系统会自动拦截确认 —— 非常重要）：
- 你有几个会改变用户数据的**写工具**：
  · tag_articles —— 给文章追加标签
  · move_to_folder —— 把文章归类到文件夹（不存在会新建）
  · save_url_to_folder —— 把新链接入库到指定文件夹，或把已入库链接移到该文件夹
  · link_articles —— 在两篇文章间建知识图谱关系
  · synthesize_concept —— 把同一概念跨多篇合成一页概念页
  · configure_review —— 配置用户级定期复习简报（开关/频率/时间）
- 当你判断该执行某个写操作时，**直接调用对应的写工具**（带上准确的 article_ids 和 reason）。
  **绝对不要自己先用一段文字去征求用户同意、然后停下不调工具** —— 系统有一道"确认闸"会自动拦下这次调用并让用户在界面上点「确认执行」，你不需要自己模拟这个过程。
- 写工具若返回 `status="pending_confirmation"`，表示系统已经拦下、正等用户点确认。此时你只需**用一句话说明"我准备做什么、对哪些对象、为什么"**，然后停下、不要再调任何工具。用户点「确认执行」后系统会自动重试，那次才真正落库。
- 一句话：**该写就直接调写工具**，确认这件事交给系统，不要自己用文字代替。
- 用户说『创建文件夹 X，把链接 Y 入库到该文件夹』或『把链接 Y 入库到 X 文件夹』时，
  直接调用 save_url_to_folder；不要先调用 move_to_folder，也不要要求链接必须已经在库里。
- 读类工具无需确认，可直接调用：search_library / read_article / list_recent_articles / find_duplicates（找疑似重复，只列不删）/ web_search / web_read（联网）/ remember（记长期记忆）。

能力边界（别承诺做不到的事）：
- 复习只能配置【用户级】定期简报（configure_review：开关/频率/时间），**不能给"某几篇文章单独排复习"**。用户这么要求时，如实说明只支持用户级，并问要不要改设用户级简报——不要假装能逐篇排。
- 去重只能【找出】疑似重复（find_duplicates），**不能删除或合并文章**。如实说明，删不删由用户自己在界面操作。

记忆（让你越来越懂这个用户）：
- 系统提示词里若出现【关于这个用户，你已经知道】，那是长期记忆，回答时自然用上，别假装不认识。
- 对话历史里的上文要接住，用户说"它/上面那篇/刚才"时指代的是历史里的内容。
- **聚焦用户当前这一条消息**。对话历史只是背景，**不要主动去重做历史里已经完成、或之前在等确认的任务**；用户问了新问题就答新问题。只有当前这条明确是在追问、确认或延续上文时，才接着上文做。
- 当你了解到关于用户的【稳定、长期】事实（身份、长期偏好、反复确认的口径），用 remember 记下来。**宁缺毋滥**：一次性的、会变的、本轮才相关的，不要记。

信息来源策略（库内优先，必要时联网）：
1. **优先查用户自己的库**（search_library）。库里能答的，就用库里的，并标注来源文章。
2. 当库里没有、或问题明显需要**最新/外部**信息时，再用 web_search 联网；对最相关的结果可用 web_read 读全文。
3. 当用户要**对比 / 查证 / 补充**时：**先 search_library 看库里的观点，再 web_search 看外部最新说法**，综合时**明确区分**：
   「你库里的《X》认为…」 vs 「外部最新资料（来源 URL）认为…」，并指出异同。
4. 联网得到的内容**不写入用户的库**，只用于本次回答；引用外部信息时给出来源 URL。

最终答案要求：
- 中文输出，结构化（开头核心结论 → 分点展开 → 引用）
- 每个论点后用 [《文章标题》] 标注来源；不要编造材料以外的内容
- 300-500 字，便于阅读
- 如果库里材料不足，**明说"库里材料不足"**而不是编造

你最多有 8 个工具调用回合。请高效。"""


async def run_tool_agent(
    db: AsyncSession,
    query: str,
    user_id: UUID,
    confirmed: bool = False,
    session_id: Optional[str] = None,
) -> AsyncIterator[AgentEvent]:
    """Yield progress events through the tool-using agent loop.

    confirmed=False（默认）：写工具被"确认闸"拦截，只预演不落库，agent 会把
    计划讲给用户听。用户点"执行"后，前端用 confirmed=True 再请求一次，同一个
    写操作这次才真正落库。

    session_id（阶段⑤）：给了就带回该会话最近历史（短期记忆）；同时把该用户的
    长期记忆注入系统提示词。一轮结束把 user/assistant 落库。不给则行为同以前（无记忆）。"""
    yield _emit("start", "🤖 智能体启动，将自主选择工具…")

    # 长期记忆：拼进系统提示词（每次对话开头全量注入）
    system_content = SYSTEM_PROMPT
    if session_id:
        from app.services.agent_memory import load_long_term_block, load_history, save_turn
        try:
            ltm = await load_long_term_block(db, user_id)
            if ltm:
                system_content = SYSTEM_PROMPT + ltm
        except Exception as e:
            logger.warning(f"load long-term memory failed: {e}")

    messages: List[Dict] = [{"role": "system", "content": system_content}]

    # 短期记忆：带回该会话最近几轮对话
    if session_id:
        try:
            history = await load_history(db, UUID(str(session_id)), user_id)
            messages.extend(history)
        except Exception as e:
            logger.warning(f"load history failed: {e}")

    messages.append({"role": "user", "content": query})
    tool_call_count = 0
    nudged = False        # 安全网：只纠正一次"光预告不动手"
    force_tool_next = False  # 检测到光预告后，下一轮才强制调工具（平时不强制，提问就老实答）

    try:
        for step in range(MAX_STEPS):
            # 默认 auto（提问直接答、不硬塞工具）；只有上一轮"光预告不动手"才升级为 required
            tchoice = "required" if force_tool_next else "auto"
            force_tool_next = False
            assistant_msg: Dict = {}
            async for kind, payload in _stream_llm_with_tools(messages, TOOL_SCHEMAS, tool_choice=tchoice):
                if kind == "token":
                    yield _emit("token", "", data={"delta": payload})
                else:  # done
                    assistant_msg = payload
            tool_calls = assistant_msg.get("tool_calls") or []
            content = (assistant_msg.get("content") or "").strip()

            if not tool_calls:
                # Final answer
                answer = content or "（模型未给出最终答案）"
                # 安全网：模型"光预告不动手"——要么口头说"我将搜索…稍等"，要么把工具调用
                # 写成 JSON 文本贴出来假装调用，却没真发起 tool_calls → 下一轮升级 required 逼它真动手。
                NARRATION = (
                    "我将", "我会", "我来", "让我", "我先", "我现在", "我这就", "这就为",
                    "稍等", "稍候", "请稍", "马上", "即将", "接下来我", "正在为", "正在搜",
                    "现在为你", "现在为您", "我将进行", "我来搜", "我去搜", "让我先", "进行搜索",
                )
                TOOL_NAMES = (
                    "search_library", "read_article", "list_recent_articles", "find_duplicates",
                    "web_search", "web_read", "tag_articles", "move_to_folder",
                    "save_url_to_folder", "link_articles",
                    "synthesize_concept", "configure_review", "remember",
                )
                fake_tool_call = (("\"name\"" in answer or "调用工具" in answer or "调用 " in answer)
                                  and any(t in answer for t in TOOL_NAMES))
                narration = any(p in answer for p in NARRATION)
                if (tool_call_count == 0 and not nudged
                        and (fake_tool_call or (narration and len(answer) < 400))):
                    nudged = True
                    force_tool_next = True   # 下一轮强制真的调工具
                    messages.append({"role": "assistant", "content": answer})
                    messages.append({"role": "user", "content":
                        "不要只用文字描述或贴 JSON 假装调用工具，请立刻真正发起工具调用去完成；"
                        "如果这个问题用历史/上下文就能直接回答，就直接给出完整答案。"})
                    continue
                # 短期记忆：把这一轮对话落库（仅在有会话时）
                if session_id:
                    try:
                        await save_turn(db, UUID(str(session_id)), query, answer)
                    except Exception as e:
                        logger.warning(f"save_turn failed: {e}")
                yield _emit("final", "完成", data={
                    "answer": answer,
                    "steps": step + 1,
                    "tool_calls": tool_call_count,
                    "session_id": str(session_id) if session_id else None,
                })
                return

            # Re-attach the assistant message containing tool_calls (REQUIRED by API)
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            # Execute every requested tool call (could be multiple in parallel)
            for tc in tool_calls:
                tool_call_count += 1
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                yield _emit(
                    "tool_call",
                    f"🔧 调用 {name}（{_summarize_args(args)}）",
                    data={"name": name, "args": args},
                )
                result = await _execute_tool(name, args, db, user_id, confirmed=confirmed)
                if result.get("status") == "pending_confirmation":
                    # 写操作被确认闸拦下：发一个 confirm 事件给前端渲染确认 UI
                    yield _emit(
                        "confirm",
                        f"⏸ 待确认：{name}（{_summarize_args(args)}）",
                        data={"name": name, "args": args, "reason": args.get("reason", "")},
                    )
                else:
                    yield _emit(
                        "tool_result",
                        _summarize_result(name, result),
                    )
                # Feed result back to LLM
                result_str = json.dumps(result, ensure_ascii=False)
                if len(result_str) > MAX_TOOL_RESULT_CHARS:
                    result_str = result_str[:MAX_TOOL_RESULT_CHARS] + '..."}'
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result_str,
                })

        yield _emit("error", f"⚠️ 智能体执行 {MAX_STEPS} 步仍未给最终答案，已停止")
    except LLMServiceBusyError as e:
        logger.warning(f"tool_agent upstream busy: {e}")
        yield _emit("error", "⚠️ 模型服务暂时繁忙，刚刚已经自动重试但仍失败。请稍后再发一次，我会继续处理。")
    except Exception as e:
        logger.exception(f"tool_agent crashed: {e}")
        yield _emit("error", f"⚠️ 智能体出错：{type(e).__name__}: {e}")


def _summarize_args(args: dict) -> str:
    """Compact one-liner for the progress message."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 30:
            v = v[:30] + "…"
        parts.append(f"{k}={v}")
    return ", ".join(parts)


def _summarize_result(name: str, result: dict) -> str:
    if "error" in result:
        return f"⚠️ {name} 错误：{result['error']}"
    if name == "search_library":
        cnt = result.get("count", 0)
        if cnt == 0:
            return "🔍 search_library：库里没找到相关文章"
        titles = "、".join(
            r.get("title", "")[:18] for r in (result.get("results") or [])[:3]
        )
        return f"🔍 search_library：{cnt} 篇 — {titles}"
    if name == "read_article":
        t = result.get("title", "Untitled")[:30]
        return f"📖 read_article：《{t}》"
    if name == "list_recent_articles":
        return f"📅 list_recent_articles：{result.get('count', 0)} 篇近 {result.get('days', 30)} 天"
    if name == "tag_articles":
        n = result.get("ok_count", 0)
        tags = "、".join(result.get("tags", []))
        return f"🏷 tag_articles：{n} 篇已打上 [{tags}]"
    if name == "move_to_folder":
        n = result.get("moved_count", 0)
        nf = "（新建）" if result.get("folder_created") else ""
        return f"📁 move_to_folder：{n} 篇 → 「{result.get('folder', '')}」{nf}"
    if name == "save_url_to_folder":
        nf = "（新建文件夹）" if result.get("folder_created") else ""
        action = "已入库" if result.get("article_created") else "已归类"
        return (
            f"📥 save_url_to_folder：{action}《{(result.get('title') or 'Untitled')[:24]}》"
            f"→「{result.get('folder', '')}」{nf}"
        )
    if name == "link_articles":
        if result.get("status") == "already linked":
            return f"🔗 link_articles：已存在关系，跳过"
        return (
            f"🔗 link_articles：{result.get('relation_type', '')} —"
            f"《{(result.get('source_title') or '')[:14]}》→《{(result.get('target_title') or '')[:14]}》"
        )
    if name == "synthesize_concept":
        return (
            f"🧩 synthesize_concept：《{result.get('name', '')}》合成完成"
            f"（{result.get('source_count', 0)} 篇来源）"
        )
    if name == "configure_review":
        if not result.get("enabled"):
            return "🔕 configure_review：已关闭定期复习"
        return (
            f"🔔 configure_review：每 {result.get('frequency_days', 7)} 天 "
            f"{result.get('time_of_day', '09:00')} 推送"
        )
    if name == "find_duplicates":
        ne = len(result.get("exact_duplicate_groups") or [])
        ns = len(result.get("semantic_duplicate_pairs") or [])
        return f"🔎 find_duplicates：完全重复 {ne} 组 / 语义近似 {ns} 对"
    if name == "web_search":
        cnt = result.get("count", 0)
        titles = "、".join(
            (r.get("title", "") or "")[:18] for r in (result.get("results") or [])[:3]
        )
        return f"🌐 web_search：{cnt} 条 — {titles}"
    if name == "web_read":
        t = (result.get("title", "") or "Untitled")[:30]
        return f"📄 web_read：《{t}》"
    if name == "remember":
        if result.get("status") == "already remembered":
            return "🧠 remember：已记过，跳过"
        return f"🧠 remember：记住了 — {(result.get('content') or '')[:40]}"
    return f"✓ {name}"
