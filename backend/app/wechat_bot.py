"""Multi-account WeChat bot runner.

Reads bound WeChat accounts from the wechat_accounts table, spawns one async
long-polling loop per account, routes each inbound message to its owning
Trove AI user via the X-Act-As-User header (requires the bot's service token
to be mapped to a superadmin user).

Run inside the same Docker image as the backend (which gives us DB access and
parser_service):

    TROVE_BASE=http://backend:8000 TROVE_TOKEN=<superadmin-token> \
    python -m app.wechat_bot

See memory: trove_wechat_bot, reference_openclaw_weixin.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import html
import json
import logging
import mimetypes
import os
import re
import secrets
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, Optional
from urllib.parse import quote_plus
from uuid import UUID

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import WechatAccount
from app.services.transcription_service import transcription_service


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("trove.wechat-bot")


# ── ilinkai wire constants ─────────────────────────────────────────────
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = "132099"
BOT_AGENT = "TroveBot/0.2-multi"
LONGPOLL_TIMEOUT_S = 35
WECHAT_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
MAX_WECHAT_MEDIA_BYTES = 80 * 1024 * 1024

UPLOADABLE_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".txt", ".html", ".htm", ".epub", ".csv", ".md",
}

URL_RE = re.compile(
    r"https?://[^\s一-鿿\"'<>{}|\\^`，。、；：！？（）【】《》]+",
    re.IGNORECASE,
)

# Heuristic: queries containing any of these keywords are routed to deep research
# automatically (no need for /r prefix). Conservative — only obvious "synthesis"
# verbs. Other queries default to the fast single-shot RAG path.
COMPLEX_KEYWORDS = (
    "梳理", "综述", "对比", "比较", "演化", "演变", "整理一下", "归纳",
    "哪些", "全面", "系统讲", "系统总结", "汇总", "不同观点",
    "演进", "发展脉络", "区别和联系",
)

LIGHT_COMMANDS = {
    "/最近": "列出我最近收藏的 10 篇文章，并简单概括这些内容主要集中在哪些主题。",
    "最近": "列出我最近收藏的 10 篇文章，并简单概括这些内容主要集中在哪些主题。",
    "/回顾": "根据我最近收藏和知识库内容，生成一份简短知识回顾，给出本周值得复习的主题。",
    "回顾": "根据我最近收藏和知识库内容，生成一份简短知识回顾，给出本周值得复习的主题。",
    "/整理": "帮我整理最近收藏，先给出归类和标签建议；如果需要改动知识库，请先列计划并等待我确认。",
    "整理": "帮我整理最近收藏，先给出归类和标签建议；如果需要改动知识库，请先列计划并等待我确认。",
}

WRITE_TOOL_LABELS = {
    "tag_articles": "打标签",
    "move_to_folder": "归类到文件夹",
    "save_url_to_folder": "链接入库并归类",
    "link_articles": "建立知识关系",
    "synthesize_concept": "合成概念页",
    "configure_review": "配置复习简报",
}


def _confirm_summary(name: str, args: dict) -> str:
    args = args or {}
    tool_label = WRITE_TOOL_LABELS.get(name, name or "写操作")
    details: list[str] = []
    if isinstance(args.get("article_ids"), list):
        details.append(f"影响文章：{len(args['article_ids'])} 篇")
    if args.get("folder_name"):
        details.append(f"目标文件夹：{args['folder_name']}")
    if args.get("url"):
        details.append(f"链接：{args['url']}")
    if args.get("tag"):
        details.append(f"标签：{args['tag']}")
    if args.get("topic"):
        details.append(f"主题：{args['topic']}")
    if args.get("relation_type"):
        details.append(f"关系：{args['relation_type']}")
    if args.get("frequency_days"):
        details.append(f"频率：每 {args['frequency_days']} 天")
    if args.get("time_of_day"):
        details.append(f"时间：{args['time_of_day']}")
    lines = [f"待确认：{tool_label}"]
    lines.extend(details)
    return "\n".join(lines)


def _is_complex_query(text: str) -> bool:
    """Cheap rule-based classifier — no LLM call.

    Returns True if the query is "obviously" a synthesis/comparison/list task.
    Errs on the side of False (fast path) when ambiguous; users can force the
    research path with /r prefix.
    """
    if not text or len(text) < 12:
        return False
    return any(kw in text for kw in COMPLEX_KEYWORDS)


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
    return {"channel_version": "2.4.3", "bot_agent": BOT_AGENT}


def _client_id() -> str:
    return f"trove-bot:{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def _extract_url(text: str) -> Optional[str]:
    m = URL_RE.search(html.unescape(text or ""))
    return m.group(0).rstrip(".,;:!?)]") if m else None


def _has_url_folder_intent(text: str) -> bool:
    """Compound URL commands belong to the Agent; plain URLs keep the fast path."""
    return bool(_extract_url(text)) and any(
        keyword in (text or "")
        for keyword in ("文件夹", "入库到", "保存到", "存到", "放到", "归入", "归到", "归档到")
    )


def _safe_filename(name: str, fallback: str = "wechat-file") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name or "").strip()
    return cleaned[:120] or fallback


def _guess_content_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _decode_media_aes_key(aes_key: str) -> Optional[bytes]:
    """Decode iLink CDN AES key.

    Observed formats:
    - base64(raw 16 bytes)
    - base64(32-byte ASCII hex string)
    - occasionally plain 32-char hex string from image_item.aeskey
    """
    if not aes_key:
        return None
    raw = aes_key.strip()
    try:
        if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
            return bytes.fromhex(raw)
        decoded = base64.b64decode(raw)
        if len(decoded) == 16:
            return decoded
        if len(decoded) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", decoded):
            return bytes.fromhex(decoded.decode("ascii"))
    except (binascii.Error, ValueError):
        return None
    return None


def _aes_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad = padded[-1]
    if 1 <= pad <= 16 and padded.endswith(bytes([pad]) * pad):
        return padded[:-pad]
    return padded


def _media_from_item(item: dict, item_key: str) -> Optional[dict]:
    payload = item.get(item_key) or {}
    media = payload.get("media") or {}
    if media.get("encrypt_query_param"):
        return media
    return None


def _sniff_image_ext(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return ".gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _voice_suffix(encode_type: Optional[int]) -> str:
    return {
        1: ".pcm",
        2: ".adpcm",
        4: ".spx",
        5: ".amr",
        6: ".silk",
        7: ".mp3",
        8: ".spx",
    }.get(encode_type or 0, ".mp3")


# ── Trove AI backend calls (per-user via X-Act-As-User) ────────────────
class TroveClient:
    def __init__(self, base_url: str, service_token: str):
        self.base_url = base_url.rstrip("/")
        self.token = service_token
        # One shared httpx client; longer than long-poll for upload and Agent paths.
        self._client = httpx.AsyncClient(timeout=150.0)

    async def close(self):
        await self._client.aclose()

    def _h(self, target_user_id: UUID) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Act-As-User": str(target_user_id),
            "Content-Type": "application/json",
        }

    def _auth_h(self, target_user_id: UUID) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Act-As-User": str(target_user_id),
        }

    async def add_article(self, target_user_id: UUID, url: str) -> tuple[bool, str]:
        try:
            r = await self._client.post(
                f"{self.base_url}/api/articles",
                headers=self._h(target_user_id),
                json={"url": url},
            )
        except Exception as e:
            return False, f"❌ 网络错误：{type(e).__name__}"

        if r.status_code == 201:
            data = r.json()
            title = (data.get("title") or "Untitled")[:50]
            return True, f"✅ 已添加：{title}"
        if r.status_code == 409:
            return True, "ℹ️ 这条已经在库里了"
        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = r.text[:100]
        return False, f"❌ 添加失败 ({r.status_code})：{detail}"

    async def upload_file(
        self,
        target_user_id: UUID,
        filename: str,
        content: bytes,
        content_type: Optional[str] = None,
    ) -> tuple[bool, str]:
        content_type = content_type or _guess_content_type(filename)
        try:
            r = await self._client.post(
                f"{self.base_url}/api/articles/upload",
                headers=self._auth_h(target_user_id),
                files={"file": (filename, content, content_type)},
                timeout=240,
            )
        except Exception as e:
            return False, f"❌ 上传失败：{type(e).__name__}"

        if r.status_code == 201:
            data = r.json()
            title = (data.get("title") or filename)[:50]
            return True, f"✅ 文件已入库：{title}\n\n我会继续自动生成摘要、标签和关键点。"

        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = r.text[:160]
        if r.status_code == 400 and "Unsupported file format" in detail:
            return False, "❌ 这个文件格式暂不支持入库。支持 PDF、Word、PPT、Excel、图片、TXT、Markdown、HTML、EPUB、CSV。"
        if r.status_code == 413:
            return False, f"❌ 文件太大：{detail or '超过当前上传限制'}"
        return False, f"❌ 文件入库失败 ({r.status_code})：{detail[:120]}"

    async def research_stream(
        self, target_user_id: UUID, query: str, mode: str = "sequential"
    ) -> AsyncIterator[dict]:
        """Open an SSE stream against research endpoints, yielding decoded events.

        mode='sequential' → /ask (fixed 4-stage)
        mode='tool'       → /agent (ReAct loop with library tools)
        """
        endpoint = "/api/research/agent" if mode == "tool" else "/api/research/ask"
        async with self._client.stream(
            "POST",
            f"{self.base_url}{endpoint}",
            headers=self._h(target_user_id),
            json={"query": query},
            timeout=300,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield {"stage": "error", "message": f"研究助理启动失败 ({resp.status_code}): {body[:200]!r}"}
                return
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                try:
                    yield json.loads(line[6:])
                except Exception as e:
                    logger.warning(f"bad SSE line: {e}: {line[:120]}")

    async def create_spark(self, target_user_id: UUID, sentence: str) -> dict:
        """Call /api/articles/spark — generates a full article from a one-liner topic.
        Returns the article dict (id, title, content...) or {error: str}."""
        try:
            r = await self._client.post(
                f"{self.base_url}/api/articles/spark",
                headers=self._h(target_user_id),
                json={"sentence": sentence, "enable_search": False},
                timeout=240,
            )
        except Exception as e:
            return {"error": f"网络错误：{type(e).__name__}"}
        if r.status_code != 201:
            try:
                detail = r.json().get("detail", "")
            except Exception:
                detail = r.text[:200]
            return {"error": f"生成失败 ({r.status_code})：{detail}"}
        return r.json()

    async def ask(self, target_user_id: UUID, question: str) -> str:
        try:
            r = await self._client.post(
                f"{self.base_url}/api/assistant/ask",
                headers=self._h(target_user_id),
                json={"question": question, "top_k": 5},
            )
        except Exception as e:
            return f"❌ 网络错误：{type(e).__name__}"
        if r.status_code != 200:
            try:
                detail = r.json().get("detail", "")
            except Exception:
                detail = r.text[:100]
            return f"❌ 检索失败 ({r.status_code})：{detail}"
        data = r.json()
        answer = (data.get("answer") or "").strip() or "（空回答）"
        cites = data.get("citations") or []
        if cites:
            titles = "、".join(c.get("title", "")[:20] for c in cites[:3])
            return f"{answer}\n\n📚 参考：{titles}"
        return answer


# ── Per-account long-poll loop ─────────────────────────────────────────
class AccountWorker:
    def __init__(self, account_id: UUID, lm: TroveClient):
        self.account_id = account_id
        self.lm = lm
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._agent_state: dict[str, dict] = {}

    def start(self):
        self._task = asyncio.create_task(self._run(), name=f"wechat-{self.account_id}")

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _load(self) -> Optional[WechatAccount]:
        async with async_session() as db:
            r = await db.execute(
                select(WechatAccount).where(WechatAccount.id == self.account_id)
            )
            return r.scalar_one_or_none()

    async def _save_cursor(self, cursor: str):
        async with async_session() as db:
            await db.execute(
                update(WechatAccount)
                .where(WechatAccount.id == self.account_id)
                .values(sync_cursor=cursor)
            )
            await db.commit()

    async def _mark_seen(self):
        async with async_session() as db:
            await db.execute(
                update(WechatAccount)
                .where(WechatAccount.id == self.account_id)
                .values(last_seen_at=datetime.now(timezone.utc))
            )
            await db.commit()

    async def _send_text(self, client: httpx.AsyncClient, base_url: str, token: str,
                         to_user_id: str, context_token: Optional[str], text: str) -> None:
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": _client_id(),
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
                **({"context_token": context_token} if context_token else {}),
            },
            "base_info": _base_info(),
        }
        try:
            r = await client.post(
                f"{base_url}/ilink/bot/sendmessage",
                headers=_ilink_headers(token),
                json=body,
                timeout=15,
            )
            if r.status_code != 200:
                logger.warning(f"[{self.account_id}] sendmessage {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.warning(f"[{self.account_id}] sendmessage failed: {e}")

    async def _download_media(self, client: httpx.AsyncClient, media: dict) -> tuple[Optional[bytes], str]:
        query_param = media.get("encrypt_query_param") or ""
        if not query_param:
            return None, "缺少媒体下载参数"
        url = f"{WECHAT_CDN_BASE}/download?encrypted_query_param={quote_plus(query_param)}"
        try:
            async with client.stream("GET", url, timeout=90) as resp:
                if resp.status_code != 200:
                    return None, f"媒体下载失败 HTTP {resp.status_code}"
                size = 0
                chunks: list[bytes] = []
                async for chunk in resp.aiter_bytes(64 * 1024):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_WECHAT_MEDIA_BYTES:
                        return None, "媒体超过 80MB，暂不支持"
        except Exception as e:
            return None, f"媒体下载失败：{type(e).__name__}"

        ciphertext = b"".join(chunks)
        key = _decode_media_aes_key(media.get("aes_key") or "")
        if not key:
            logger.warning(f"[{self.account_id}] media without valid aes_key; trying raw bytes")
            return ciphertext, ""
        try:
            return _aes_ecb_decrypt(ciphertext, key), ""
        except Exception as e:
            logger.warning(f"[{self.account_id}] media decrypt failed: {e}")
            return None, "媒体解密失败"

    async def _handle_file_item(
        self, client: httpx.AsyncClient, acct: WechatAccount,
        sender: str, ctx: str, item: dict,
    ) -> bool:
        payload = item.get("file_item") or {}
        media = _media_from_item(item, "file_item")
        filename = _safe_filename(payload.get("file_name") or "wechat-file")
        ext = os.path.splitext(filename.lower())[1]
        if ext and ext not in UPLOADABLE_EXTENSIONS:
            await self._send_text(
                client, acct.base_url, acct.token, sender, ctx,
                "❌ 这个文件格式暂不支持入库。支持 PDF、Word、PPT、Excel、图片、TXT、Markdown、HTML、EPUB、CSV。",
            )
            return True
        if not media:
            await self._send_text(client, acct.base_url, acct.token, sender, ctx, "❌ 没拿到这个文件的下载信息。")
            return True

        await self._send_text(client, acct.base_url, acct.token, sender, ctx, f"📎 收到文件，正在入库：{filename}")
        content, err = await self._download_media(client, media)
        if err or not content:
            await self._send_text(client, acct.base_url, acct.token, sender, ctx, f"❌ {err or '文件为空'}")
            return True

        ok, reply = await self.lm.upload_file(acct.user_id, filename, content, _guess_content_type(filename))
        logger.info(f"[{acct.account_id}] upload_file ok={ok} name={filename!r} bytes={len(content)}")
        await self._send_text(client, acct.base_url, acct.token, sender, ctx, reply)
        return True

    async def _handle_image_item(
        self, client: httpx.AsyncClient, acct: WechatAccount,
        sender: str, ctx: str, item: dict,
    ) -> bool:
        media = _media_from_item(item, "image_item")
        if not media:
            return False
        # Some iLink image payloads put a plain hex key at image_item.aeskey.
        image_payload = item.get("image_item") or {}
        if image_payload.get("aeskey") and not media.get("aes_key"):
            media = {**media, "aes_key": image_payload.get("aeskey")}

        await self._send_text(client, acct.base_url, acct.token, sender, ctx, "🖼️ 收到图片，正在入库…")
        content, err = await self._download_media(client, media)
        if err or not content:
            await self._send_text(client, acct.base_url, acct.token, sender, ctx, f"❌ {err or '图片为空'}")
            return True

        ext = _sniff_image_ext(content)
        filename = f"wechat-image-{int(time.time())}{ext}"
        ok, reply = await self.lm.upload_file(acct.user_id, filename, content, _guess_content_type(filename))
        logger.info(f"[{acct.account_id}] upload_image ok={ok} bytes={len(content)}")
        await self._send_text(client, acct.base_url, acct.token, sender, ctx, reply)
        return True

    async def _handle_voice_item(
        self, client: httpx.AsyncClient, acct: WechatAccount,
        sender: str, ctx: str, item: dict,
    ) -> bool:
        payload = item.get("voice_item") or {}
        transcript = (payload.get("text") or "").strip()
        if transcript:
            await self._send_text(client, acct.base_url, acct.token, sender, ctx, f"🎙️ 我听到的是：{transcript}")
            await self._handle_agent(client, acct, sender, ctx, transcript)
            return True

        media = _media_from_item(item, "voice_item")
        if not media:
            await self._send_text(client, acct.base_url, acct.token, sender, ctx, "❌ 这条语音没有可识别文本，也没有下载信息。")
            return True
        await self._send_text(client, acct.base_url, acct.token, sender, ctx, "🎙️ 收到语音，正在识别…")
        content, err = await self._download_media(client, media)
        if err or not content:
            await self._send_text(client, acct.base_url, acct.token, sender, ctx, f"❌ {err or '语音为空'}")
            return True
        if not transcription_service.available:
            await self._send_text(client, acct.base_url, acct.token, sender, ctx, "❌ 语音识别服务还没配置，暂时无法识别这条语音。")
            return True

        suffix = _voice_suffix(payload.get("encode_type"))
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            transcript = (await transcription_service._transcribe_local(tmp_path) or "").strip()
        finally:
            tmp_path.unlink(missing_ok=True)
        if not transcript:
            await self._send_text(client, acct.base_url, acct.token, sender, ctx, "❌ 这条语音没有识别出文字。")
            return True
        await self._send_text(client, acct.base_url, acct.token, sender, ctx, f"🎙️ 我听到的是：{transcript}")
        await self._handle_agent(client, acct, sender, ctx, transcript)
        return True

    async def _handle(self, client: httpx.AsyncClient, acct: WechatAccount, msg: dict):
        sender = msg.get("from_user_id") or ""
        ctx = msg.get("context_token") or ""
        for it in (msg.get("item_list") or []):
            item_type = it.get("type")
            if item_type == 4 and await self._handle_file_item(client, acct, sender, ctx, it):
                return
            if item_type == 2 and await self._handle_image_item(client, acct, sender, ctx, it):
                return
            if item_type == 3 and await self._handle_voice_item(client, acct, sender, ctx, it):
                return

        text = ""
        for it in (msg.get("item_list") or []):
            if it.get("type") == 1:
                text = (it.get("text_item") or {}).get("text", "") or ""
                break

        if not text:
            await self._send_text(client, acct.base_url, acct.token, sender, ctx,
                                  "目前支持文本、链接、文件、图片和语音。这个消息类型我还不会处理。")
            return

        text_stripped = text.strip()

        # /h or /help — show available commands
        if text_stripped in ("/h", "/help", "帮助"):
            await self._send_text(
                client, acct.base_url, acct.token, sender, ctx,
                "📚 Trove AI 用法\n\n"
                "• 直接发链接 → 自动存入你的知识库\n"
                "• 发『把链接入库到 X 文件夹』→ 确认后自动建/复用文件夹并归类\n"
                "• 直接发问题 → 知识管理 Agent 帮你办：\n"
                "  ◦ 库内问答 / 最近收藏回顾 / 库内材料对比\n"
                "  ◦ 找素材：『帮我从库里挑 5 篇做 AI Agent 综述的素材』\n"
                "  ◦ 主题整理：『我最近收藏主要集中在哪些方向』\n"
                "• 直接发文件、图片、语音 → 入库或转成 Agent 输入\n"
                "• 轻命令：/最近 /回顾 /整理\n"
                "• 含「梳理/综述/对比/演化…」等词 → 自动深度研究（20-40s）\n"
                "• /r <问题> → 强制 4 阶段深度研究\n"
                "• /c <主题> → 灵感创作（一句话生成完整文章入库）\n"
                "• /help → 显示本帮助",
            )
            return

        # Lightweight natural commands for common mobile workflows.
        if text_stripped in LIGHT_COMMANDS:
            await self._handle_agent(client, acct, sender, ctx, LIGHT_COMMANDS[text_stripped])
            return

        url = _extract_url(text)
        if url and not _has_url_folder_intent(text_stripped):
            ok, reply = await self.lm.add_article(acct.user_id, url)
            logger.info(f"[{acct.account_id}] add_article ok={ok} → {reply[:80]}")
            await self._send_text(client, acct.base_url, acct.token, sender, ctx, reply)
            return

        # Spark creation: /c <topic> → AI 灵感创作 generates full article
        if text_stripped.startswith("/c ") or text_stripped.startswith("/create "):
            topic = text_stripped.split(" ", 1)[1].strip()
            if not topic:
                await self._send_text(
                    client, acct.base_url, acct.token, sender, ctx,
                    "请在 /c 后面写主题。例：/c AI Agent 在产品经理工作流中的应用",
                )
                return
            await self._handle_spark(client, acct, sender, ctx, topic)
            return

        # Tool-using agent: /a or /agent prefix
        if text_stripped.startswith("/a ") or text_stripped.startswith("/agent "):
            query = text_stripped.split(" ", 1)[1].strip()
            if not query:
                await self._send_text(
                    client, acct.base_url, acct.token, sender, ctx,
                    "请在 /a 后面写具体问题。例：/a 帮我从库里挑 5 篇做 AI Agent 综述的素材",
                )
                return
            await self._handle_agent(client, acct, sender, ctx, query)
            return

        # Sequential research: explicit /r or /research prefix
        explicit_research = False
        if text_stripped.startswith("/r ") or text_stripped.startswith("/research "):
            text_stripped = text_stripped.split(" ", 1)[1].strip()
            explicit_research = True
            if not text_stripped:
                await self._send_text(
                    client, acct.base_url, acct.token, sender, ctx,
                    "请在 /r 后面写具体问题。例：/r 梳理我对 AI Agent 的看法演化",
                )
                return

        # Automatic routing: complex queries (by rule) go sequential research even without /r
        if explicit_research or _is_complex_query(text_stripped):
            await self._handle_research(
                client, acct, sender, ctx, text_stripped, mode="sequential"
            )
            return

        # Default: knowledge-management Agent over the user's own library.
        logger.info(f"[{acct.account_id}] agent q={text[:40]!r}")
        await self._handle_agent(client, acct, sender, ctx, text_stripped)

    async def _handle_spark(
        self, client: httpx.AsyncClient, acct: WechatAccount,
        sender: str, ctx: str, topic: str,
    ):
        """Generate a full article from a topic via /api/articles/spark and push the result."""
        # ack
        await self._send_text(
            client, acct.base_url, acct.token, sender, ctx,
            f"✨ 灵感创作启动：「{topic[:50]}」\n（LLM 写大纲+各章节，约 30-90 秒，完成后会推送链接）",
        )

        result = await self.lm.create_spark(acct.user_id, topic)
        if "error" in result:
            await self._send_text(
                client, acct.base_url, acct.token, sender, ctx,
                f"⚠️ {result['error']}",
            )
            return

        article_id = result.get("id", "")
        title = (result.get("title") or "Untitled").strip()
        # First paragraph of content as preview
        content = (result.get("content") or "").strip()
        preview = ""
        if content:
            # strip leading markdown heading if any
            first_para = next(
                (p.strip() for p in content.split("\n\n") if p.strip() and not p.strip().startswith("#")),
                "",
            )
            preview = first_para[:180] + ("…" if len(first_para) > 180 else "")

        # deep link to /read
        public_base = os.environ.get("TROVE_PUBLIC_BASE", "http://localhost")
        link = f"{public_base}/read/{article_id}" if article_id else ""

        msg = f"✅ 已生成《{title[:50]}》"
        if preview:
            msg += f"\n\n{preview}"
        if link:
            msg += f"\n\n📖 完整阅读：{link}"
        await self._send_text(client, acct.base_url, acct.token, sender, ctx, msg)

    async def _handle_agent(
        self, client: httpx.AsyncClient, acct: WechatAccount,
        sender: str, ctx: str, query: str,
    ):
        """Knowledge-management Agent entry backed by /api/research/agent.

        The open-source agent is read-only today (search/read/list recent). It can
        help users understand and organize their knowledge, while write-side
        confirmation flows are reserved for a future unified /api/agent endpoint.
        """
        await self._send_text(client, acct.base_url, acct.token, sender, ctx, "🤖 收到，正在处理…")
        final_answer, tools = None, []
        try:
            async for ev in self.lm.research_stream(acct.user_id, query, mode="tool"):
                st = ev.get("stage")
                if st == "tool_call":
                    tools.append(ev.get("data", {}).get("name", ""))
                elif st == "final":
                    final_answer = (ev.get("data", {}) or {}).get("answer")
                elif st == "error":
                    final_answer = "⚠️ " + ev.get("message", "出错了")
        except Exception as e:
            logger.exception(f"[{acct.account_id}] agent_chat crashed: {e}")
            await self._send_text(client, acct.base_url, acct.token, sender, ctx, f"⚠️ Agent 出错：{type(e).__name__}")
            return

        trace = f"\n\n🔧 用了：{'、'.join(t for t in tools if t)}" if tools else ""
        reply = (final_answer or "（没有回答）") + trace
        await self._send_text(client, acct.base_url, acct.token, sender, ctx, reply)

    async def _handle_research(
        self, client: httpx.AsyncClient, acct: WechatAccount,
        sender: str, ctx: str, query: str, mode: str = "sequential",
    ):
        """Multi-stage research with progress messages between stages.

        Educational value: user sees the Agent's thinking unfold. In sequential
        mode (4 stages); in tool mode (Agent picks tools each step).
        """
        # Initial ack — wording differs slightly to teach user the distinction
        ack = (
            "🤖 智能体已启动（会自主选工具调用，约 20-40 秒）"
            if mode == "tool"
            else "🔬 研究助理已启动（4 阶段：拆解→检索→综述→自审，约 20-40 秒）"
        )
        await self._send_text(client, acct.base_url, acct.token, sender, ctx, ack)

        STAGE_ICONS = {
            "plan": "🧩", "retrieve": "🔍", "synthesize": "✍️",
            "critique": "🪞", "final": "✅", "error": "⚠️",
            "start": "🚀", "thought": "💭", "tool_call": "🔧", "tool_result": "✓",
        }
        last_stage = ""
        final_data: Optional[dict] = None
        try:
            async for ev in self.lm.research_stream(acct.user_id, query, mode=mode):
                stage = ev.get("stage", "")
                msg = ev.get("message", "")
                if stage == "final":
                    final_data = ev.get("data") or {}
                    continue
                if stage == "error":
                    await self._send_text(
                        client, acct.base_url, acct.token, sender, ctx,
                        f"⚠️ {msg}",
                    )
                    return
                # Only send progress on stage transition or when stage stays same
                # but message is different (e.g. plan: "拆解…" → "拆出 N 个子问题…").
                icon = STAGE_ICONS.get(stage, "•")
                await self._send_text(
                    client, acct.base_url, acct.token, sender, ctx,
                    f"{icon} {msg}",
                )
                last_stage = stage
        except Exception as e:
            logger.exception(f"research stream failed: {e}")
            await self._send_text(
                client, acct.base_url, acct.token, sender, ctx,
                f"⚠️ 研究助理出错：{type(e).__name__}",
            )
            return

        if not final_data or not final_data.get("answer"):
            await self._send_text(
                client, acct.base_url, acct.token, sender, ctx,
                "（没拿到最终结果，请重试）",
            )
            return

        answer = final_data["answer"]
        critique = final_data.get("critique") or ""
        cites = final_data.get("citations") or []
        cite_text = ""
        if cites:
            titles = "、".join((c.get("title", "")[:20]) for c in cites[:5])
            cite_text = f"\n\n📚 参考：{titles}"

        # Send answer (may be 300-500字)
        await self._send_text(
            client, acct.base_url, acct.token, sender, ctx,
            f"✅ 综述：\n\n{answer}{cite_text}",
        )
        if critique:
            await self._send_text(
                client, acct.base_url, acct.token, sender, ctx,
                f"🪞 自我审稿：\n\n{critique}",
            )

    async def _run(self):
        # Per-worker httpx client (long-poll-friendly timeout).
        async with httpx.AsyncClient(timeout=LONGPOLL_TIMEOUT_S + 5) as client:
            backoff = 1.0
            while not self._stop.is_set():
                acct = await self._load()
                if not acct or not acct.is_active:
                    logger.info(f"[{self.account_id}] account gone/inactive — exiting worker")
                    return

                try:
                    r = await client.post(
                        f"{acct.base_url}/ilink/bot/getupdates",
                        headers=_ilink_headers(acct.token),
                        json={"get_updates_buf": acct.sync_cursor or "",
                              "base_info": _base_info()},
                    )
                    r.raise_for_status()
                    resp = r.json()
                    backoff = 1.0
                except httpx.ReadTimeout:
                    continue
                except Exception as e:
                    logger.warning(f"[{acct.account_id}] poll err: {type(e).__name__}: {e}; backoff {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                errcode = resp.get("errcode") or resp.get("ret")
                if errcode and errcode != 0:
                    errmsg = resp.get("errmsg") or ""
                    logger.warning(f"[{acct.account_id}] server errcode={errcode} msg={errmsg}")
                    # If token revoked/invalid, give up this worker — supervisor will not respawn
                    # until user re-binds.
                    if errcode in (40001, 42001, 88001):  # heuristic auth-related errors
                        logger.error(f"[{acct.account_id}] auth invalid, marking inactive")
                        async with async_session() as db:
                            await db.execute(
                                update(WechatAccount)
                                .where(WechatAccount.id == acct.id)
                                .values(is_active=False,
                                        unbound_at=datetime.now(timezone.utc))
                            )
                            await db.commit()
                        return
                    await asyncio.sleep(5)
                    continue

                new_cursor = resp.get("get_updates_buf") or acct.sync_cursor or ""
                if new_cursor != (acct.sync_cursor or ""):
                    await self._save_cursor(new_cursor)
                await self._mark_seen()

                for m in (resp.get("msgs") or []):
                    if m.get("message_type") != 1:  # USER only
                        continue
                    try:
                        await self._handle(client, acct, m)
                    except Exception as e:
                        import traceback
                        logger.error(f"[{acct.account_id}] handle err: {e}\n{traceback.format_exc()}")


# ── Supervisor: spawns / culls workers from DB ─────────────────────────
class BotSupervisor:
    REFRESH_INTERVAL_S = 30

    def __init__(self, lm: TroveClient):
        self.lm = lm
        self.workers: Dict[UUID, AccountWorker] = {}
        self._stop = asyncio.Event()

    async def _list_active_ids(self) -> set[UUID]:
        async with async_session() as db:
            r = await db.execute(
                select(WechatAccount.id).where(WechatAccount.is_active.is_(True))
            )
            return {row[0] for row in r.all()}

    async def stop(self):
        self._stop.set()
        for w in list(self.workers.values()):
            await w.stop()
        await self.lm.close()

    async def run(self):
        logger.info("Bot supervisor started")
        while not self._stop.is_set():
            try:
                active = await self._list_active_ids()
                # Spawn new
                for aid in active - self.workers.keys():
                    logger.info(f"Spawning worker for account {aid}")
                    w = AccountWorker(aid, self.lm)
                    w.start()
                    self.workers[aid] = w
                # Cull removed
                for aid in list(self.workers.keys() - active):
                    logger.info(f"Stopping worker for account {aid}")
                    await self.workers[aid].stop()
                    del self.workers[aid]
            except Exception as e:
                logger.exception(f"Supervisor refresh err: {e}")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.REFRESH_INTERVAL_S)
            except asyncio.TimeoutError:
                pass


async def _async_main():
    base = os.environ.get("TROVE_BASE", "http://localhost:8000")
    token = os.environ.get("TROVE_TOKEN", "")
    if not token:
        logger.error("Missing TROVE_TOKEN env (superadmin service token)")
        sys.exit(2)

    sup = BotSupervisor(TroveClient(base, token))
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(sup.stop()))
    await sup.run()


def main():
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
