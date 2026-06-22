"""Periodic review generation + push.

Cron-like loop (run from app lifespan) scans review_schedules for due rows,
generates an LLM-written review of the user's recent articles, and pushes it
through the user's bound WeChat bot.

See for the bot push protocol.
"""
import asyncio
import base64
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Article, ReviewSchedule, User, WechatAccount

logger = logging.getLogger("trove.review")

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCAN_INTERVAL_S = 60  # cron tick rate

ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = "132099"
BOT_AGENT_PUSH = "TroveReview/0.1"

# Per-message char cap to avoid WeChat truncation
WECHAT_MSG_MAX = 1800


# ── Wire helpers (parallel to wechat_bot.py) ───────────────────────────
def _random_uin() -> str:
    n = secrets.randbelow(2**32)
    return base64.b64encode(str(n).encode()).decode()


def _ilink_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }


def _base_info() -> dict:
    return {"channel_version": "2.4.3", "bot_agent": BOT_AGENT_PUSH}


def _client_id() -> str:
    return f"trove-review:{int(time.time() * 1000)}-{secrets.token_hex(4)}"


async def send_wechat(client: httpx.AsyncClient, acct: WechatAccount, text: str) -> bool:
    """Send a single text message to the user via their bound bot.

    The recipient is `acct.wechat_user_id` (the user who scanned to bind).
    Returns True on HTTP 200, False otherwise.
    """
    if not acct.wechat_user_id:
        logger.warning(f"acct {acct.id} has no wechat_user_id; cannot push")
        return False
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": acct.wechat_user_id,
            "client_id": _client_id(),
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        },
        "base_info": _base_info(),
    }
    try:
        r = await client.post(
            f"{acct.base_url}/ilink/bot/sendmessage",
            headers=_ilink_headers(acct.token),
            json=body,
            timeout=20,
        )
        if r.status_code != 200:
            logger.warning(f"sendmessage {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"sendmessage error: {e}")
        return False


# ── Review content generation ──────────────────────────────────────────
async def _list_articles_since(db: AsyncSession, user_id: UUID, since: datetime) -> List[Article]:
    """User's articles created since `since` (timezone-aware)."""
    r = await db.execute(
        select(Article)
        .where(Article.user_id == user_id, Article.created_at >= since)
        .order_by(Article.created_at.desc())
        .limit(50)
    )
    return list(r.scalars().all())


async def generate_review_text(
    db: AsyncSession, user_id: UUID, since: datetime, freq_days: int
) -> Tuple[Optional[str], List[dict]]:
    """Build the review text + citation map.

    Returns (text_with_markers, cite_map) where:
    - text_with_markers may contain [[N]] tokens that frontends can replace
      with clickable links to article id at index N
    - cite_map is a list of {"idx": N, "id": "<uuid>", "title": "..."}

    Returns (None, []) when there's nothing to review.
    """
    articles = await _list_articles_since(db, user_id, since)
    if not articles:
        return None, []

    # Build numbered article list for the LLM. The same N becomes the [[N]] citation.
    lines = []
    cite_map: List[dict] = []
    for i, a in enumerate(articles[:30], 1):
        platform = a.source_platform or "web"
        title = (a.title or "Untitled").strip()
        summary_head = (a.summary or "").strip().replace("\n", " ")[:80]
        lines.append(f"{i}. [{platform}] {title[:60]}" + (f" — {summary_head}" if summary_head else ""))
        cite_map.append({"idx": i, "id": str(a.id), "title": title[:60]})
    articles_block = "\n".join(lines)

    from app.services.ai_service import llm_service
    system_prompt = (
        "你是用户的个人知识助理。基于用户最近收藏的文章，生成一份精炼的知识回顾，"
        "用于在微信里推送给用户回看。输出纯文本（不要 markdown 符号），"
        "总长度控制在 350 字以内。"
    )
    freq_label = "这一天" if freq_days <= 1 else (f"过去 {freq_days} 天")
    user_prompt = f"""请基于以下 {len(articles)} 篇用户最近收藏的文章，生成{freq_label}的知识回顾：

{articles_block}

回顾要求：
1. 开头一句话总览（"{freq_label}你收藏了 {len(articles)} 篇，主要关注 X / Y / Z"，主题从内容里归纳）
2. 按主题聚类列出 2-3 个重点，每个 1-2 句概括（用「」括出主题名）
3. 推荐 1 篇最值得重读的——**必须用 [[N]] 标记文章编号**（N 是上方编号），例如 [[3]]，方便前端转链接
4. 结尾一句简短鼓励

直接返回正文，不要任何前缀/标题/markdown。"""
    try:
        text = await llm_service._chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
        )
        return text.strip(), cite_map
    except Exception as e:
        logger.exception(f"review LLM failed for user {user_id}: {e}")
        return None, cite_map


_CITE_TOKEN_RE = re.compile(r"\[\[(\d+)\]\]")


def render_for_wechat(text: str, cite_map: List[dict]) -> str:
    """Replace [[N]] tokens with 《标题》 since WeChat can't render links."""
    idx_to_title = {c["idx"]: c["title"] for c in cite_map}

    def repl(m):
        idx = int(m.group(1))
        title = idx_to_title.get(idx)
        return f"《{title}》" if title else m.group(0)

    return _CITE_TOKEN_RE.sub(repl, text)


def _split_for_wechat(text: str, cap: int = WECHAT_MSG_MAX) -> List[str]:
    """Split a long string into chunks fitting WeChat single-message limit."""
    if len(text) <= cap:
        return [text]
    parts: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= cap:
            parts.append(remaining)
            break
        # Try to break on a paragraph or sentence boundary near the cap.
        cut = remaining.rfind("\n", 0, cap)
        if cut < cap // 2:
            cut = remaining.rfind("。", 0, cap)
        if cut < cap // 2:
            cut = cap
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return parts


