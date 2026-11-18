"""
Xərc Agentinin alətləri (tools).

Hər alət adi Python funksiyasıdır. Məlumatlar sadə JSON faylında saxlanılır
(data/expenses.json). Agent (LLM) hansı aləti nə vaxt çağıracağına ÖZÜ qərar verir —
bu fayl yalnız alətlərin işini və onların sxemlərini (TOOLS) təsvir edir.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Baku"))
except Exception:  # Windows-da tzdata yoxdursa
    TZ = None

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = Path(os.getenv("DATA_FILE", BASE_DIR / "data" / "expenses.json"))

CATEGORIES = [
    "yemək",
    "nəqliyyat",
    "alış-veriş",
    "kommunal",
    "əyləncə",
    "sağlamlıq",
    "təhsil",
    "rabitə",
    "digər",
]

PERIODS = ["today", "yesterday", "week", "month", "all"]
PERIOD_NAMES_AZ = {
    "today": "bu gün",
    "yesterday": "dünən",
    "week": "bu həftə",
    "month": "bu ay",
    "all": "bütün dövr",
}
BUDGET_PERIODS = ["daily", "weekly", "monthly"]
BUDGET_TO_PERIOD = {"daily": "today", "weekly": "week", "monthly": "month"}
BUDGET_NAMES_AZ = {"daily": "günlük", "weekly": "həftəlik", "monthly": "aylıq"}

WARNING_THRESHOLD = float(os.getenv("BUDGET_WARNING_PERCENT", "80"))
MAX_SINGLE_AMOUNT = float(os.getenv("MAX_SINGLE_AMOUNT", "10000"))


# --------------------------------------------------------------------------- #
# Köməkçi funksiyalar
# --------------------------------------------------------------------------- #
def now() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()


def today() -> date:
    return now().date()


def _empty_db() -> dict:
    return {"next_id": 1, "expenses": [], "budgets": {}, "notifications": []}


def load_db() -> dict:
    if not DATA_FILE.exists():
        return _empty_db()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
        for key, val in _empty_db().items():
            db.setdefault(key, val)
        return db
    except (json.JSONDecodeError, OSError):
        return _empty_db()


def save_db(db: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_FILE)


def reset_db() -> None:
    save_db(_empty_db())


def _parse_date(value: str | None) -> date:
    if not value:
        return today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def period_range(period: str) -> tuple[date | None, date | None]:
    t = today()
    if period == "today":
        return t, t
    if period == "yesterday":
        y = t - timedelta(days=1)
        return y, y
    if period == "week":
        start = t - timedelta(days=t.weekday())  # Bazar ertəsi
        return start, t
    if period == "month":
        return t.replace(day=1), t
    return None, None  # all


def _in_period(exp: dict, period: str) -> bool:
    start, end = period_range(period)
    if start is None:
        return True
    d = _parse_date(exp["date"])
    return start <= d <= end


def _round(x: float) -> float:
    return round(float(x), 2)


def _find(db: dict, expense_id: int) -> dict | None:
    return next((e for e in db["expenses"] if e["id"] == int(expense_id)), None)


# --------------------------------------------------------------------------- #
# ALƏTLƏR
# --------------------------------------------------------------------------- #
def add_expense(
    amount: float,
    category: str,
    description: str = "",
    date: str | None = None,
    original_amount: float | None = None,
    original_currency: str | None = None,
) -> dict:
    """Yeni xərc qeyd edir."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Məbləğ rəqəm olmalıdır. İstifadəçidən dəqiq məbləği soruş."}

    # Guardrail (kod səviyyəsində): mənfi/sıfır və ya qeyri-real məbləğ qəbul edilmir
    if amount <= 0:
        return {"ok": False, "error": "Məbləğ 0-dan böyük olmalıdır. İstifadəçidən dəqiq məbləği soruş."}
    if amount > MAX_SINGLE_AMOUNT:
        return {
            "ok": False,
            "error": f"Məbləğ çox böyükdür ({amount} AZN). Qeyd etməzdən əvvəl istifadəçidən təsdiq istə.",
        }

    category = (category or "digər").strip().lower()
    if category not in CATEGORIES:
        category = "digər"

    try:
        exp_date = _parse_date(date)
    except ValueError:
        return {"ok": False, "error": "Tarix formatı YYYY-MM-DD olmalıdır."}
    if exp_date > today():
        return {"ok": False, "error": "Gələcək tarixə xərc yazmaq olmaz. İstifadəçidən tarixi dəqiqləşdir."}

    db = load_db()
    expense = {
        "id": db["next_id"],
        "amount": _round(amount),
        "currency": "AZN",
        "category": category,
        "description": description.strip(),
        "date": exp_date.isoformat(),
        "created_at": now().isoformat(timespec="seconds"),
    }
    if original_amount and original_currency and original_currency.upper() != "AZN":
        expense["original_amount"] = _round(original_amount)
        expense["original_currency"] = original_currency.upper()
    db["expenses"].append(expense)
    db["next_id"] += 1
    save_db(db)

    result: dict[str, Any] = {"ok": True, "expense": expense}
    if db["budgets"]:
        result["hint"] = "Büdcə limiti təyin olunub — check_budget çağırıb limiti yoxla."
    return result


