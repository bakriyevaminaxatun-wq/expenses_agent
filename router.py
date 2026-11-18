"""
Model seçimi (router).

"Avtomatik" rejimdə hər istifadəçi mesajı üçün agent ÖZÜ qərar verir ki, hansı model işləsin:
  1. Kiçik, sürətli model (Groq · Llama 3.1 8B) mesajı oxuyur və JSON ilə seçim + səbəb qaytarır.
  2. Router əlçatan deyilsə (açar yoxdur, xəta, limit) — sadə qaydalara əsaslanan ehtiyat seçim.
  3. Yalnız bir modelin açarı varsa — həmin model.
  4. Bu yaxında limitə düşmüş modellər (models.limited_models) seçilmir.
  OpenRouter router tərəfindən seçilmir — istifadəçi seçə bilər və ehtiyat model kimi işləyir.
  Claude Haiku (ödənişli) Avtomatik rejimdə seçilmir — yalnız istifadəçi seçəndə və ya
  pulsuz model qalmayanda (məs. yeganə açar odursa) işləyir.
İstifadəçi panelden modeli özü seçibsə, router işləmir.
"""

from __future__ import annotations

import json
import re

from models import (
    AUTO,
    MODELS,
    available_models,
    is_paid,
    limited_models,
    make_router_client,
    paid_fallback_allowed,
    router_model_name,
)

ROUTER_PROMPT = """\
Sən xərc agenti üçün model seçən routersən. İstifadəçinin son mesajına (və qısa kontekstə) bax
və tapşırığı hansı modelin icra edəcəyini seç.

Modellər:
- "groq": çox sürətli. SADƏ sorğular üçün: bir aydın xərc ("taksiyə 4 manat verdim"),
  sadə hesabat sualı ("bu gün nə qədər xərclədim?"), salam, mövzudan kənar sorğu.
- "gemini": Azərbaycan dilini daha yaxşı anlayır. MÜRƏKKƏB sorğular üçün: bir mesajda bir neçə xərc,
  əvvəlki mesajlara istinad ("bayaq", "o xərci", "sonuncunu sil/dəyiş"), qeyri-müəyyən və ya qarışıq
  ifadələr, büdcə təyini + analiz, bir neçə addım tələb edən tapşırıqlar, qənaət məsləhəti.

YALNIZ JSON qaytar, başqa heç nə yazma:
{"model": "groq" və ya "gemini", "reason": "Azərbaycan dilində 3-8 sözlük səbəb"}
"""

COMPLEX_HINTS = re.compile(
    r"\b(bayaq|əvvəlki|sonuncu|o xərc|onu|sil|dəyiş|düzəlt|yenilə|büdcə|limit|analiz|məsləhət|"
    r"qənaət|müqayisə|hamısı|həm|sonra|niyə)\w*",
    re.IGNORECASE,
)


def heuristic_route(message: str) -> tuple[str, str]:
    """Router modeli işləmədikdə ehtiyat qayda."""
    numbers = re.findall(r"\d+(?:[.,]\d+)?", message)
    if len(numbers) >= 2:
        return "gemini", "bir neçə məbləğ var — mürəkkəb sorğu"
    if COMPLEX_HINTS.search(message):
        return "gemini", "əvvəlki məlumata istinad və ya çoxaddımlı tapşırıq"
    if len(message) > 120:
        return "gemini", "uzun mesaj"
    return "groq", "sadə sorğu — sürətli model kifayətdir"


def _context_snippet(history: list[dict], limit: int = 4) -> str:
    """Router üçün son bir neçə mətn mesajı (alət nəticələri olmadan)."""
    lines = []
    for m in history[-12:]:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"]:
            lines.append(f"{m['role']}: {m['content'][:200]}")
    return "\n".join(lines[-limit:])


def llm_route(message: str, history: list[dict], client=None) -> tuple[str, str] | None:
    client = client or make_router_client()
    if client is None:
        return None
    try:
        resp = client.chat.completions.create(
            model=router_model_name(),
            messages=[
                {"role": "system", "content": ROUTER_PROMPT},
                {
                    "role": "user",
                    "content": f"Kontekst:\n{_context_snippet(history) or '(yoxdur)'}\n\nSon mesaj: {message}",
                },
            ],
            temperature=0,
            max_tokens=80,
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content or ""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(match.group(0) if match else text)
        choice = str(data.get("model", "")).lower().strip()
        if choice in MODELS:
            return choice, str(data.get("reason", "")).strip() or "router seçdi"
    except Exception:
        return None
    return None


def choose_model(
    message: str,
    history: list[dict],
    user_choice: str = AUTO,
    unavailable: set[str] | None = None,
    router_client=None,
) -> dict:
    """
    Qaytarır: {"model": "gemini"|"groq", "reason": str, "by": "user"|"router"|"rule"|"only"}
    """
    skip = (unavailable or set()) | limited_models()
    avail = [m for m in available_models() if m not in skip]
    if not avail:
        avail = available_models()  # hamısı limitdədirsə, yenə də cəhd edək
    if not avail:
        raise RuntimeError("Heç bir model üçün API açarı tapılmadı (GEMINI_API_KEY və ya GROQ_API_KEY).")

    if user_choice == AUTO:
        free = [m for m in avail if not is_paid(m)]
        all_free = [m for m in available_models() if not is_paid(m)]
        if free:
            avail = free  # Avtomatik rejimdə pulsuz modellərə üstünlük
        elif all_free and not paid_fallback_allowed():
            avail = all_free  # pulsuzlar limitdədir, amma icazəsiz pul xərcləmirik — yenə pulsuzu sınayırıq

    if user_choice != AUTO:
        if user_choice in avail:
            return {"model": user_choice, "reason": "istifadəçi seçdi", "by": "user"}
        if user_choice in available_models():
            free = [m for m in avail if not is_paid(m)] or avail
            return {
                "model": free[0],
                "reason": f"{MODELS[user_choice]['label']} limitdədir, müvəqqəti əvəz",
                "by": "rule",
            }
    if len(avail) == 1:
        others_exist = len([m for m in available_models() if not is_paid(m) or m == avail[0]]) > 1
        why = "digər modellər limitdədir" if others_exist else "yalnız bu modelin açarı var"
        return {"model": avail[0], "reason": why, "by": "only"}

    routed = llm_route(message, history, client=router_client)
    if routed and routed[0] in avail:
        return {"model": routed[0], "reason": routed[1], "by": "router"}

    model, reason = heuristic_route(message)
    if model not in avail:
        model = avail[0]
        reason += f" ({MODELS[model]['label']} istifadə olunur)"
    return {"model": model, "reason": reason, "by": "rule"}
