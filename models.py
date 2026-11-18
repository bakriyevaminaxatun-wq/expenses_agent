"""
Mövcud (pulsuz) modellərin reyestri.

Hər ikisi OpenAI-uyğun API ilə işləyir, ona görə eyni `openai` kitabxanası istifadə olunur.
Model adlarını .env faylında dəyişmək olar (GEMINI_MODEL, GROQ_MODEL, ROUTER_MODEL).
"""

from __future__ import annotations

import os
import re
import time

from openai import OpenAI

AUTO = "auto"

MODELS = {
    "gemini": {
        "label": "Google Gemini Flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-flash-latest",
        "key_env": "GEMINI_API_KEY",
        "strengths": "Azərbaycan dilini daha yaxşı başa düşür; mürəkkəb, çoxaddımlı tapşırıqlar üçün güclüdür.",
    },
    "groq": {
        "label": "Groq · GPT-OSS 120B",
        "base_url": "https://api.groq.com/openai/v1",
        "model_env": "GROQ_MODEL",
        "default_model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
        "strengths": "Çox sürətlidir, pulsuz limiti genişdir; sadə, birmənalı sorğular üçün idealdır.",
    },
    "openrouter": {
        "label": "OpenRouter (pulsuz modellər)",
        "base_url": "https://openrouter.ai/api/v1",
        "model_env": "OPENROUTER_MODEL",
        # openrouter/free — sorğuya uyğun (alət çağırmağı dəstəkləyən) pulsuz modeli özü seçir
        "default_model": "openrouter/free",
        "key_env": "OPENROUTER_API_KEY",
        "strengths": "Pulsuz modellərdən birini avtomatik seçir; digər modellərin limiti dolanda ehtiyat kimi.",
        "headers": {"X-Title": "Xerc Agenti"},
    },
    "anthropic": {
        "label": "Claude Haiku 5.5 (ən ucuz Claude)",
        "base_url": "https://api.anthropic.com/v1/",  # Anthropic-in OpenAI-uyğun API-si
        "model_env": "ANTHROPIC_MODEL",
        "default_model": "claude-haiku-5-5",
        "key_env": "ANTHROPIC_API_KEY",
        "strengths": "Ödənişli, amma çox ucuz (~0.1 sent/mesaj); pulsuz limitlər yoxdur, alətləri etibarlı çağırır.",
        "paid": True,
        "no_temperature": True,  # Claude 4.7+ modellərində temperature < 1 xəta verir
        # USD / 1M token (Haiku 5.5, ≤100K token-lik sorğular)
        "price_in": float(os.getenv("ANTHROPIC_PRICE_INPUT", "0.10")),
        "price_out": float(os.getenv("ANTHROPIC_PRICE_OUTPUT", "0.50")),
    },
}


def is_paid(key: str) -> bool:
    return bool(MODELS[key].get("paid"))


def paid_fallback_allowed() -> bool:
    """Pulsuz modellər limitə düşəndə ödənişli modelə avtomatik keçmək olarmı (default: yox)."""
    return os.getenv("ANTHROPIC_AS_FALLBACK", "false").strip().lower() in ("1", "true", "yes", "bəli")


def cost_usd(key: str, prompt_tokens: int, completion_tokens: int) -> float:
    m = MODELS[key]
    if not m.get("paid"):
        return 0.0
    return (prompt_tokens * m["price_in"] + completion_tokens * m["price_out"]) / 1_000_000

# Limitə düşən modeli bir müddət istifadə etmirik (boşuna sorğu göndərməmək üçün)
COOLDOWN_SECONDS = int(os.getenv("MODEL_COOLDOWN_SECONDS", "60"))
_cooldown_until: dict[str, float] = {}


def mark_limited(key: str, seconds: int | None = None) -> None:
    _cooldown_until[key] = time.time() + (COOLDOWN_SECONDS if seconds is None else seconds)


def limited_models() -> set[str]:
    now = time.time()
    return {k for k, t in _cooldown_until.items() if t > now}


def clear_cooldowns() -> None:
    _cooldown_until.clear()

# Avtomatik rejimdə hansı modelin seçiləcəyinə qərar verən kiçik, sürətli model (Groq)
ROUTER = {
    "base_url": MODELS["groq"]["base_url"],
    "model_env": "ROUTER_MODEL",
    "default_model": "llama-3.1-8b-instant",
    "key_env": "GROQ_API_KEY",
}


def model_name(key: str) -> str:
    m = MODELS[key]
    return os.getenv(m["model_env"]) or m["default_model"]


# Hər provayderin açarı adətən belə başlayır (yalnız xəbərdarlıq üçün)
KEY_PREFIX = {"gemini": "AIza", "groq": "gsk_", "openrouter": "sk-or-", "anthropic": "sk-ant-"}
PLACEHOLDER = re.compile(r"your|sizin|açar|acar|xxx|here|example|placeholder", re.IGNORECASE)


def _raw_key(key: str) -> str:
    return (os.getenv(MODELS[key]["key_env"]) or "").strip().strip('"').strip("'").strip()


def key_problem(key: str) -> str | None:
    """Açar yazılıb, amma səhv görünürsə — səbəbini qaytarır."""
    v = _raw_key(key)
    env = MODELS[key]["key_env"]
    if not v:
        return None
    if PLACEHOLDER.search(v) or not v.isascii() or " " in v:
        return f"{env} nümunə mətndir və ya səhv simvollar var — real açarı yapışdırın."
    prefix = KEY_PREFIX.get(key)
    if prefix and not v.startswith(prefix):
        return f"{env} adətən `{prefix}` ilə başlayır — açarı düzgün kopyaladığınızı yoxlayın."
    return None


def api_key(key: str) -> str | None:
    """Etibarlı açar və ya None (nümunə/səhv mətn açar sayılmır)."""
    v = _raw_key(key)
    if not v or PLACEHOLDER.search(v) or not v.isascii() or " " in v:
        return None
    return v


def available_models() -> list[str]:
    """Açarı .env-də olan modellər."""
    return [k for k in MODELS if api_key(k)]


def make_client(key: str) -> OpenAI:
    m = MODELS[key]
    return OpenAI(
        api_key=api_key(key),
        base_url=m["base_url"],
        default_headers=m.get("headers"),
        max_retries=0,
        timeout=60,
    )


def make_router_client() -> OpenAI | None:
    k = api_key("groq")
    if not k:
        return None
    return OpenAI(api_key=k, base_url=ROUTER["base_url"], max_retries=0, timeout=15)


def router_model_name() -> str:
    return os.getenv(ROUTER["model_env"]) or ROUTER["default_model"]