def get_summary(period: str = "month", category: str | None = None) -> dict:
    """Seçilmiş dövr üzrə xərclərin cəmi və kateqoriyalar üzrə bölgüsü."""
    if period not in PERIODS:
        return {"ok": False, "error": f"Dövr bunlardan biri olmalıdır: {PERIODS}"}

    db = load_db()
    items = [e for e in db["expenses"] if _in_period(e, period)]
    if category:
        items = [e for e in items if e["category"] == category.lower()]

    by_cat: dict[str, float] = {}
    for e in items:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]

    start, end = period_range(period)
    total = sum(e["amount"] for e in items)
    return {
        "ok": True,
        "period": period,
        "period_az": PERIOD_NAMES_AZ[period],
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "category_filter": category,
        "total": _round(total),
        "currency": "AZN",
        "count": len(items),
        "by_category": {k: _round(v) for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])},
        "largest": max(items, key=lambda e: e["amount"]) if items else None,
    }


def list_expenses(period: str = "all", limit: int = 10) -> dict:
    """Son xərclərin siyahısı (id-ləri ilə). Redaktə/silmə üçün lazımdır."""
    if period not in PERIODS:
        period = "all"
    db = load_db()
    items = [e for e in db["expenses"] if _in_period(e, period)]
    items = sorted(items, key=lambda e: e["id"], reverse=True)[: max(1, min(int(limit), 50))]
    return {"ok": True, "period": period, "count": len(items), "expenses": items}


def update_expense(
    expense_id: int,
    amount: float | None = None,
    category: str | None = None,
    description: str | None = None,
    date: str | None = None,
) -> dict:
    """Mövcud xərci dəyişir."""
    db = load_db()
    exp = _find(db, expense_id)
    if not exp:
        return {"ok": False, "error": f"#{expense_id} nömrəli xərc tapılmadı. list_expenses ilə yoxla."}

    old = dict(exp)
    if amount is not None:
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Məbləğ rəqəm olmalıdır."}
        if amount <= 0 or amount > MAX_SINGLE_AMOUNT:
            return {"ok": False, "error": "Məbləğ etibarsızdır."}
        exp["amount"] = _round(amount)
    if category is not None:
        c = category.strip().lower()
        exp["category"] = c if c in CATEGORIES else "digər"
    if description is not None:
        exp["description"] = description.strip()
    if date is not None:
        try:
            d = _parse_date(date)
        except ValueError:
            return {"ok": False, "error": "Tarix formatı YYYY-MM-DD olmalıdır."}
        if d > today():
            return {"ok": False, "error": "Gələcək tarix olmaz."}
        exp["date"] = d.isoformat()

    save_db(db)
    return {"ok": True, "before": old, "after": exp}


def delete_expense(expense_id: int) -> dict:
    """Xərci silir."""
    db = load_db()
    exp = _find(db, expense_id)
    if not exp:
        return {"ok": False, "error": f"#{expense_id} nömrəli xərc tapılmadı."}
    db["expenses"] = [e for e in db["expenses"] if e["id"] != exp["id"]]
    save_db(db)
    return {"ok": True, "deleted": exp}


