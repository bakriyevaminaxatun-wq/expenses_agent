"""
Agent dövrü (agent loop).

0. Model seçimi: istifadəçi seçib, yoxsa "Avtomatik" — router.py qərar verir (Gemini / Groq)
1. düşün → alət çağır → nəticəni oxu → (lazım olsa yenə alət) → cavab ver
2. Seçilən modelin pulsuz limiti dolarsa, agent avtomatik digər modelə keçir.

Hər iki model OpenAI-uyğun API ilə işləyir. Model hər addımda özü qərar verir:
alət çağırmaq (tool_calls) yoxsa yekun cavab vermək.
"""

from __future__ import annotations

import copy
import json
import os
import time
from typing import Callable

from openai import APIConnectionError, APIStatusError, AuthenticationError, PermissionDeniedError, RateLimitError

from models import (
    AUTO,
    MODELS,
    available_models,
    cost_usd,
    is_paid,
    limited_models,
    make_client,
    mark_limited,
    model_name,
    paid_fallback_allowed,
)
from prompts import build_system_prompt
from router import choose_model
from tools import TOOLS, execute_tool

MAX_STEPS = int(os.getenv("MAX_AGENT_STEPS", "8"))
# Modelə yalnız son N mesaj göndərilir — tokenlərə qənaət (pulsuz limitlər tez dolmasın)
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "24"))
MAX_RETRIES = 2
GEMINI_DUMMY_SIGNATURE = "skip_thought_signature_validator"

ToolCallback = Callable[[str, dict, dict], None]
EventCallback = Callable[[str, dict], None]


class ModelUnavailable(Exception):
    """Model müvəqqəti işləmir (limit, server xətası, şəbəkə)."""


def openai_tools() -> list[dict]:
    """tools.py-dəki sxemləri OpenAI "function" formatına çevirir."""
    out = []
    for t in TOOLS:
        fn = {"name": t["name"], "description": t["description"]}
        if t["input_schema"].get("properties"):
            fn["parameters"] = t["input_schema"]
        out.append({"type": "function", "function": fn})
    return out


def _assistant_to_dict(msg, model_key: str) -> dict:
    """
    Modelin cavabını tarixçəyə yazır. tool_calls tam saxlanılır (Gemini-nin
    `extra_content` / thought_signature sahəsi daxil). `_model` yalnız bizim üçündür,
    API-yə göndərilmir.
    """
    data = msg.model_dump(exclude_none=True)
    out = {"role": "assistant", "content": data.get("content") or "", "_model": model_key}
    if data.get("tool_calls"):
        out["tool_calls"] = data["tool_calls"]
    if data.get("extra_content"):
        out["extra_content"] = data["extra_content"]
    return out


def prepare_messages(messages: list[dict], model_key: str) -> list[dict]:
    """
    Eyni söhbət tarixçəsini (YADDAŞ) hər iki modelə uyğun formaya salır:
    - Groq: Gemini-yə məxsus `extra_content` sahələri silinir.
    - Gemini: başqa modelin yaratdığı tool_calls-a "dummy" thought_signature əlavə olunur.
    """
    out = []
    for m in messages:
        m = copy.deepcopy(m)
        m.pop("_model", None)
        if m.get("role") == "assistant":
            if model_key == "gemini":
                for tc in m.get("tool_calls", []):
                    tc.setdefault("extra_content", {"google": {"thought_signature": GEMINI_DUMMY_SIGNATURE}})
            else:
                m.pop("extra_content", None)
                for tc in m.get("tool_calls", []):
                    tc.pop("extra_content", None)
        out.append(m)
    return out


