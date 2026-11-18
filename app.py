"""
Xərc Agenti — Streamlit interfeysi (Azərbaycan dilində).

İşə salmaq:  streamlit run app.py
"""

from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

import tools  # noqa: E402  (load_dotenv-dən sonra import olunmalıdır)

st.set_page_config(page_title="Xərc Agenti", page_icon="💰", layout="wide")


# --------------------------------------------------------------------------- #
# API açarı: .env və ya Streamlit Cloud secrets
# --------------------------------------------------------------------------- #
def get_secret(name: str) -> str | None:
    val = os.getenv(name)
    if val:
        return val
    try:
        return st.secrets.get(name)
    except Exception:
        return None


for key in (
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "CURRENCYLAYER_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_AS_FALLBACK",
    "GEMINI_MODEL",
    "GROQ_MODEL",
    "OPENROUTER_MODEL",
    "MAX_HISTORY_MESSAGES",
    "ROUTER_MODEL",
    "DEFAULT_MODEL_CHOICE",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
):
    v = get_secret(key)
    if v and not os.getenv(key):
        os.environ[key] = str(v)

from agent import ExpenseAgent  # noqa: E402
from models import (  # noqa: E402
    AUTO,
    MODELS,
    available_models,
    is_paid,
    key_problem,
    limited_models,
    model_name,
    paid_fallback_allowed,
    router_model_name,
)

AVAILABLE = available_models()

# --------------------------------------------------------------------------- #
# Sessiya vəziyyəti (YADDAŞ)
# --------------------------------------------------------------------------- #
if "api_messages" not in st.session_state:
    st.session_state.api_messages = []  # modelə göndərilən tam tarixçə (alət nəticələri daxil)
if "chat_log" not in st.session_state:
    st.session_state.chat_log = []  # ekranda göstərilən mesajlar
if "cost_usd" not in st.session_state:
    st.session_state.cost_usd = 0.0  # bu sessiyada ödənişli modelə xərclənən məbləğ
if "model_choice" not in st.session_state:
    st.session_state.model_choice = os.getenv("DEFAULT_MODEL_CHOICE", AUTO)

BY_ICON = {"router": "🧭", "rule": "🧭", "user": "👤", "only": "🔑"}


def render_route(route: dict | None, fallback: str | None = None) -> None:
    if not route:
        return
    who = {"router": "agent seçdi", "rule": "agent seçdi (qayda)", "user": "siz seçdiniz", "only": "yeganə model"}
    cost = f" · 💲 {route['cost_cents']:.3f} sent" if route.get("cost_cents") else ""
    st.caption(
        f"{BY_ICON.get(route['by'], '🤖')} **{route['label']}** — {who.get(route['by'], '')}: {route['reason']}{cost}"
    )
    if fallback:
        st.caption(f"🔁 {fallback}")


def render_tool_calls(calls: list[dict]) -> None:
    for c in calls:
        ok = c["result"].get("ok", True)
        icon = "✅" if ok else "⚠️"
        with st.expander(f"🔧 `{c['name']}` çağırıldı {icon}", expanded=False):
            st.caption("Giriş:")
            st.code(json.dumps(c["input"], ensure_ascii=False, indent=2), language="json")
            st.caption("Nəticə:")
            st.code(json.dumps(c["result"], ensure_ascii=False, indent=2), language="json")