# ── Schedule lifecycle helpers ─────────────────────────────────────────
def compute_next_send_at(freq_days: int, time_of_day: str, ref: Optional[datetime] = None) -> datetime:
    """Compute the next send time in UTC.

    Uses Asia/Shanghai for the time_of_day field. If `ref` is given, start from
    ref+freq_days; otherwise from "today" (or tomorrow if today's time has already passed).
    """
    hh, mm = (int(x) for x in time_of_day.split(":"))
    now_sh = datetime.now(SHANGHAI)
    if ref is None:
        candidate = now_sh.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now_sh:
            candidate += timedelta(days=1)
    else:
        ref_sh = ref.astimezone(SHANGHAI)
        candidate = (ref_sh + timedelta(days=freq_days)).replace(
            hour=hh, minute=mm, second=0, microsecond=0
        )
        if candidate <= now_sh:
            # ref+freq_days landed in the past (clock skew or paused for a while);
            # bump forward by full freq cycles until in future.
            while candidate <= now_sh:
                candidate += timedelta(days=max(freq_days, 1))
    return candidate.astimezone(timezone.utc)


# ── Cron loop ──────────────────────────────────────────────────────────
async def _run_one_schedule(client: httpx.AsyncClient, sched: ReviewSchedule) -> None:
    """Generate + push for one due schedule. Updates last_sent_at and next_send_at."""
    # `next_send_at` was already bumped by the cron's claim transaction;
    # here we only need to update `last_sent_at` after a successful push.
    async with async_session() as db:
        since = sched.last_sent_at or (datetime.now(timezone.utc) - timedelta(days=sched.frequency_days))
        raw_text, cite_map = await generate_review_text(
            db, sched.user_id, since, sched.frequency_days
        )
        text = render_for_wechat(raw_text, cite_map) if raw_text else None
        now_utc = datetime.now(timezone.utc)

        if not text:
            logger.info(f"user {sched.user_id} has no new articles; skipping push (next_send_at already set)")
            await db.execute(
                update(ReviewSchedule)
                .where(ReviewSchedule.id == sched.id)
                .values(last_sent_at=now_utc)
            )
            await db.commit()
            return

        r = await db.execute(
            select(WechatAccount).where(
                WechatAccount.user_id == sched.user_id, WechatAccount.is_active.is_(True)
            )
        )
        acct = r.scalar_one_or_none()
        if not acct:
            logger.warning(f"user {sched.user_id} has no active wechat; no push but next_send_at already set")
            return

        # 追加一条图谱洞察(Graph Insights):意外连接 / 知识缺口 / 枢纽。
        insight_line = None
        try:
            from app.services.graph_insights import compute_insights, pick_digest_line
            insight_line = pick_digest_line(await compute_insights(db, sched.user_id))
        except Exception as e:
            logger.warning(f"insight digest failed for {sched.user_id}: {e}")

        header = f"📚 Trove AI 知识回顾\n\n"
        body = text + (f"\n\n{insight_line}" if insight_line else "")
        chunks = _split_for_wechat(header + body)
        ok = True
        for chunk in chunks:
            if not await send_wechat(client, acct, chunk):
                ok = False
                break

        if ok:
            await db.execute(
                update(ReviewSchedule)
                .where(ReviewSchedule.id == sched.id)
                .values(last_sent_at=now_utc)
            )
            await db.commit()
        logger.info(f"review pushed user={sched.user_id} ok={ok} chunks={len(chunks)}")


async def review_cron_loop():
    """Run forever, scanning for due review schedules every SCAN_INTERVAL_S seconds.

    Multi-worker safety: prod runs `uvicorn --workers 2`, so this loop runs in
    BOTH worker processes. We claim due rows atomically via `SELECT … FOR
    UPDATE SKIP LOCKED` and immediately bump `next_send_at` to the next cycle
    inside the same transaction. The losing worker sees zero rows and skips.
    """
    logger.info("Review cron loop started")
    await asyncio.sleep(10)  # let backend finish init
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            try:
                claimed: List[ReviewSchedule] = []
                async with async_session() as db:
                    now_utc = datetime.now(timezone.utc)
                    stmt = (
                        select(ReviewSchedule)
                        .where(
                            ReviewSchedule.enabled.is_(True),
                            ReviewSchedule.next_send_at <= now_utc,
                        )
                        .with_for_update(skip_locked=True)
                    )
                    r = await db.execute(stmt)
                    rows = list(r.scalars().all())
                    # Claim each by pre-bumping next_send_at INSIDE the same lock.
                    # A racing worker on the other process either skipped these
                    # rows (SKIP LOCKED) or will see next_send_at > now after we commit.
                    for sched in rows:
                        sched.next_send_at = compute_next_send_at(
                            sched.frequency_days, sched.time_of_day, ref=now_utc
                        )
                        claimed.append(sched)
                    await db.commit()
                if claimed:
                    logger.info(f"review cron: claimed {len(claimed)} due schedule(s)")
                for sched in claimed:
                    try:
                        # next_send_at already bumped inside the lock above; only
                        # need last_sent_at to update after push succeeds.
                        await _run_one_schedule(client, sched)
                    except Exception:
                        logger.exception(f"review run failed for {sched.id}")
            except Exception:
                logger.exception("review cron iteration failed")
            await asyncio.sleep(SCAN_INTERVAL_S)
