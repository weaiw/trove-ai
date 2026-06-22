"""知识库定位(kb_purpose)注入 —— 借鉴 llm_wiki 的 purpose.md。

用户在设置里自述"这个知识库是干嘛的",我们把它作为一段定位说明,注入到
RAG 问答 / 深度研究的 system prompt,让 AI 的理解、组织、回答都贴合该用途的
视角、优先级与术语。空则不注入,行为与原来完全一致。
"""
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


def purpose_clause(purpose: Optional[str]) -> str:
    """把用户的知识库定位渲染成一段可拼进 system prompt 的说明。空返回空串。"""
    p = (purpose or "").strip()
    if not p:
        return ""
    # 截断,避免用户写超长 purpose 把 prompt 撑爆
    if len(p) > 600:
        p = p[:600] + "…"
    return (
        f"\n\n【知识库定位】用户这个知识库的用途是：{p}\n"
        "请在理解、组织与回答时贴合该定位的视角、优先级与术语；"
        "但不要因此编造定位之外不存在的内容。"
    )


async def get_kb_purpose(db: AsyncSession, user_id: UUID) -> str:
    """按 user_id 取知识库定位文本,取不到返回空串。"""
    from app.models.user import User
    u = await db.get(User, user_id)
    return (u.kb_purpose or "").strip() if u and u.kb_purpose else ""
