"""System management endpoints — cache clear, rebuild, stats, config."""
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends

from app.dependencies import require_superadmin_strict
from app.models.user import User

router = APIRouter(prefix="/api/system", tags=["system"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
APP_NAME = "Trove AI"
APP_VERSION = os.getenv("TROVE_VERSION", "1.4.1")
APP_REPO = os.getenv("TROVE_REPO_URL", "https://github.com/weaiw/trove-ai")
APP_RELEASES_URL = f"{APP_REPO.rstrip('/')}/releases"


def _github_latest_release_api(repo_url: str) -> Optional[str]:
    parsed = urlparse(repo_url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    return f"https://api.github.com/repos/{owner}/{repo}/releases/latest"


def _git_commit() -> Optional[str]:
    env_commit = os.getenv("TROVE_COMMIT") or os.getenv("GIT_COMMIT")
    if env_commit:
        return env_commit[:12]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass
    return None


def _normalize_version(v: str) -> str:
    return (v or "").strip().lstrip("v")


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for part in _normalize_version(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts or [0])


def _is_newer(latest: str, current: str) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


@router.get("/stats")
async def system_stats(
    _super: User = Depends(require_superadmin_strict),
):
    """Get system statistics."""
    next_dir = FRONTEND_DIR / ".next"
    cache_size_mb = 0
    if next_dir.exists():
        total = sum(f.stat().st_size for f in next_dir.rglob("*") if f.is_file())
        cache_size_mb = round(total / (1024 * 1024), 2)
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "cache_size_mb": cache_size_mb,
        "cache_exists": next_dir.exists(),
    }


@router.get("/version")
async def system_version():
    """Public version metadata used by UI diagnostics."""
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "commit": _git_commit(),
        "repo": APP_REPO,
        "releases_url": APP_RELEASES_URL,
        "update_check_enabled": os.getenv("TROVE_UPDATE_CHECK", "true").lower() not in {"0", "false", "no"},
    }


@router.get("/update-check")
async def update_check(
    _super: User = Depends(require_superadmin_strict),
):
    """Best-effort GitHub Releases update check for self-hosted deployments."""
    enabled = os.getenv("TROVE_UPDATE_CHECK", "true").lower() not in {"0", "false", "no"}
    checked_at = datetime.now(timezone.utc).isoformat()
    if not enabled:
        return {
            "ok": False,
            "disabled": True,
            "current": APP_VERSION,
            "latest": None,
            "has_update": False,
            "checked_at": checked_at,
            "message": "更新检查已关闭",
        }

    latest_release_api = _github_latest_release_api(APP_REPO)
    if not latest_release_api:
        return {
            "ok": False,
            "current": APP_VERSION,
            "latest": None,
            "has_update": False,
            "checked_at": checked_at,
            "message": "当前仓库不是 GitHub Releases，无法自动检查更新",
        }

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(
                latest_release_api,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "TroveAI-VersionCheck/1.0",
                },
            )
        if resp.status_code >= 400:
            return {
                "ok": False,
                "current": APP_VERSION,
                "latest": None,
                "has_update": False,
                "checked_at": checked_at,
                "message": f"GitHub 返回 {resp.status_code}",
            }
        data = resp.json()
        latest = _normalize_version(data.get("tag_name") or data.get("name") or "")
        return {
            "ok": True,
            "current": APP_VERSION,
            "latest": latest or None,
            "has_update": bool(latest and _is_newer(latest, APP_VERSION)),
            "release_url": data.get("html_url") or APP_RELEASES_URL,
            "published_at": data.get("published_at"),
            "checked_at": checked_at,
            "message": "检查完成",
        }
    except Exception as exc:
        return {
            "ok": False,
            "current": APP_VERSION,
            "latest": None,
            "has_update": False,
            "checked_at": checked_at,
            "message": f"无法检查更新: {type(exc).__name__}",
        }


@router.delete("/cache")
async def clear_cache(_super: User = Depends(require_superadmin_strict)):
    """Clear frontend .next build cache and restart frontend container."""
    result = {"action": "clear_cache", "steps": []}
    next_dir = FRONTEND_DIR / ".next"
    if next_dir.exists():
        shutil.rmtree(str(next_dir))
        result["steps"].append({"step": "remove_next", "status": "ok", "detail": ".next directory removed"})
    else:
        result["steps"].append({"step": "remove_next", "status": "skipped", "detail": ".next directory not found"})
    try:
        subprocess.run(
            ["docker", "compose", "restart", "frontend"],
            cwd=str(PROJECT_ROOT), capture_output=True, timeout=30,
        )
        result["steps"].append({"step": "restart_frontend", "status": "ok", "detail": "Frontend container restarted"})
    except Exception as e:
        result["steps"].append({"step": "restart_frontend", "status": "error", "detail": str(e)})
    result["success"] = True
    result["message"] = "缓存已清除，前端正在重启（约10秒后生效）"
    return result


@router.post("/rebuild")
async def rebuild_frontend(_super: User = Depends(require_superadmin_strict)):
    """Rebuild frontend Docker image from scratch and restart."""
    try:
        subprocess.run(
            ["docker", "compose", "build", "--no-cache", "frontend"],
            cwd=str(PROJECT_ROOT), capture_output=True, timeout=300,
        )
        subprocess.run(
            ["docker", "compose", "up", "-d", "frontend"],
            cwd=str(PROJECT_ROOT), capture_output=True, timeout=60,
        )
        return {"success": True, "message": "前端已重新构建并部署（约30秒后生效）", "action": "rebuild"}
    except Exception as e:
        return {"success": False, "message": f"构建失败: {str(e)}", "action": "rebuild"}


