"""MCP server(Phase 6·G) —— 把知识库暴露给外部 AI agent(Claude 等)。

借鉴 llm_wiki 的「MCP server for external AI agent integration」。
实现:自包含的 Streamable-HTTP MCP(JSON-RPC 2.0 over POST,JSON 响应,无 SSE 流)。
不引第三方 SDK,鉴权复用 Bearer token(同步 token / 登录 JWT 皆可),按 user 隔离。

连接方式(外部 agent):
  endpoint: https://<host>/api/mcp   header: Authorization: Bearer <token>

支持方法:initialize / notifications/initialized / ping / tools/list / tools/call。
工具:search_knowledge / get_article / knowledge_insights / list_recent_articles。
"""
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.article import Article

logger = logging.getLogger("trove.mcp")
router = APIRouter(prefix="/api", tags=["mcp"])

SERVER_INFO = {"name": "Trove AI Knowledge Base", "version": "1.0.0"}
DEFAULT_PROTOCOL = "2024-11-05"

# ── Tool schemas (advertised via tools/list) ──────────────────────────────
TOOLS = [
    {
        "name": "search_knowledge",
        "description": "语义检索用户的知识库,返回最相关的文章(标题/摘要/相似度)。用于回答'我之前存过关于X的内容吗'这类问题。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索的自然语言问题或关键词"},
                "top_k": {"type": "integer", "description": "返回条数,默认 5", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_article",
        "description": "按 id 取单篇文章全文(标题/摘要/要点/正文/原文链接)。通常先用 search_knowledge 拿到 id。",
        "inputSchema": {
            "type": "object",
            "properties": {"article_id": {"type": "string", "description": "文章 UUID"}},
            "required": ["article_id"],
        },
    },
    {
        "name": "knowledge_insights",
        "description": "返回知识库的图谱洞察:主题簇、核心枢纽、意外连接(该连未连的强关联)、知识缺口(孤岛文章)。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_recent_articles",
        "description": "列出最近收藏的文章(标题/平台/时间),用于了解用户近期在关注什么。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "条数,默认 10", "default": 10}},
        },
    },
]