# --------------------------------------------------------------------------- #
# Yan panel: büdcə, hesabat, bildirişlər
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("📊 Panel")

    s_today = tools.get_summary("today")
    s_week = tools.get_summary("week")
    s_month = tools.get_summary("month")
    c1, c2, c3 = st.columns(3)
    c1.metric("Bu gün", f"{s_today['total']:.2f} ₼")
    c2.metric("Həftə", f"{s_week['total']:.2f} ₼")
    c3.metric("Ay", f"{s_month['total']:.2f} ₼")

    st.subheader("🎯 Büdcə")
    budget = tools.check_budget()
    if not budget.get("budgets_set"):
        st.caption("Limit təyin olunmayıb. Məs: «Aylıq büdcəm 300 manatdır»")
    else:
        for r in budget["results"]:
            pct = min(r["percent_used"] / 100, 1.0)
            label = f"{r['budget_az'].capitalize()}: {r['spent']:.2f} / {r['limit']:.2f} ₼ ({r['percent_used']}%)"
            st.progress(pct, text=label)
            if r["status"] == "exceeded":
                st.error(f"Limit {abs(r['remaining']):.2f} ₼ aşılıb!")
            elif r["status"] == "warning":
                st.warning(f"Limitə {r['remaining']:.2f} ₼ qalıb.")

    db = tools.load_db()

    st.subheader("🔔 Bildirişlər")
    notes = db["notifications"][-5:][::-1]
    if not notes:
        st.caption("Bildiriş yoxdur.")
    for n in notes:
        box = {"critical": st.error, "warning": st.warning}.get(n["level"], st.info)
        box(f"{n['message']}\n\n_{n['created_at'].replace('T', ' ')}_")

    st.subheader("🧾 Son xərclər")
    if db["expenses"]:
        df = pd.DataFrame(db["expenses"])
        df["ilkin"] = [
            f"{e['original_amount']:g} {e['original_currency']}" if e.get("original_currency") else ""
            for e in db["expenses"]
        ]
        df = df[["id", "date", "amount", "ilkin", "category", "description"]]
        df.columns = ["#", "Tarix", "Məbləğ ₼", "İlkin valyuta", "Kateqoriya", "Təsvir"]
        st.dataframe(df.sort_values("#", ascending=False), hide_index=True, width="stretch")
        if s_month["by_category"]:
            st.caption("Bu ay kateqoriyalar üzrə")
            st.bar_chart(pd.Series(s_month["by_category"], name="₼"))
    else:
        st.caption("Hələ xərc yoxdur.")

    st.divider()
    st.subheader("🤖 Model")
    options = [AUTO] + AVAILABLE
    labels = {AUTO: "🧭 Avtomatik (agent seçir)", **{k: MODELS[k]["label"] for k in MODELS}}
    if st.session_state.model_choice not in options:
        st.session_state.model_choice = AUTO
    busy = limited_models()
    st.radio(
        "Hansı model cavab versin?",
        options,
        format_func=lambda k: labels[k]
        + ("  💲" if k != AUTO and is_paid(k) else "")
        + ("  ⏳ limitdə" if k in busy else ""),
        key="model_choice",
        disabled=len(AVAILABLE) < 2,
    )
    if st.session_state.model_choice == AUTO:
        if len(AVAILABLE) >= 2:
            st.caption(f"Hər mesaj üçün `{router_model_name()}` router modeli Gemini və Groq arasında seçim edir.")
        if "openrouter" in AVAILABLE:
            st.caption("OpenRouter ehtiyat modeldir: digərlərinin limiti dolanda avtomatik işə düşür.")
        if "anthropic" in AVAILABLE:
            if paid_fallback_allowed():
                st.caption("💲 Claude Haiku pulsuz modellərin limiti dolanda ehtiyat kimi işləyir.")
            else:
                st.caption("💲 Claude Haiku Avtomatik rejimdə istifadə olunmur — yalnız özünüz seçəndə.")
    else:
        m = st.session_state.model_choice
        st.caption(f"`{model_name(m)}` — {MODELS[m]['strengths']}")
    if st.session_state.cost_usd > 0 or st.session_state.model_choice == "anthropic":
        cents = st.session_state.cost_usd * 100
        st.caption(f"💲 Bu sessiyada Claude xərci: **{cents:.3f} sent** (${st.session_state.cost_usd:.5f})")
    for k in MODELS:
        if problem := key_problem(k):
            st.warning(f"🔑 {problem}")
    missing = [MODELS[k]["key_env"] for k in MODELS if k not in AVAILABLE and not key_problem(k)]
    if missing and AVAILABLE:
        st.caption(f"ℹ️ İkinci model üçün `.env`-ə əlavə edin: {', '.join(missing)}")
    st.divider()
    col_a, col_b = st.columns(2)
    if col_a.button("🧹 Söhbəti təmizlə", width="stretch"):
        st.session_state.api_messages = []
        st.session_state.chat_log = []
        st.rerun()
    if col_b.button("🗑️ Məlumatları sıfırla", width="stretch"):
        tools.reset_db()
        st.rerun()

