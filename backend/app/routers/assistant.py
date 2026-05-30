"""
RAG-powered AI Assistant — semantic Q&A with citation tracing.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import BaseModel
from typing import List, Optional
import json
import os
import httpx

from ..database import get_db
from app.services.ai_service import llm_service
from app.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/assistant", tags=["assistant"])


class AskRequest(BaseModel):
    question: str
    top_k: int = 5
    article_id: Optional[str] = None  # 锁定单篇:仅基于该文章整篇正文回答,不做全库检索


class Citation(BaseModel):
    article_id: str
    title: str
    chunk: str
    relevance_score: float


class AskResponse(BaseModel):
    answer: str
    citations: List[Citation]


def _get_llm_config():
    """Read LLM config using config_manager (returns real API key)."""
    from app.config_manager import get_llm_config as _get_cfg
    try:
        cfg = _get_cfg()
        return {
            "provider": cfg.get("provider", "siliconflow"),
            "api_key": cfg.get("api_key", ""),
            "api_base": cfg.get("api_base", "https://api.deepseek.com/v1"),
            "model": cfg.get("model", "deepseek-chat"),
        }
    except Exception:
        pass
    
    # Final fallback
    return {
        "provider": "siliconflow",
        "api_key": os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("OPENAI_API_KEY", ""),
        "api_base": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
    }


async def _call_llm(prompt: str, system: str) -> str:
    """Call the configured OpenAI-compatible chat endpoint and return the answer text."""
    llm_config = _get_llm_config()
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{llm_config['api_base']}/chat/completions",
            headers={
                "Authorization": f"Bearer {llm_config['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": llm_config["model"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 2048,
            },
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _answer_single_article(req: AskRequest, session: AsyncSession, current_user: User) -> AskResponse:
    """本文问答:把指定文章整篇正文塞进上下文,严格基于该篇回答(不做全库检索)。"""
    row = (await session.execute(
        text("SELECT id, title, clean_content, raw_content FROM articles WHERE id = :aid AND user_id = :uid"),
        {"aid": req.article_id, "uid": current_user.id},
    )).fetchone()
    if not row:
        return AskResponse(answer="未找到该文章，或你无权访问。", citations=[])

    article_id, title, clean_content, raw_content = row
    content = (clean_content or raw_content or "").strip()
    if not content:
        return AskResponse(answer="这篇文章还没有可用的正文内容，无法基于它回答。", citations=[])

    # 整篇入上下文(留出回答与 prompt 余量),长文按字符截断
    context = content[:12000]
    truncated = len(content) > 12000

    prompt = f"""请仅依据下面这篇文章的内容回答用户的问题。

要求：
1. 只能基于本文内容作答，不要引入文章之外的知识或编造
2. 若本文内容不足以回答，请明确说明"本文未涉及"
3. 回答简洁、准确，可适当引用原文关键句
{"4. 注意：本文较长，提供给你的是前半部分内容，若答案可能在后文，请说明" if truncated else ""}

--- 文章《{title or "Untitled"}》 ---
{context}
--- 结束 ---

用户问题: {req.question}

请回答："""

    answer = await _call_llm(prompt, "你是一个阅读助手，严格基于用户当前正在阅读的这篇文章作答。")
    citations = [Citation(
        article_id=str(article_id),
        title=title or "Untitled",
        chunk=(context[:2000] + "...") if len(context) > 2000 else context,
        relevance_score=1.0,
    )]
    return AskResponse(answer=answer, citations=citations)


@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest, session: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """RAG pipeline: embed question → semantic search → build prompt → LLM answer"""

    # 传了 article_id → 切到"本文问答"模式:整篇正文入上下文,不做全库向量检索
    if req.article_id:
        return await _answer_single_article(req, session, current_user)
    
    # Step 1: Embed the question
    question_embedding = await llm_service.get_embedding(req.question, emb_type="query")
    emb_str = "[" + ",".join(str(v) for v in question_embedding) + "]"

    # Step 2: Semantic search — find top_k most relevant articles (scoped to user)
    search_sql = text(f"""
        SELECT id, title, clean_content, raw_content,
               (embedding <-> '{emb_str}'::vector) AS distance
        FROM articles
        WHERE embedding IS NOT NULL AND user_id = :user_id
        ORDER BY embedding <-> '{emb_str}'::vector
        LIMIT :top_k
    """)
    result = await session.execute(search_sql, {"top_k": req.top_k, "user_id": current_user.id})
    rows = result.fetchall()

    if not rows:
        return AskResponse(
            answer="知识库中暂无相关内容，请先添加一些文章或笔记。",
            citations=[]
        )

    # Step 3: Extract relevant chunks from each article
    citations = []
    context_parts = []
    
    for row in rows:
        article_id, title, clean_content, raw_content, distance = row
        content = clean_content or raw_content or ""

        # Split into paragraphs
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        if not paragraphs:
            paragraphs = [p.strip() for p in content.split("\n") if p.strip()]

        # Take first 2000 chars as context chunk
        chunk = "\n".join(paragraphs[:min(len(paragraphs), 10)])
        if len(chunk) > 2000:
            chunk = chunk[:2000] + "..."

        relevance = max(0.0, 1.0 - float(distance) / 10.0)
        
        citations.append(Citation(
            article_id=str(article_id),
            title=title or "Untitled",
            chunk=chunk,
            relevance_score=round(relevance, 4)
        ))
        
        context_parts.append(f"--- 文章: {title} ---\n{chunk}")

    # Step 4: Build prompt
    context_text = "\n\n".join(context_parts)
    
    prompt = f"""你是一个知识库AI助手。请根据以下知识库内容回答用户问题。

要求：
1. 回答必须基于提供的知识库内容，不要编造
2. 回答末尾注明引用来源，格式为 [1]、[2] 等
3. 如果知识库内容不足以回答问题，请明确说明
4. 回答简洁、准确

--- 知识库内容 ---
{context_text}
--- 结束 ---

用户问题: {req.question}

请回答（并在末尾列出引用来源）："""

    # Step 5: Call LLM
    answer = await _call_llm(prompt, "你是一个专业的知识库AI助手，基于知识库内容准确回答问题。")

    return AskResponse(answer=answer, citations=citations)