# ── Tool implementations ───────────────────────────────────────────────────
async def _tool_search_knowledge(db: AsyncSession, user: User, args: Dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "请提供 query。"
    top_k = max(1, min(int(args.get("top_k") or 5), 20))
    from app.services.ai_service import llm_service
    emb = await llm_service.get_embedding(query, emb_type="query")
    emb_str = "[" + ",".join(str(v) for v in emb) + "]"
    rows = (await db.execute(
        text(f"""
            SELECT id, title, summary, (embedding <-> '{emb_str}'::vector) AS distance
            FROM articles
            WHERE embedding IS NOT NULL AND user_id = :uid
            ORDER BY embedding <-> '{emb_str}'::vector
            LIMIT :k
        """),
        {"uid": user.id, "k": top_k},
    )).fetchall()
    if not rows:
        return "知识库中没有匹配内容。"
    lines = []
    for aid, title, summary, dist in rows:
        score = round(max(0.0, 1.0 - float(dist) / 10.0), 3)
        lines.append(f"- [{score}] {title or 'Untitled'} (id={aid})\n  {(summary or '')[:120]}")
    return "\n".join(lines)


async def _tool_get_article(db: AsyncSession, user: User, args: Dict) -> str:
    from uuid import UUID
    try:
        aid = UUID(str(args.get("article_id")))
    except (ValueError, TypeError):
        return "无效的 article_id。"
    a = await db.get(Article, aid)
    if not a or a.user_id != user.id:
        return "未找到该文章(或无权访问)。"
    body = (a.clean_content or a.raw_content or "")[:8000]
    parts = [
        f"# {a.title or 'Untitled'}",
        f"平台: {a.source_platform or 'web'} | 作者: {a.author or '未知'}",
    ]
    if a.url:
        parts.append(f"原文: {a.url}")
    if a.summary:
        parts.append(f"\n摘要: {a.summary}")
    if a.key_points:
        parts.append("要点:\n" + "\n".join(f"- {p}" for p in a.key_points))
    parts.append("\n正文:\n" + body)
    return "\n".join(parts)


async def _tool_knowledge_insights(db: AsyncSession, user: User, args: Dict) -> str:
    from app.services.graph_insights import compute_insights
    ins = await compute_insights(db, user.id)
    if ins.get("empty"):
        return "图谱还太小,暂无洞察。"
    s = ins["stats"]
    out = [f"文章 {s['articles']} · 关联 {s['edges']} · 主题簇 {s['communities']} · 孤岛 {s['orphans']}"]
    if ins["communities"]:
        out.append("主题簇:" + "、".join(f"{c['label']}({c['size']})" for c in ins["communities"][:8]))
    if ins["surprising_links"]:
        out.append("意外连接:")
        for sl in ins["surprising_links"][:5]:
            tag = "[跨主题]" if sl["cross_community"] else ""
            out.append(f"  {tag}《{sl['source']['title'][:30]}》↔《{sl['target']['title'][:30]}》")
    if ins["hubs"]:
        out.append("核心枢纽:" + "、".join(f"《{h['title'][:24]}》" for h in ins["hubs"][:5]))
    if (ins["gaps"]["orphan_count"] or 0) > 0:
        out.append(f"知识缺口:{ins['gaps']['orphan_count']} 篇孤岛文章未建立关联")
    return "\n".join(out)


async def _tool_list_recent(db: AsyncSession, user: User, args: Dict) -> str:
    limit = max(1, min(int(args.get("limit") or 10), 50))
    rows = (await db.execute(
        select(Article.id, Article.title, Article.source_platform, Article.created_at)
        .where(Article.user_id == user.id)
        .order_by(Article.created_at.desc())
        .limit(limit)
    )).fetchall()
    if not rows:
        return "知识库为空。"
    return "\n".join(
        f"- {title or 'Untitled'} [{plat or 'web'}] (id={aid})"
        for aid, title, plat, _ in rows
    )


_TOOL_FUNCS = {
    "search_knowledge": _tool_search_knowledge,
    "get_article": _tool_get_article,
    "knowledge_insights": _tool_knowledge_insights,
    "list_recent_articles": _tool_list_recent,
}


# ── JSON-RPC helpers ───────────────────────────────────────────────────────
def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Streamable-HTTP MCP(JSON-RPC over POST,JSON 响应)。鉴权:Bearer token。"""
    # 全 app 的 get_current_user 在无 token 时会回退到"第一个活跃用户"(给浏览器直连用)。
    # MCP 是对外 agent 入口,必须显式要求 token,杜绝匿名访问默认用户的知识库。
    if not request.headers.get("authorization"):
        return JSONResponse(
            _err(None, -32001, "Unauthorized: Bearer token required"),
            status_code=401,
        )
    try:
        msg = await request.json()
    except Exception:
        return JSONResponse(_err(None, -32700, "Parse error"), status_code=400)

    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # 通知(无 id):initialized 等 → 202 无体
    if req_id is None and method and method.startswith("notifications/"):
        return Response(status_code=202)

    if method == "initialize":
        proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return JSONResponse(_ok(req_id, {
            "protocolVersion": proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }))

    if method == "ping":
        return JSONResponse(_ok(req_id, {}))

    if method == "tools/list":
        return JSONResponse(_ok(req_id, {"tools": TOOLS}))

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = _TOOL_FUNCS.get(name)
        if not fn:
            return JSONResponse(_err(req_id, -32602, f"Unknown tool: {name}"))
        try:
            text_out = await fn(db, current_user, args)
            return JSONResponse(_ok(req_id, {
                "content": [{"type": "text", "text": text_out}],
                "isError": False,
            }))
        except Exception as e:
            logger.exception(f"mcp tool {name} failed: {e}")
            return JSONResponse(_ok(req_id, {
                "content": [{"type": "text", "text": f"工具执行出错:{type(e).__name__}: {e}"}],
                "isError": True,
            }))

    return JSONResponse(_err(req_id, -32601, f"Method not found: {method}"))