# --------------------------------------------------------------------------- #
# Əsas hissə: söhbət
# --------------------------------------------------------------------------- #
st.title("💰 Xərc Agenti")
st.caption("Xərclərinizi sərbəst mətnlə yazın — agent qeyd edir, hesabat verir və büdcəni izləyir.")

if not AVAILABLE:
    st.error(
        "API açarı tapılmadı. `.env.example` faylını `.env` adı ilə kopyalayıb ən azı bir pulsuz açar yazın:\n\n"
        "- `GEMINI_API_KEY` — https://aistudio.google.com/apikey\n"
        "- `GROQ_API_KEY` — https://console.groq.com/keys\n"
        "- `OPENROUTER_API_KEY` — https://openrouter.ai/settings/keys\n\n"
        "Streamlit Cloud-da isə Settings → Secrets."
    )
    st.stop()

if not st.session_state.chat_log:
    with st.chat_message("assistant"):
        st.markdown(
            "Salam! 👋 Mən sizin xərc köməkçinizəm. Nümunələr:\n"
            "- *Bu gün naharda 7 manat xərclədim*\n"
            "- *Aylıq büdcəm 300 manatdır*\n"
            "- *Bu həftə nə qədər xərcləmişəm?*\n"
            "- *Bayaq yazdığım xərci 5 manat et*"
        )

for msg in st.session_state.chat_log:
    with st.chat_message(msg["role"]):
        render_route(msg.get("route"), msg.get("fallback"))
        if msg.get("tool_calls"):
            render_tool_calls(msg["tool_calls"])
        st.markdown(msg["content"])

if prompt := st.chat_input("Mesajınızı yazın..."):
    st.session_state.chat_log.append({"role": "user", "content": prompt})
    st.session_state.api_messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        calls: list[dict] = []
        route_box = st.empty()
        live = st.empty()

        def on_event(kind: str, data: dict) -> None:
            if kind == "route":
                with route_box.container():
                    render_route(data)
            elif kind == "fallback":
                st.toast(f"🔁 {data['reason']} → {data['label']}", icon="🔁")

        def on_tool_call(name: str, args: dict, result: dict) -> None:
            calls.append({"name": name, "input": args, "result": result})
            with live.container():
                for c in calls:
                    st.info(f"🔧 **{c['name']}** çağırıldı")
            if name == "send_notification" and result.get("ok"):
                st.toast(result["notification"]["message"], icon="🔔")

        # Xəta olarsa tarixçəni pozmamaq üçün nüsxə üzərində işləyirik
        working = list(st.session_state.api_messages)
        route, fallback = None, None
        try:
            with st.spinner("Düşünürəm..."):
                result = ExpenseAgent().run(
                    working,
                    model_choice=st.session_state.model_choice,
                    on_tool_call=on_tool_call,
                    on_event=on_event,
                )
            st.session_state.api_messages = working
            answer = result["text"]
            route = {
                **result["route"],
                "label": MODELS[result["route"]["model"]]["label"],
                "cost_cents": result.get("usage", {}).get("cost_usd", 0.0) * 100,
            }
            fallback = result["fallback"]
            st.session_state.cost_usd += result.get("usage", {}).get("cost_usd", 0.0)
        except Exception as exc:
            st.session_state.api_messages.pop()  # uğursuz istifadəçi mesajını çıxar
            answer = f"⚠️ Xəta baş verdi: {exc}"

        live.empty()
        route_box.empty()
        render_route(route, fallback)
        render_tool_calls(calls)
        st.markdown(answer or "…")

    st.session_state.chat_log.append(
        {"role": "assistant", "content": answer, "tool_calls": calls, "route": route, "fallback": fallback}
    )
    st.rerun()  # yan paneli yeniləmək üçün