# ============================================================
# Configuration management — uses config_manager.py functions
# ============================================================

from app.config_manager import (
    CONFIG_SCHEMA, get_effective_config, save_config, get_masked_config,
    test_llm_connection, test_embedding_connection, test_vision_connection, test_search_connection,
)
from app.services.lark_service import test_lark_connection
from app.services.proxy_service import apply_proxy_config, test_proxy_connection


@router.get("/config")
async def get_all_configs(_super: User = Depends(require_superadmin_strict)):
    """Get all configuration groups with masked values."""
    groups = []
    for group_name, schema in CONFIG_SCHEMA.items():
        fields = []
        for f in schema.get("fields", []):
            fields.append({
                "key": f["key"],
                "label": f["label"],
                "type": f.get("type", "text"),
                "required": f.get("required", False),
                "placeholder": f.get("placeholder", ""),
                "options": f.get("options"),
            })
        # Get current values (masked)
        try:
            values = get_masked_config(group_name)
        except Exception:
            values = {}
        groups.append({
            "name": group_name,
            "fields": fields,
            "_values": values,
        })
    return {"groups": groups}


def _merge_with_saved(group_name: str, body: dict) -> dict:
    """Merge incoming form values with effective (saved) config so that fields
    the user didn't change — most commonly the masked `api_key` placeholder
    like `abcd****wxyz` — are replaced with the real saved value. Otherwise
    the test sends the masked string as the key and the upstream returns 401.
    """
    from app.config_manager import get_effective_config
    try:
        saved = get_effective_config(group_name)
    except Exception:
        saved = {}
    merged = dict(saved or {})
    for k, v in (body or {}).items():
        # Skip empty values or masked placeholders — keep the saved one
        if v is None:
            continue
        if isinstance(v, str):
            if v == "" or "****" in v:
                continue
        merged[k] = v
    return merged


@router.post("/config/{group_name}/test")
async def test_config(group_name: str, body: dict, _super: User = Depends(require_superadmin_strict)):
    """Test connectivity for a config group without saving."""
    body = _merge_with_saved(group_name, body or {})
    t0 = time.time()
    try:
        if group_name == "llm":
            result = await test_llm_connection(body)
        elif group_name == "embedding":
            result = await test_embedding_connection(body)
        elif group_name == "vision":
            result = await test_vision_connection(body)
        elif group_name == "search":
            result = await test_search_connection(body)
        elif group_name == "proxy":
            result = await test_proxy_connection(body)
        elif group_name == "lark":
            result = await test_lark_connection(body)
        elif group_name in CONFIG_SCHEMA and CONFIG_SCHEMA[group_name].get("test_provider") is None:
            result = {"ok": True, "message": "该配置无需连接测试"}
        else:
            return {"success": False, "message": f"未知配置组: {group_name}"}
        latency_ms = round((time.time() - t0) * 1000)
        return {
            "ok": result.get("ok", False),
            "message": result.get("error") or result.get("message", "连接成功"),
            "latency_ms": latency_ms,
        }
    except Exception as e:
        latency_ms = round((time.time() - t0) * 1000)
        return {"ok": False, "message": f"连接失败: {str(e)}", "latency_ms": latency_ms}


@router.put("/config/{group_name}")
async def update_config(group_name: str, body: dict, _super: User = Depends(require_superadmin_strict)):
    """Update config — tests connectivity first, saves only on success."""
    body = _merge_with_saved(group_name, body or {})
    t0 = time.time()

    # Test connectivity first
    try:
        if group_name == "llm":
            test_result = await test_llm_connection(body)
        elif group_name == "embedding":
            test_result = await test_embedding_connection(body)
        elif group_name == "vision":
            test_result = await test_vision_connection(body)
        elif group_name == "search":
            test_result = await test_search_connection(body)
        elif group_name == "proxy":
            test_result = await test_proxy_connection(body)
        elif group_name == "lark":
            test_result = await test_lark_connection(body)
        elif group_name in CONFIG_SCHEMA and CONFIG_SCHEMA[group_name].get("test_provider") is None:
            test_result = {"ok": True, "message": "该配置无需连接测试"}
        else:
            return {"success": False, "message": f"未知配置组: {group_name}"}
    except Exception as e:
        return {"success": False, "message": f"连通性测试异常: {str(e)}，配置未保存"}

    if not test_result.get("ok"):
        return {
            "success": False,
            "message": f"连通性测试失败: {test_result.get('error') or test_result.get('message', '未知错误')}，配置未保存",
        }

    if group_name == "proxy" and str(body.get("enabled", "false")).lower() == "true":
        apply_result = await apply_proxy_config(body)
        if not apply_result.get("ok"):
            return {"success": False, "message": f"{apply_result.get('error', 'Mihomo 配置应用失败')}，配置未保存"}

    latency_ms = round((time.time() - t0) * 1000)

    # Save
    values = {}
    for k, v in body.items():
        if v and str(v).strip():
            values[k] = str(v).strip()

    try:
        save_config(group_name, values)
    except Exception as e:
        return {"success": False, "message": f"配置保存失败: {str(e)}"}

    return {
        "success": True,
        "message": f"配置已保存（测试通过，延迟 {latency_ms}ms）",
        "latency_ms": latency_ms,
    }