def set_budget(period: str, amount: float) -> dict:
    """Günlük / həftəlik / aylıq büdcə limiti təyin edir (amount=0 limiti silir)."""
    if period not in BUDGET_PERIODS:
        return {"ok": False, "error": f"Dövr bunlardan biri olmalıdır: {BUDGET_PERIODS}"}
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Limit rəqəm olmalıdır."}
    if amount < 0:
        return {"ok": False, "error": "Limit mənfi ola bilməz."}

    db = load_db()
    if amount == 0:
        db["budgets"].pop(period, None)
        save_db(db)
        return {"ok": True, "removed": period}
    db["budgets"][period] = _round(amount)
    save_db(db)
    return {"ok": True, "budgets": db["budgets"]}


def check_budget() -> dict:
    """Bütün təyin olunmuş limitləri cari xərclərlə müqayisə edir."""
    db = load_db()
    if not db["budgets"]:
        return {"ok": True, "budgets_set": False, "message": "Heç bir büdcə limiti təyin olunmayıb."}

    results = []
    for bperiod, limit in db["budgets"].items():
        p = BUDGET_TO_PERIOD[bperiod]
        spent = sum(e["amount"] for e in db["expenses"] if _in_period(e, p))
        pct = (spent / limit * 100) if limit else 0
        if pct >= 100:
            status = "exceeded"
        elif pct >= WARNING_THRESHOLD:
            status = "warning"
        else:
            status = "ok"
        results.append(
            {
                "budget": bperiod,
                "budget_az": BUDGET_NAMES_AZ[bperiod],
                "limit": limit,
                "spent": _round(spent),
                "remaining": _round(limit - spent),
                "percent_used": round(pct, 1),
                "status": status,
            }
        )
    needs = any(r["status"] != "ok" for r in results)
    return {
        "ok": True,
        "budgets_set": True,
        "warning_threshold_percent": WARNING_THRESHOLD,
        "results": results,
        "action_needed": "send_notification çağır" if needs else None,
    }


# --------------------------------------------------------------------------- #
# Valyuta çevirmə
# --------------------------------------------------------------------------- #
CURRENCY_ALIASES = {
    "manat": "AZN", "₼": "AZN", "azn": "AZN",
    "dollar": "USD", "dolar": "USD", "$": "USD", "usd": "USD",
    "avro": "EUR", "yevro": "EUR", "euro": "EUR", "€": "EUR", "eur": "EUR",
    "rubl": "RUB", "rub": "RUB", "₽": "RUB",
    "lirə": "TRY", "lira": "TRY", "try": "TRY", "₺": "TRY",
    "funt": "GBP", "sterlinq": "GBP", "£": "GBP", "gbp": "GBP",
    "lari": "GEL", "gel": "GEL",
    "dirhəm": "AED", "dirhem": "AED", "aed": "AED",
}
_rates_cache: dict[str, dict] = {}  # {"2026-11-18": {"rates": {...}, "date": ..., "source": ...}}
RATES_CACHE_FILE = DATA_FILE.parent / "rates_cache.json"


class CurrencyApiError(Exception):
    pass


def _currencylayer_key() -> str | None:
    k = (os.getenv("CURRENCYLAYER_API_KEY") or "").strip().strip('"').strip("'")
    if not k or not k.isascii() or " " in k or "your" in k.lower():
        return None
    return k


