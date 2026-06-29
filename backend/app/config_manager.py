"""
Config Manager — JSON-based persistent config with env var fallback.

Layered config priority (highest to lowest):
  1. config_store.json (user overrides via settings UI)
  2. Environment variables (docker-compose.yml / .env)
  3. Built-in defaults

This allows the settings page to update config without modifying
Docker env vars. Values are read via get_effective_config() which
merges the layers correctly.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
from functools import lru_cache

import httpx

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent / "config_store.json"

# === Schema definition ===
# Each config group has:
#   - fields: list of {key, label, type, default, placeholder, required}
#   - test: provider name for connectivity test (None = no test)

CONFIG_SCHEMA: Dict[str, dict] = {
    "llm": {
        "label": "AI 对话模型",
        "description": "用于生成摘要、知识图谱、灵感创作等",
        "icon": "brain",
        "fields": [
            {
                "key": "provider",
                "label": "服务商",
                "type": "select",
                "options": [
                    {"value": "siliconflow", "label": "硅基流动"},
                    {"value": "minimax", "label": "MiniMax"},
                    {"value": "openai", "label": "OpenAI"},
                    {"value": "xunfei-coding", "label": "讯飞星辰 (MaaS)"},
                    {"value": "zhipu", "label": "智谱 AI"},
                    {"value": "custom", "label": "自定义"},
                ],
                "default": "siliconflow",
                "required": False,
            },
            {
                "key": "api_key",
                "label": "API Key",
                "type": "password",
                "default": "",
                "placeholder": "sk-... 或 MiniMax Key",
                "required": True,
                "secret": True,
            },
            {
                "key": "api_base",
                "label": "API 地址",
                "type": "url",
                "default": "https://api.deepseek.com/v1",
                "placeholder": "https://api.deepseek.com/v1/chat/completions",
                "required": True,
            },
            {
                "key": "model",
                "label": "模型名称",
                "type": "text",
                "default": "deepseek-chat",
                "placeholder": "deepseek-chat",
                "required": True,
            },
        ],
        "test_provider": "llm",
    },
    "embedding": {
        "label": "嵌入模型",
        "description": "用于语义搜索的向量化模型",
        "icon": "layers",
        "fields": [
            {
                "key": "provider",
                "label": "服务商",
                "type": "select",
                "options": [
                    {"value": "local", "label": "本地模型 (fastembed)"},
                    {"value": "siliconflow", "label": "硅基流动"},
                    {"value": "openai", "label": "OpenAI"},
                    {"value": "minimax", "label": "MiniMax"},
                ],
                "default": "local",
                "required": False,
            },
            {
                "key": "api_key",
                "label": "API Key",
                "type": "password",
                "default": "",
                "placeholder": "硅基流动 / OpenAI API Key",
                "required": False,
                "secret": True,
            },
            {
                "key": "api_base",
                "label": "API 地址",
                "type": "url",
                "default": "https://api.siliconflow.cn/v1",
                "placeholder": "https://api.siliconflow.cn/v1",
                "required": False,
            },
            {
                "key": "model",
                "label": "模型名称",
                "type": "text",
                "default": "BAAI/bge-small-en-v1.5",
                "placeholder": "BAAI/bge-small-en-v1.5",
                "required": True,
            },
        ],
        "test_provider": "embedding",
    },
    "xhs": {
        "label": "小红书",
        "description": (
            "用于解析小红书笔记（web_session cookie，几周到几个月失效一次）。"
            "从浏览器登录 xiaohongshu.com 后，F12 → Application → Cookies "
            "复制 web_session 的值粘贴到此处。失效后文章会标"
            "「解析失败」，重新获取一次即可。"
        ),
        "icon": "cookie",
        "fields": [
            {
                "key": "web_session",
                "label": "web_session cookie",
                "type": "password",
                "default": "",
                "placeholder": "粘贴 web_session 的值",
                "required": False,
                "secret": True,
            },
        ],
        "test_provider": None,
    },
}


# ============================================================
#  Config store — read/write JSON overrides
# ============================================================

def _load_overrides() -> dict:
    """Load user overrides from JSON file."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load config_store.json: {e}")
    return {}


