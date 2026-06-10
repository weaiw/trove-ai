"""System management endpoints — cache clear, rebuild, stats, config."""
import os
import shutil
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Depends

from app.dependencies import require_superadmin
from app.models.user import User

router = APIRouter(prefix="/api/system", tags=["system"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"


@router.get("/stats")
async def system_stats(
    _super: User = Depends(require_superadmin),
):
    """Get system statistics."""
    next_dir = FRONTEND_DIR / ".next"
    cache_size_mb = 0
    if next_dir.exists():
        total = sum(f.stat().st_size for f in next_dir.rglob("*") if f.is_file())
        cache_size_mb = round(total / (1024 * 1024), 2)
    return {
        "app": "Trove AI",
        "version": "1.2",
        "cache_size_mb": cache_size_mb,
        "cache_exists": next_dir.exists(),
    }


@router.delete("/cache")
async def clear_cache():
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
async def rebuild_frontend():
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
    test_llm_connection, test_embedding_connection,
)


@router.get("/config")
async def get_all_configs():
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
async def test_config(group_name: str, body: dict):
    """Test connectivity for a config group without saving."""
    body = _merge_with_saved(group_name, body or {})
    t0 = time.time()
    try:
        if group_name == "llm":
            result = await test_llm_connection(body)
        elif group_name == "embedding":
            result = await test_embedding_connection(body)
        elif group_name in CONFIG_SCHEMA and not CONFIG_SCHEMA[group_name].get("test_provider"):
            # Config groups without a connectivity test (e.g. xhs cookie)
            return {"ok": True, "message": "该配置无连通性测试，保存即可生效", "latency_ms": 0}
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
async def update_config(group_name: str, body: dict):
    """Update config — tests connectivity first, saves only on success."""
    body = _merge_with_saved(group_name, body or {})
    t0 = time.time()

    # Test connectivity first (groups without test_provider skip the gate)
    skip_test = (
        group_name in CONFIG_SCHEMA
        and not CONFIG_SCHEMA[group_name].get("test_provider")
    )
    if skip_test:
        test_result = {"ok": True}
    else:
        try:
            if group_name == "llm":
                test_result = await test_llm_connection(body)
            elif group_name == "embedding":
                test_result = await test_embedding_connection(body)
            else:
                return {"success": False, "message": f"未知配置组: {group_name}"}
        except Exception as e:
            return {"success": False, "message": f"连通性测试异常: {str(e)}，配置未保存"}

        if not test_result.get("ok"):
            return {
                "success": False,
                "message": f"连通性测试失败: {test_result.get('error') or test_result.get('message', '未知错误')}，配置未保存",
            }

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