def _fetch_currencylayer(key: str) -> dict:
    """
    currencylayer.com /live. Pulsuz plan: yalnız USD bazası, ayda 100 sorğu.
    Cavab: {"success": true, "timestamp": ..., "source": "USD", "quotes": {"USDAZN": 1.7, "USDEUR": 0.86, ...}}
    HTTPS əvvəl yoxlanılır; pulsuz plan HTTPS-ə icazə vermirsə (xəta 105), HTTP ilə təkrarlanır.
    """
    last_error = None
    for base in ("https://api.currencylayer.com/live", "http://api.currencylayer.com/live"):
        url = f"{base}?{urllib.parse.urlencode({'access_key': key})}"
        try:
            data = json.loads(_http_get(url))
        except Exception as exc:
            last_error = f"bağlantı xətası: {exc}"
            continue
        if data.get("success"):
            quotes = data.get("quotes") or {}
            usd_azn = quotes.get("USDAZN")
            if not usd_azn:
                raise CurrencyApiError("cavabda USDAZN yoxdur")
            # 1 X = (USDAZN / USDX) AZN
            rates = {q[3:]: usd_azn / v for q, v in quotes.items() if q.startswith("USD") and v}
            rates["USD"], rates["AZN"] = usd_azn, 1.0
            ts = data.get("timestamp")
            when = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M") if ts else today().isoformat()
            return {"rates": rates, "date": when, "source": "currencylayer.com"}
        err = data.get("error") or {}
        code = err.get("code")
        last_error = f"xəta {code}: {err.get('info') or err.get('type')}"
        if code == 105 and base.startswith("https"):
            continue  # pulsuz plan: HTTPS yoxdur → HTTP ilə yoxla
        raise CurrencyApiError(last_error)  # 101 səhv açar, 104 aylıq limit doldu və s.
    raise CurrencyApiError(last_error or "naməlum xəta")


def _load_disk_cache() -> dict | None:
    try:
        with open(RATES_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_disk_cache(day: str, info: dict) -> None:
    try:
        RATES_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RATES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"day": day, **info}, f, ensure_ascii=False)
    except OSError:
        pass


def _normalize_currency(code: str) -> str:
    c = (code or "").strip()
    return CURRENCY_ALIASES.get(c.lower(), c.upper())


def _http_get(url: str, timeout: int = 8) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "xerc-agenti/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_cbar_xml(raw: bytes) -> dict[str, float]:
    """CBAR XML-i: <Valute Code="USD"><Nominal>1</Nominal><Value>1.7</Value></Valute> → {USD: 1.7} (1 vahid neçə AZN)."""
    root = ET.fromstring(raw)
    rates = {"AZN": 1.0}
    for v in root.iter("Valute"):
        code = (v.get("Code") or "").upper()
        try:
            nominal = float((v.findtext("Nominal") or "1").replace(",", ".").split()[0])
            value = float((v.findtext("Value") or "").replace(",", "."))
        except ValueError:
            continue
        if code and nominal > 0 and value > 0:
            rates[code] = value / nominal
    return rates


def get_rates() -> dict:
    """
    Günün məzənnələri (1 vahid = neçə AZN). Mənbələr, sıra ilə:
      1. currencylayer.com — .env-də CURRENCYLAYER_API_KEY varsa
      2. Azərbaycan Mərkəzi Bankı (CBAR) — rəsmi məzənnə; həftəsonu üçün əvvəlki iş günü
      3. open.er-api.com — digərləri əlçatan olmasa
    Gün ərzində bir dəfə yüklənir və diskdə saxlanılır (currencylayer-in aylıq 100 sorğusuna qənaət).
    """
    key = today().isoformat()
    if key in _rates_cache:
        return _rates_cache[key]
    disk = _load_disk_cache()
    if disk and disk.get("day") == key and disk.get("rates"):
        _rates_cache[key] = {k: disk[k] for k in ("rates", "date", "source")}
        return _rates_cache[key]

    def remember(info: dict) -> dict:
        _rates_cache[key] = info
        _save_disk_cache(key, info)
        return info

    cl_key = _currencylayer_key()
    if cl_key:
        try:
            return remember(_fetch_currencylayer(cl_key))
        except CurrencyApiError:
            pass  # ehtiyat mənbələrə keç

    for back in range(0, 5):
        d = today() - timedelta(days=back)
        try:
            rates = _parse_cbar_xml(_http_get(f"https://www.cbar.az/currencies/{d.strftime('%d.%m.%Y')}.xml"))
            if len(rates) > 5:
                return remember({"rates": rates, "date": d.isoformat(), "source": "Azərbaycan Mərkəzi Bankı (CBAR)"})
        except Exception:
            continue

    try:
        data = json.loads(_http_get("https://open.er-api.com/v6/latest/AZN"))
        if data.get("result") == "success":
            rates = {c: 1 / r for c, r in data["rates"].items() if r}
            rates["AZN"] = 1.0
            return remember(
                {"rates": rates, "date": (data.get("time_last_update_utc") or key)[:16], "source": "open.er-api.com"}
            )
    except Exception:
        pass
    raise RuntimeError("Məzənnə mənbələri əlçatan deyil")