def _save_overrides(overrides: dict) -> None:
    """Save user overrides to JSON file."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(overrides, f, indent=2, ensure_ascii=False)
    logger.info("Config overrides saved to config_store.json")


# ============================================================
#  Effective config — merge layers
# ============================================================

def get_effective_config(group: str) -> Dict[str, Any]:
    """Get fully resolved config for a group (overrides > env > defaults)."""
    schema = CONFIG_SCHEMA.get(group)
    if not schema:
        return {}

    overrides = _load_overrides().get(group, {})
    result = {"_group": group}

    for field in schema["fields"]:
        key = field["key"]
        # Priority: JSON override > env var > field default
        if key in overrides and overrides[key] not in (None, ""):
            result[key] = overrides[key]
        else:
            # Try env var: LLM_API_KEY, MINIMAX_API_KEY, EMBEDDING_MODEL etc.
            env_key = f"{group.upper()}_{key.upper()}"
            env_val = os.environ.get(env_key)
            # Also try legacy env var names
            if not env_val and group == "llm" and key == "api_key":
                env_val = os.environ.get("MINIMAX_API_KEY")
            if not env_val and group == "llm" and key == "api_base":
                env_val = os.environ.get("MINIMAX_API_BASE")
            if not env_val and group == "llm" and key == "model":
                env_val = os.environ.get("LLM_MODEL")

            result[key] = env_val if env_val else field.get("default", "")

    return result


def save_config(group: str, values: Dict[str, Any]) -> None:
    """Save user overrides for a config group."""
    all_overrides = _load_overrides()
    schema = CONFIG_SCHEMA.get(group)
    if not schema:
        raise ValueError(f"Unknown config group: {group}")

    valid_keys = {f["key"] for f in schema["fields"]}
    clean = {k: v for k, v in values.items() if k in valid_keys}

    all_overrides[group] = clean
    _save_overrides(all_overrides)


def get_masked_config(group: str) -> Dict[str, Any]:
    """Get config for display — mask secret fields."""
    cfg = get_effective_config(group)
    schema = CONFIG_SCHEMA.get(group)
    if not schema:
        return cfg

    for field in schema["fields"]:
        if field.get("secret") and cfg.get(field["key"]):
            val = cfg[field["key"]]
            if len(val) > 8:
                cfg[field["key"]] = val[:4] + "****" + val[-4:]
            else:
                cfg[field["key"]] = "****"

    return cfg


# ============================================================
#  Convenience accessors (used by other modules)
# ============================================================

def get_llm_config() -> Dict[str, str]:
    """Get effective LLM config for AI service usage."""
    return get_effective_config("llm")


def get_embedding_config() -> Dict[str, str]:
    """Get effective embedding config."""
    return get_effective_config("embedding")


def get_all_schemas() -> Dict[str, dict]:
    """Get all config schemas for the settings UI."""
    result = {}
    for group, schema in CONFIG_SCHEMA.items():
        entry = {
            "group": group,
            "label": schema["label"],
            "description": schema.get("description", ""),
            "icon": schema.get("icon", "settings"),
            "fields": [],
            "test_provider": schema.get("test_provider"),
        }
        for field in schema["fields"]:
            f = dict(field)
            # Remove internal flags
            f.pop("secret", None)
            entry["fields"].append(f)
        result[group] = entry
    return result


# ============================================================
#  Connectivity testing
# ============================================================

async def test_llm_connection(config: Optional[Dict[str, Any]] = None) -> dict:
    """Test LLM API connectivity. If config provided, test with those params;
    otherwise use effective config."""
    if config is None:
        config = get_effective_config("llm")

    api_key = config.get("api_key", "")
    api_base = config.get("api_base", "https://api.deepseek.com/v1").rstrip("/")
    model = config.get("model", "deepseek-chat")

    if not api_key:
        return {"ok": False, "error": "API Key 未配置", "latency_ms": 0}

    provider = config.get("provider", "siliconflow")

    # Auto-detect: SiliconFlow and OpenAI use the same chat/completions format
    if "siliconflow" in api_base.lower() or provider in ("siliconflow", "openai", "custom", "xunfei-coding", "zhipu"):
        effective_provider = "openai"
    else:
        effective_provider = provider

    try:
        if effective_provider == "minimax":
            url = f"{api_base}/v1/text/chatcompletion_v2"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 10,
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        elif provider == "openai":
            url = f"{api_base}/chat/completions"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 10,
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        else:
            # Custom — try OpenAI-compatible endpoint
            url = f"{api_base}/chat/completions"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 10,
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

        async with httpx.AsyncClient(timeout=15.0) as client:
            import time
            t0 = time.time()
            resp = await client.post(url, headers=headers, json=payload)
            latency = round((time.time() - t0) * 1000)

            if resp.status_code == 200:
                data = resp.json()
                # Extract reply content
                reply = ""
                if "choices" in data and len(data["choices"]) > 0:
                    msg = data["choices"][0].get("message", {})
                    reply = msg.get("content", "") or msg.get("reasoning_content", "")
                return {
                    "ok": True,
                    "error": None,
                    "latency_ms": latency,
                    "detail": f"连接成功 ({latency}ms)，模型回复: {reply[:50] if reply else '(空)'}",
                }
            elif resp.status_code == 401:
                return {"ok": False, "error": "API Key 无效 (401)", "latency_ms": latency}
            elif resp.status_code == 403:
                return {"ok": False, "error": "无权限访问 (403)，请检查 API Key", "latency_ms": latency}
            else:
                body = resp.text[:200]
                return {"ok": False, "error": f"请求失败 ({resp.status_code}): {body}", "latency_ms": latency}

    except httpx.TimeoutException:
        return {"ok": False, "error": "连接超时，请检查 API 地址是否正确", "latency_ms": 0}
    except httpx.ConnectError:
        return {"ok": False, "error": "无法连接，请检查网络和 API 地址", "latency_ms": 0}
    except Exception as e:
        return {"ok": False, "error": f"连接异常: {str(e)}", "latency_ms": 0}


async def test_embedding_connection(config: Optional[Dict[str, Any]] = None) -> dict:
    """Test embedding connectivity."""
    if config is None:
        config = get_effective_config("embedding")

    provider = config.get("provider", "local")

    if provider == "local":
        try:
            import time
            t0 = time.time()
            from fastembed import TextEmbedding
            model_name = config.get("model", "BAAI/bge-small-en-v1.5")
            model = TextEmbedding(model_name=model_name)
            embeddings = list(model.embed(["test"]))
            latency = round((time.time() - t0) * 1000)
            dim = len(embeddings[0]) if embeddings else 0
            return {
                "ok": True,
                "error": None,
                "latency_ms": latency,
                "detail": f"本地模型已加载，维度: {dim} ({latency}ms)",
            }
        except Exception as e:
            return {"ok": False, "error": f"模型加载失败: {str(e)}", "latency_ms": 0}

    # API-based embedding test
    api_key = config.get("api_key", os.environ.get("MINIMAX_API_KEY", ""))
    api_base = config.get("api_base", "https://api.siliconflow.cn/v1").rstrip("/")

    if not api_key:
        return {"ok": False, "error": "API Key 未配置", "latency_ms": 0}

    try:
        if provider == "minimax":
            url = f"{api_base}/v1/embeddings"
            payload = {
                "model": "embo-01",
                "texts": ["test"],
                "type": "query",
            }
        else:
            url = f"{api_base}/embeddings"
            payload = {
                "model": config.get("model", "text-embedding-3-small"),
                "input": ["test"],
            }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            import time
            t0 = time.time()
            resp = await client.post(url, headers=headers, json=payload)
            latency = round((time.time() - t0) * 1000)

            if resp.status_code == 200:
                return {
                    "ok": True,
                    "error": None,
                    "latency_ms": latency,
                    "detail": f"连接成功 ({latency}ms)",
                }
            else:
                return {"ok": False, "error": f"请求失败 ({resp.status_code})", "latency_ms": latency}
    except Exception as e:
        return {"ok": False, "error": f"连接异常: {str(e)}", "latency_ms": 0}