def trim_history(messages: list[dict], max_messages: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """
    Uzun söhbətdə yalnız son hissəni göndərir. Kəsmə nöqtəsi həmişə istifadəçi mesajında olur,
    ki, alət çağırışı və onun nəticəsi (tool_calls ↔ tool) ayrılmasın. Cari sorğu heç vaxt kəsilmir.
    Tam tarixçə yaddaşda qalır — "bayaq" kimi istinadlar üçün son mesajlar kifayətdir.
    """
    if len(messages) <= max_messages:
        return messages

    def is_user_text(m):
        return m.get("role") == "user" and isinstance(m.get("content"), str)

    current = max(i for i, m in enumerate(messages) if is_user_text(m)) if any(map(is_user_text, messages)) else 0
    start = len(messages) - max_messages
    while start < current and not is_user_text(messages[start]):
        start += 1
    return messages[min(start, current):]


class ExpenseAgent:
    def __init__(self, clients: dict | None = None, router_client=None):
        """`clients` / `router_client` yalnız testlər üçündür (saxta model ötürmək)."""
        self._clients = clients or {}
        self._router_client = router_client
        self.tools = openai_tools()

    def _client(self, key: str):
        if key not in self._clients:
            self._clients[key] = make_client(key)
        return self._clients[key]

    def _call_model(self, key: str, messages: list[dict]):
        """Qısa xətalarda bir dəfə təkrar cəhd; davam edərsə ModelUnavailable."""
        for attempt in range(MAX_RETRIES):
            try:
                kwargs = {
                    "model": model_name(key),
                    "messages": prepare_messages(messages, key),
                    "tools": self.tools,
                    "tool_choice": "auto",
                }
                if not MODELS[key].get("no_temperature"):
                    kwargs["temperature"] = 0.2
                return self._client(key).chat.completions.create(**kwargs)
            except RateLimitError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise ModelUnavailable(f"{MODELS[key]['label']}: pulsuz limit doldu") from exc
                time.sleep(3)
            except (AuthenticationError, PermissionDeniedError) as exc:
                # 401/403: açar səhvdir — bu modeli 10 dəqiqəlik kənara qoyuruq, digərinə keçirik
                mark_limited(key, 600)
                raise ModelUnavailable(
                    f"{MODELS[key]['label']}: API açarı qəbul olunmadı ({MODELS[key]['key_env']}-i yoxlayın)"
                ) from exc
            except APIConnectionError as exc:
                raise ModelUnavailable(f"{MODELS[key]['label']}: bağlantı xətası") from exc
            except APIStatusError as exc:
                if exc.status_code >= 500:
                    if attempt == MAX_RETRIES - 1:
                        raise ModelUnavailable(f"{MODELS[key]['label']}: server xətası") from exc
                    time.sleep(2)
                    continue
                raise

    def run(
        self,
        messages: list[dict],
        model_choice: str = AUTO,
        on_tool_call: ToolCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> dict:
        """
        `messages` — bütün söhbət tarixçəsi (YADDAŞ); yerində yenilənir.
        Qaytarır: {"text", "model", "route": {...}, "fallback": None | str}
        """
        emit = on_event or (lambda *_: None)
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user" and isinstance(m.get("content"), str)),
            "",
        )

        route = choose_model(last_user, messages[:-1], model_choice, router_client=self._router_client)
        key = route["model"]
        emit("route", {**route, "label": MODELS[key]["label"]})

        system = {"role": "system", "content": build_system_prompt()}
        tried: set[str] = set()
        fallback_note = None
        steps = 0
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

        def finish(text: str) -> dict:
            return {"text": text, "model": key, "route": route, "fallback": fallback_note, "usage": usage}

        while steps < MAX_STEPS:
            try:
                response = self._call_model(key, [system, *trim_history(messages)])
            except ModelUnavailable as exc:
                tried.add(key)
                if key not in limited_models():
                    mark_limited(key)
                limited = limited_models()
                allowed = [
                    m for m in available_models()
                    if m not in tried and (not is_paid(m) or paid_fallback_allowed() or model_choice == m)
                ]
                others = [m for m in allowed if m not in limited]
                others += [m for m in allowed if m in limited and m not in others]
                if not others:
                    raise RuntimeError(f"{exc}. 30-60 saniyə gözləyib yenidən yazın.") from exc
                new_key = others[0]
                fallback_note = f"{exc} → {MODELS[new_key]['label']} modelinə keçdim"
                emit("fallback", {"from": key, "to": new_key, "reason": str(exc), "label": MODELS[new_key]["label"]})
                key = new_key
                continue

            steps += 1
            u = getattr(response, "usage", None)
            if u:
                pt, ct = getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0
                usage["prompt_tokens"] += pt
                usage["completion_tokens"] += ct
                usage["cost_usd"] += cost_usd(key, pt, ct)
            msg = response.choices[0].message
            messages.append(_assistant_to_dict(msg, key))

            if not msg.tool_calls:
                return finish((msg.content or "").strip())

            # Model alət(lər) çağırmağa qərar verdi — icra edib nəticəni geri qaytarırıq
            for call in msg.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = execute_tool(call.function.name, args)
                if on_tool_call:
                    on_tool_call(call.function.name, args, result)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)}
                )

        return finish("Bağışlayın, sorğunu tamamlaya bilmədim (addım limiti keçildi). Zəhmət olmasa yenidən yazın.")