def convert_currency(amount: float, from_currency: str, to_currency: str = "AZN") -> dict:
    """Məbləği bir valyutadan digərinə çevirir (default: manata)."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Məbləğ rəqəm olmalıdır."}
    if amount <= 0:
        return {"ok": False, "error": "Məbləğ 0-dan böyük olmalıdır."}

    src, dst = _normalize_currency(from_currency), _normalize_currency(to_currency)
    if src == dst:
        return {"ok": True, "amount": amount, "from": src, "to": dst, "result": _round(amount), "rate": 1.0}

    try:
        info = get_rates()
    except RuntimeError:
        return {
            "ok": False,
            "error": "Məzənnəni hazırda almaq mümkün olmadı. Məzənnə UYDURMA — istifadəçidən məbləği manatla soruş.",
        }

    rates = info["rates"]
    missing = [c for c in (src, dst) if c not in rates]
    if missing:
        return {"ok": False, "error": f"Naməlum valyuta: {', '.join(missing)}. ISO kodu yaz (məs. USD, EUR, TRY)."}

    rate = rates[src] / rates[dst]  # 1 src = rate dst
    return {
        "ok": True,
        "amount": _round(amount),
        "from": src,
        "to": dst,
        "rate": round(rate, 4),
        "result": _round(amount * rate),
        "rate_date": info["date"],
        "source": info["source"],
    }


def send_notification(message: str, level: str = "warning") -> dict:
    """İstifadəçiyə bildiriş göndərir (tətbiqdə göstərilir, istəyə görə Telegram-a da)."""
    if level not in ("info", "warning", "critical"):
        level = "warning"
    db = load_db()
    note = {
        "id": len(db["notifications"]) + 1,
        "message": message.strip(),
        "level": level,
        "created_at": now().isoformat(timespec="seconds"),
        "seen": False,
    }
    db["notifications"].append(note)
    save_db(db)

    channels = ["app"]
    tg = _send_telegram(f"⚠️ Xərc Agenti: {message}")
    if tg is True:
        channels.append("telegram")
    result = {"ok": True, "notification": note, "channels": channels}
    if isinstance(tg, str):
        result["telegram_error"] = tg
    return result


def _send_telegram(text: str):
    """TELEGRAM_BOT_TOKEN və TELEGRAM_CHAT_ID .env-də varsa Telegram-a göndərir."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status == 200
    except Exception as exc:  # bildiriş xətası agenti dayandırmamalıdır
        return f"Telegram xətası: {exc}"


# --------------------------------------------------------------------------- #
# Alətlərin icrası və sxemləri
# --------------------------------------------------------------------------- #
TOOL_FUNCTIONS = {
    "add_expense": add_expense,
    "get_summary": get_summary,
    "list_expenses": list_expenses,
    "update_expense": update_expense,
    "delete_expense": delete_expense,
    "set_budget": set_budget,
    "check_budget": check_budget,
    "send_notification": send_notification,
    "convert_currency": convert_currency,
}


