"""Feishu self-built app webhook, user binding, and channel message adapter."""
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config_manager import get_effective_config
from app.database import async_session, get_db
from app.dependencies import create_access_token, get_current_user
from app.models import ChannelBindCode, ChannelBinding, ChannelEvent, User
from app.services.channel_backend_client import ChannelBackendClient
from app.services.lark_service import LarkClient
from app.services.transcription_service import transcription_service

logger = logging.getLogger("trove.lark")
router = APIRouter(prefix="/api/lark", tags=["lark"])
URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+")


def _has_url_folder_intent(text: str) -> bool:
    return bool(URL_RE.search(text or "")) and any(
        keyword in (text or "")
        for keyword in ("文件夹", "入库到", "保存到", "存到", "放到", "归入", "归到", "归档到")
    )


def _hash_code(code: str) -> str:
    return hashlib.sha256(f"lark:{code}".encode()).hexdigest()


def _is_enabled(config: dict) -> bool:
    return str(config.get("enabled", "false")).lower() == "true"


@router.post("/bind-code", status_code=201)
async def create_bind_code(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    config = get_effective_config("lark")
    if not _is_enabled(config):
        raise HTTPException(status_code=409, detail="管理员尚未启用飞书 Bot")
    await db.execute(
        update(ChannelBindCode)
        .where(ChannelBindCode.channel == "lark", ChannelBindCode.user_id == current_user.id, ChannelBindCode.used_at.is_(None))
        .values(used_at=datetime.now(timezone.utc))
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    db.add(ChannelBindCode(channel="lark", user_id=current_user.id, code_hash=_hash_code(code), expires_at=expires_at))
    await db.commit()
    return {"code": code, "expires_at": expires_at, "command": f"/bind {code}"}


@router.get("/binding")
async def get_binding(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChannelBinding).where(
            ChannelBinding.channel == "lark", ChannelBinding.user_id == current_user.id, ChannelBinding.is_active.is_(True)
        )
    )
    binding = result.scalar_one_or_none()
    if not binding:
        return {"bound": False}
    return {
        "bound": True,
        "display_name": binding.display_name,
        "last_seen_at": binding.last_seen_at,
        "created_at": binding.created_at,
    }


@router.delete("/binding")
async def delete_binding(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        update(ChannelBinding)
        .where(ChannelBinding.channel == "lark", ChannelBinding.user_id == current_user.id, ChannelBinding.is_active.is_(True))
        .values(is_active=False, unbound_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return {"success": True}


@router.post("/events")
async def lark_events(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    config = get_effective_config("lark")
    supplied_token = (body.get("header") or {}).get("token") or body.get("token")
    expected_token = config.get("verification_token") or ""
    if not expected_token or not secrets.compare_digest(str(supplied_token or ""), str(expected_token)):
        raise HTTPException(status_code=403, detail="invalid verification token")
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}
    if not _is_enabled(config):
        return {"code": 0}
    if (body.get("header") or {}).get("event_type") == "im.message.receive_v1":
        background_tasks.add_task(_process_message_event, body)
    return {"code": 0}


async def _bind_external_user(db: AsyncSession, external_user_id: str, tenant_id: str, code: str) -> str:
    result = await db.execute(
        select(ChannelBindCode).where(
            ChannelBindCode.channel == "lark",
            ChannelBindCode.code_hash == _hash_code(code),
            ChannelBindCode.used_at.is_(None),
            ChannelBindCode.expires_at > datetime.now(timezone.utc),
        )
    )
    bind_code = result.scalar_one_or_none()
    if not bind_code:
        return "绑定码无效或已过期，请在 Trove AI 个人设置中重新生成。"
    await db.execute(
        update(ChannelBinding)
        .where(
            ChannelBinding.channel == "lark",
            ((ChannelBinding.user_id == bind_code.user_id) | (ChannelBinding.external_user_id == external_user_id)),
            ChannelBinding.is_active.is_(True),
        )
        .values(is_active=False, unbound_at=datetime.now(timezone.utc))
    )
    db.add(ChannelBinding(
        channel="lark", user_id=bind_code.user_id, external_user_id=external_user_id,
        external_tenant_id=tenant_id or None, display_name="飞书用户", is_active=True,
    ))
    bind_code.used_at = datetime.now(timezone.utc)
    await db.commit()
    return "绑定成功。现在可以直接发链接、问题、文件、图片或语音给我。"


async def _process_message_event(payload: dict) -> None:
    header = payload.get("header") or {}
    event = payload.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    event_id = header.get("event_id") or message.get("message_id")
    chat_id = message.get("chat_id")
    external_user_id = ((sender.get("sender_id") or {}).get("open_id") or "").strip()
    if not event_id or not chat_id or not external_user_id:
        return

    lark = LarkClient()
    try:
        async with async_session() as db:
            duplicate = await db.execute(select(ChannelEvent.id).where(ChannelEvent.event_id == event_id))
            if duplicate.scalar_one_or_none():
                return
            db.add(ChannelEvent(channel="lark", event_id=event_id))
            await db.commit()

            if message.get("chat_type") != "p2p" and not message.get("mentions"):
                return
            try:
                content = json.loads(message.get("content") or "{}")
            except json.JSONDecodeError:
                content = {}
            msg_type = message.get("message_type") or ""
            text = (content.get("text") or "").strip()
            text = re.sub(r"@_user_\d+\s*", "", text).strip()

            bind_match = re.fullmatch(r"/bind\s+(\d{6})", text)
            if bind_match:
                if message.get("chat_type") != "p2p":
                    await lark.send_text(chat_id, "请在与机器人的私聊中发送绑定码。")
                    return
                reply = await _bind_external_user(db, external_user_id, header.get("tenant_key") or "", bind_match.group(1))
                await lark.send_text(chat_id, reply)
                return

            binding_result = await db.execute(
                select(ChannelBinding).where(
                    ChannelBinding.channel == "lark",
                    ChannelBinding.external_user_id == external_user_id,
                    ChannelBinding.is_active.is_(True),
                )
            )
            binding = binding_result.scalar_one_or_none()
            if not binding:
                await lark.send_text(chat_id, "还没有绑定 Trove AI。请到网页端个人设置生成绑定码，再私聊发送 /bind 绑定码。")
                return
            binding.last_seen_at = datetime.now(timezone.utc)
            await db.commit()

            bound_user = await db.get(User, binding.user_id)
            if not bound_user or not bound_user.is_active:
                await lark.send_text(chat_id, "绑定的 Trove AI 用户已停用，请重新绑定。")
                return
            backend_token = create_access_token(bound_user.id, bound_user.username, bound_user.is_super_admin)
            backend = ChannelBackendClient(os.getenv("CHANNEL_BACKEND_URL", "http://backend:8000"), backend_token)

            if msg_type == "text":
                if text in {"/help", "帮助"}:
                    await lark.send_text(chat_id, "直接发链接可入库；发『把链接入库到 X 文件夹』可自动归类；发问题可调用知识管理 Agent；也支持文件、图片和语音输入。")
                    return
                url_match = URL_RE.search(text)
                if text.lower() in {"确认", "确认执行", "执行", "执行吧", "同意", "ok", "yes"} and binding.pending_action:
                    try:
                        pending = json.loads(binding.pending_action)
                        reply = await backend.execute(binding.user_id, pending, binding.agent_session_id)
                    finally:
                        binding.pending_action = None
                        await db.commit()
                elif url_match and not _has_url_folder_intent(text):
                    reply = await backend.add_url(binding.user_id, url_match.group(0))
                else:
                    result = await backend.agent_result(binding.user_id, text, binding.agent_session_id)
                    reply = result.get("text") or "Agent 没有返回可展示的文字。"
                    binding.agent_session_id = result.get("session_id") or binding.agent_session_id
                    binding.pending_action = json.dumps(result.get("pending"), ensure_ascii=False) if result.get("pending") else None
                    await db.commit()
                await lark.send_text(chat_id, reply)
                return

            if msg_type in {"file", "image", "audio", "media"}:
                file_key = content.get("file_key") or content.get("image_key")
                if not file_key:
                    await lark.send_text(chat_id, "没有拿到附件下载信息。")
                    return
                resource_type = "image" if msg_type == "image" else "file"
                data = await lark.download_resource(message.get("message_id"), file_key, resource_type)
                if len(data) > 80 * 1024 * 1024:
                    await lark.send_text(chat_id, "附件超过 80MB，暂不支持。")
                    return
                if msg_type in {"audio", "media"}:
                    suffix = ".opus" if msg_type == "audio" else ".mp4"
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
                        temp.write(data)
                        temp_path = Path(temp.name)
                    try:
                        transcript = (await transcription_service._transcribe_local(temp_path) or "").strip()
                    finally:
                        temp_path.unlink(missing_ok=True)
                    if not transcript:
                        await lark.send_text(chat_id, "语音没有识别出文字，请稍后重试。")
                        return
                    await lark.send_text(chat_id, f"我听到的是：{transcript}")
                    result = await backend.agent_result(binding.user_id, transcript, binding.agent_session_id)
                    binding.agent_session_id = result.get("session_id") or binding.agent_session_id
                    binding.pending_action = json.dumps(result.get("pending"), ensure_ascii=False) if result.get("pending") else None
                    await db.commit()
                    await lark.send_text(chat_id, result.get("text") or "Agent 没有返回可展示的文字。")
                    return
                filename = content.get("file_name") or (f"lark-image-{message.get('create_time', '')}.png" if msg_type == "image" else "lark-file")
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                await lark.send_text(chat_id, await backend.upload(binding.user_id, filename, data, content_type))
                return

            await lark.send_text(chat_id, "目前支持文本、链接、文件、图片和语音。")
    except Exception:
        logger.exception("Feishu event processing failed event_id=%s", event_id)
        try:
            async with async_session() as cleanup_db:
                await cleanup_db.execute(delete(ChannelEvent).where(ChannelEvent.event_id == event_id))
                await cleanup_db.commit()
        except Exception:
            pass
        try:
            await lark.send_text(chat_id, "处理失败，请稍后再试。")
        except Exception:
            pass