def execute_tool(name: str, args: dict) -> dict:
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"ok": False, "error": f"Naməlum alət: {name}"}
    try:
        return fn(**(args or {}))
    except TypeError as exc:
        return {"ok": False, "error": f"Yanlış parametrlər: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"Alət xətası: {exc}"}


TOOLS = [
    {
        "name": "add_expense",
        "description": (
            "Yeni xərc qeyd edir. YALNIZ istifadəçi dəqiq məbləğ dedikdə çağır. "
            "Məbləğ aydın deyilsə ('bir az', 'xeyli', 'bilmirəm') bu aləti ÇAĞIRMA, əvvəl soruş."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Məbləğ manatla (AZN), məs. 7 və ya 4.5"},
                "category": {"type": "string", "enum": CATEGORIES, "description": "Xərcin kateqoriyası"},
                "description": {"type": "string", "description": "Qısa təsvir, məs. 'nahar', 'taksi'"},
                "date": {
                    "type": "string",
                    "description": "Tarix YYYY-MM-DD formatında. Göstərilməyibsə bu gün götürülür.",
                },
                "original_amount": {
                    "type": "number",
                    "description": "Xərc başqa valyutada idisə, ilkin məbləğ (convert_currency-dən sonra)",
                },
                "original_currency": {"type": "string", "description": "İlkin valyutanın kodu, məs. USD"},
            },
            "required": ["amount", "category"],
        },
    },
    {
        "name": "get_summary",
        "description": "Seçilmiş dövr üzrə ümumi xərci, sayını və kateqoriyalar üzrə bölgünü qaytarır.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": PERIODS,
                    "description": "today=bu gün, yesterday=dünən, week=bu həftə, month=bu ay, all=hamısı",
                },
                "category": {"type": "string", "enum": CATEGORIES, "description": "İstəyə görə kateqoriya filtri"},
            },
            "required": ["period"],
        },
    },
    {
        "name": "list_expenses",
        "description": (
            "Son xərcləri id-ləri ilə qaytarır. Xərci redaktə etmək və ya silmək lazım olduqda, "
            "id söhbətdən məlum deyilsə, əvvəlcə bunu çağır."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": PERIODS},
                "limit": {"type": "integer", "description": "Neçə qeyd (1-50), default 10"},
            },
        },
    },
    {
        "name": "update_expense",
        "description": "Mövcud xərcin məbləğini, kateqoriyasını, təsvirini və ya tarixini dəyişir.",
        "input_schema": {
            "type": "object",
            "properties": {
                "expense_id": {"type": "integer"},
                "amount": {"type": "number"},
                "category": {"type": "string", "enum": CATEGORIES},
                "description": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["expense_id"],
        },
    },
    {
        "name": "delete_expense",
        "description": "Xərci id-yə görə silir.",
        "input_schema": {
            "type": "object",
            "properties": {"expense_id": {"type": "integer"}},
            "required": ["expense_id"],
        },
    },
    {
        "name": "set_budget",
        "description": "Günlük, həftəlik və ya aylıq büdcə limiti təyin edir. amount=0 limiti silir.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": BUDGET_PERIODS},
                "amount": {"type": "number", "description": "Limit manatla"},
            },
            "required": ["period", "amount"],
        },
    },
    {
        "name": "check_budget",
        "description": (
            "Təyin olunmuş büdcə limitlərini cari xərclərlə müqayisə edir və statusu qaytarır "
            "(ok / warning / exceeded). Hər yeni xərcdən sonra limit varsa çağır."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_notification",
        "description": (
            "İstifadəçiyə xəbərdarlıq bildirişi göndərir. check_budget statusu 'warning' və ya "
            "'exceeded' olduqda çağır."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Azərbaycan dilində qısa bildiriş mətni"},
                "level": {"type": "string", "enum": ["info", "warning", "critical"]},
            },
            "required": ["message"],
        },
    },
    {
        "name": "convert_currency",
        "description": (
            "Məbləği bir valyutadan digərinə real məzənnə ilə çevirir (currencylayer / Azərbaycan Mərkəzi Bankı). "
            "İstifadəçi xərci manatdan başqa valyutada deyəndə (dollar, avro, lirə, rubl...) add_expense-dən "
            "ƏVVƏL çağır, sonra nəticəni (AZN) add_expense-ə ver. 'Neçə manatdır?' suallarında da istifadə et."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Çevriləcək məbləğ"},
                "from_currency": {"type": "string", "description": "ISO kodu: USD, EUR, TRY, RUB, GBP, GEL..."},
                "to_currency": {"type": "string", "description": "ISO kodu, default AZN"},
            },
            "required": ["amount", "from_currency"],
        },
    },
]
