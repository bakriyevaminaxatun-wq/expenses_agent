"""
API açarı olmadan alətləri və agent dövrünü yoxlayan testlər.
İşə salmaq:  python -m pytest tests   (və ya: python tests/test_tools.py)
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_FILE"] = str(Path(tempfile.mkdtemp()) / "test.json")

import tools  # noqa: E402
import agent as agent_mod  # noqa: E402

agent_mod.time.sleep = lambda *_: None  # testlərdə gözləmə olmasın
from agent import ExpenseAgent  # noqa: E402


def setup_function(_=None):
    tools.reset_db()


def test_add_and_summary():
    setup_function()
    r = tools.add_expense(7, "yemək", "nahar")
    assert r["ok"] and r["expense"]["id"] == 1
    tools.add_expense(4, "nəqliyyat", "taksi")
    s = tools.get_summary("today")
    assert s["total"] == 11 and s["count"] == 2
    assert s["by_category"] == {"yemək": 7, "nəqliyyat": 4}


def test_guardrails_on_amount():
    setup_function()
    assert not tools.add_expense(0, "yemək")["ok"]
    assert not tools.add_expense(-5, "yemək")["ok"]
    assert not tools.add_expense("bir az", "yemək")["ok"]
    assert not tools.add_expense(999999, "yemək")["ok"]
    assert not tools.add_expense(5, "yemək", date="2999-01-01")["ok"]
    assert tools.get_summary("all")["count"] == 0


def test_unknown_category_becomes_other():
    setup_function()
    assert tools.add_expense(3, "kosmos")["expense"]["category"] == "digər"


def test_update_and_delete():
    setup_function()
    tools.add_expense(4, "nəqliyyat", "taksi")
    r = tools.update_expense(1, amount=5)
    assert r["ok"] and r["before"]["amount"] == 4 and r["after"]["amount"] == 5
    assert not tools.update_expense(99, amount=1)["ok"]
    assert tools.delete_expense(1)["ok"]
    assert tools.get_summary("all")["count"] == 0


def test_budget_and_notification():
    setup_function()
    assert tools.check_budget()["budgets_set"] is False
    tools.set_budget("daily", 10)
    tools.add_expense(5, "yemək")
    assert tools.check_budget()["results"][0]["status"] == "ok"
    tools.add_expense(3.5, "yemək")  # 85%
    assert tools.check_budget()["results"][0]["status"] == "warning"
    tools.add_expense(5, "yemək")  # 135%
    b = tools.check_budget()
    assert b["results"][0]["status"] == "exceeded" and b["action_needed"]
    n = tools.send_notification("Günlük limit aşıldı", "critical")
    assert n["ok"] and tools.load_db()["notifications"][0]["level"] == "critical"


def test_execute_tool_handles_bad_input():
    assert not tools.execute_tool("nope", {})["ok"]
    assert not tools.execute_tool("add_expense", {"foo": 1})["ok"]


# --- Agent dövrü: saxta (fake) model ilə ---------------------------------- #
from openai.types.chat import ChatCompletionMessage  # noqa: E402


def _tool_msg(id_, name, args):
    return ChatCompletionMessage.model_validate(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": id_,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                    "extra_content": {"google": {"thought_signature": "sig-" + id_}},
                }
            ],
        }
    )


def _text_msg(t):
    return ChatCompletionMessage.model_validate({"role": "assistant", "content": t})


class FakeClient:
    """Əvvəlcədən yazılmış cavabları qaytarır. Exception obyekti versən, onu atır."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(message=item)])


def _router_reply(model, reason="test"):
    return _text_msg(json.dumps({"model": model, "reason": reason}, ensure_ascii=False))


def _both_keys():
    import models

    os.environ["GEMINI_API_KEY"] = "g"
    os.environ["GROQ_API_KEY"] = "q"
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("ANTHROPIC_AS_FALLBACK", None)
    models.clear_cooldowns()


def _rate_limit():
    from openai import RateLimitError

    err = RateLimitError.__new__(RateLimitError)  # HTTP cavabı olmadan 429 xətası
    Exception.__init__(err, "limit")
    return err


def test_agent_loop_two_tools_in_sequence():
    setup_function()
    _both_keys()
    gemini = FakeClient(
        [
            _tool_msg("t1", "add_expense", {"amount": 4, "category": "nəqliyyat"}),
            _tool_msg("t2", "get_summary", {"period": "week"}),
            _text_msg("Qeyd etdim. Bu həftə 4 ₼ xərcləmisiniz."),
        ]
    )
    router = FakeClient([_router_reply("gemini", "iki addımlı tapşırıq")])
    seen, events = [], []
    agent = ExpenseAgent(clients={"gemini": gemini}, router_client=router)
    history = [{"role": "user", "content": "Taksiyə 4 manat verdim, bu həftə nə qədər xərclədim?"}]
    out = agent.run(history, on_tool_call=lambda n, i, r: seen.append(n), on_event=lambda k, d: events.append(k))

    assert out["model"] == "gemini" and out["route"]["by"] == "router"
    assert events == ["route"]
    assert seen == ["add_expense", "get_summary"]
    assert "4" in out["text"]
    assert tools.get_summary("week")["total"] == 4
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert history[1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig-t1"
    assert fake_first(gemini)["messages"][0]["role"] == "system"
    # daxili `_model` sahəsi API-yə getmir
    assert all("_model" not in m for m in gemini.calls[-1]["messages"])
    cb = next(t for t in gemini.calls[0]["tools"] if t["function"]["name"] == "check_budget")
    assert "parameters" not in cb["function"]


def fake_first(client):
    return client.calls[0]


def test_router_llm_picks_groq_for_simple():
    _both_keys()
    from router import choose_model

    r = choose_model("Taksiyə 4 manat verdim", [], router_client=FakeClient([_router_reply("groq", "sadə")]))
    assert r == {"model": "groq", "reason": "sadə", "by": "router"}


def test_router_falls_back_to_rules_when_router_fails():
    _both_keys()
    from router import choose_model

    broken = FakeClient([RuntimeError("down")])
    assert choose_model("Bayaq yazdığımı sil", [], router_client=broken)["model"] == "gemini"
    broken = FakeClient([RuntimeError("down")])
    assert choose_model("Kofeyə 3 manat", [], router_client=broken)["model"] == "groq"
    bad_json = FakeClient([_text_msg("bilmirəm")])
    assert choose_model("2 manat kofe, 5 manat taksi", [], router_client=bad_json)["by"] == "rule"


def test_user_choice_skips_router():
    _both_keys()
    from router import choose_model

    router = FakeClient([])  # çağırılsa, pop() xəta verərdi
    assert choose_model("salam", [], user_choice="groq", router_client=router)["by"] == "user"
    assert router.calls == []


def test_only_one_key():
    os.environ["GEMINI_API_KEY"] = "g"
    os.environ.pop("GROQ_API_KEY", None)
    from router import choose_model

    assert choose_model("salam", [], user_choice="groq")["model"] == "gemini"
    _both_keys()


def test_fallback_to_other_model_on_rate_limit():
    setup_function()
    _both_keys()
    groq = FakeClient([_rate_limit(), _rate_limit()])
    gemini = FakeClient([_text_msg("Salam! Nə qeyd edək?")])
    events = []
    agent = ExpenseAgent(clients={"groq": groq, "gemini": gemini})
    history = [{"role": "user", "content": "salam"}]
    out = agent.run(history, model_choice="groq", on_event=lambda k, d: events.append(k))
    assert out["model"] == "gemini" and out["fallback"]
    assert events == ["route", "fallback"]


def test_history_is_cleaned_per_model():
    from agent import GEMINI_DUMMY_SIGNATURE, prepare_messages

    hist = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "",
            "_model": "groq",
            "tool_calls": [{"id": "a", "type": "function", "function": {"name": "check_budget", "arguments": "{}"}}],
        },
        {
            "role": "assistant",
            "content": "",
            "_model": "gemini",
            "tool_calls": [
                {
                    "id": "b",
                    "type": "function",
                    "function": {"name": "check_budget", "arguments": "{}"},
                    "extra_content": {"google": {"thought_signature": "real"}},
                }
            ],
        },
    ]
    g = prepare_messages(hist, "gemini")
    assert g[1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == GEMINI_DUMMY_SIGNATURE
    assert g[2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "real"
    q = prepare_messages(hist, "groq")
    assert "extra_content" not in q[2]["tool_calls"][0] and "_model" not in q[1]
    assert "_model" in hist[1]  # orijinal tarixçə dəyişmir


def test_openrouter_three_models_fallback_chain_and_cooldown():
    setup_function()
    _both_keys()
    os.environ["OPENROUTER_API_KEY"] = "o"
    import models
    from router import choose_model

    groq = FakeClient([_rate_limit(), _rate_limit()])
    gemini = FakeClient([_rate_limit(), _rate_limit()])
    orouter = FakeClient([_text_msg("Salam!")])
    agent = ExpenseAgent(clients={"groq": groq, "gemini": gemini, "openrouter": orouter})
    out = agent.run([{"role": "user", "content": "salam"}], model_choice="groq")
    assert out["model"] == "openrouter"
    assert orouter.calls[0]["model"] == "openrouter/free"
    # groq və gemini indi "cooldown"dadır — növbəti mesajda birbaşa OpenRouter seçilir, router çağırılmır
    assert models.limited_models() == {"groq", "gemini"}
    r = choose_model("salam", [], router_client=FakeClient([]))
    assert r["model"] == "openrouter" and r["reason"] == "digər modellər limitdədir"
    # istifadəçi limitdə olan modeli seçibsə — müvəqqəti əvəz
    r = choose_model("salam", [], user_choice="groq", router_client=FakeClient([]))
    assert r["model"] == "openrouter" and "limitdədir" in r["reason"]
    os.environ.pop("OPENROUTER_API_KEY")
    models.clear_cooldowns()


def test_trim_history_keeps_tool_pairs():
    from agent import trim_history

    hist = []
    for i in range(10):
        hist += [
            {"role": "user", "content": f"xərc {i}"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": str(i)}]},
            {"role": "tool", "tool_call_id": str(i), "content": "{}"},
            {"role": "assistant", "content": "ok"},
        ]
    t = trim_history(hist, 10)
    assert t[0]["role"] == "user" and len(t) <= 10 and t[-1] is hist[-1]
    # cari tapşırıq çox uzundursa belə, cari istifadəçi mesajı saxlanılır
    long_turn = [{"role": "user", "content": "uzun"}] + [{"role": "tool", "content": "{}"}] * 30
    assert trim_history(hist + long_turn, 10)[0]["content"] == "uzun"
    assert trim_history(hist[:4], 10) == hist[:4]


def test_bad_keys_are_detected():
    import models

    os.environ["OPENROUTER_API_KEY"] = "sizin-açar"
    assert models.api_key("openrouter") is None and "nümunə" in models.key_problem("openrouter")
    os.environ["OPENROUTER_API_KEY"] = "your-openrouter-key"
    assert models.api_key("openrouter") is None
    os.environ["OPENROUTER_API_KEY"] = '"sk-or-v1-abc"'
    assert models.api_key("openrouter") == "sk-or-v1-abc" and models.key_problem("openrouter") is None
    os.environ["OPENROUTER_API_KEY"] = "gsk_wrongplace"
    assert models.api_key("openrouter") and "sk-or-" in models.key_problem("openrouter")
    os.environ.pop("OPENROUTER_API_KEY")


def test_auth_error_falls_back():
    setup_function()
    _both_keys()
    from openai import AuthenticationError
    import models

    err = AuthenticationError.__new__(AuthenticationError)
    Exception.__init__(err, "Missing Authentication header")
    groq = FakeClient([err])
    gemini = FakeClient([_text_msg("Salam!")])
    out = ExpenseAgent(clients={"groq": groq, "gemini": gemini}).run(
        [{"role": "user", "content": "salam"}], model_choice="groq"
    )
    assert out["model"] == "gemini" and "açarı qəbul olunmadı" in out["fallback"]
    assert len(groq.calls) == 1 and "groq" in models.limited_models()
    models.clear_cooldowns()


SAMPLE_CBAR = """<?xml version="1.0" encoding="UTF-8"?>
<ValCurs Date="18.11.2026" Name="AZN məzənnələri">
  <ValType Type="Bank metalları"><Valute Code="XAU"><Nominal>1 t.u.</Nominal><Name>Qızıl</Name><Value>7000.5</Value></Valute></ValType>
  <ValType Type="Xarici valyutalar">
    <Valute Code="USD"><Nominal>1</Nominal><Name>1 ABŞ dolları</Name><Value>1.7</Value></Valute>
    <Valute Code="EUR"><Nominal>1</Nominal><Name>1 Avro</Name><Value>1.9</Value></Valute>
    <Valute Code="TRY"><Nominal>1</Nominal><Name>1 Türk lirəsi</Name><Value>0.04</Value></Valute>
    <Valute Code="RUB"><Nominal>100</Nominal><Name>100 Rus rublu</Name><Value>2.1</Value></Valute>
    <Valute Code="GEL"><Nominal>1</Nominal><Name>1 Gürcü larisi</Name><Value>0.62</Value></Valute>
  </ValType>
</ValCurs>""".encode()


def _mock_rates(fail_cbar=False, fail_all=False):
    tools._rates_cache.clear()
    tools.RATES_CACHE_FILE.unlink(missing_ok=True)
    os.environ.pop("CURRENCYLAYER_API_KEY", None)
    calls = []

    def fake_get(url, timeout=8):
        calls.append(url)
        if fail_all or (fail_cbar and "cbar" in url):
            raise OSError("offline")
        if "cbar" in url:
            return SAMPLE_CBAR
        return json.dumps({"result": "success", "time_last_update_utc": "Wed, 18 Nov 2026", "rates": {"AZN": 1, "USD": 0.5}}).encode()

    tools._http_get = fake_get
    return calls


def test_convert_currency_cbar():
    calls = _mock_rates()
    r = tools.convert_currency(20, "dollar")
    assert r["ok"] and r["from"] == "USD" and r["to"] == "AZN" and r["result"] == 34.0
    assert "CBAR" in r["source"]
    assert tools.convert_currency(1000, "RUB")["result"] == 21.0  # nominal 100
    assert tools.convert_currency(17, "AZN", "USD")["result"] == 10.0
    assert tools.convert_currency(10, "EUR", "USD")["rate"] == round(1.9 / 1.7, 4)
    assert len(calls) == 1  # gün ərzində keşdən
    assert not tools.convert_currency(5, "XYZ")["ok"]
    assert not tools.convert_currency(-5, "USD")["ok"]


def test_convert_currency_fallbacks():
    calls = _mock_rates(fail_cbar=True)
    r = tools.convert_currency(10, "USD")
    assert r["ok"] and r["result"] == 20.0 and r["source"] == "open.er-api.com"
    assert sum("cbar" in c for c in calls) == 5  # bu gün + 4 əvvəlki gün yoxlanıldı
    _mock_rates(fail_all=True)
    r = tools.convert_currency(10, "USD")
    assert not r["ok"] and "UYDURMA" in r["error"]


def test_add_expense_keeps_original_currency():
    setup_function()
    r = tools.add_expense(34, "əyləncə", "kino", original_amount=20, original_currency="usd")
    assert r["expense"]["original_amount"] == 20 and r["expense"]["original_currency"] == "USD"
    assert "original_currency" not in tools.add_expense(5, "yemək")["expense"]


def _mock_currencylayer(responses):
    """responses: URL-də 'https' / 'http' üçün cavablar siyahısı (sıra ilə)."""
    tools._rates_cache.clear()
    tools.RATES_CACHE_FILE.unlink(missing_ok=True)
    os.environ["CURRENCYLAYER_API_KEY"] = "abc123"
    calls = []

    def fake_get(url, timeout=8):
        calls.append(url)
        if "currencylayer" in url:
            return json.dumps(responses.pop(0)).encode()
        if "cbar" in url:
            return SAMPLE_CBAR
        raise OSError("offline")

    tools._http_get = fake_get
    return calls


CL_OK = {"success": True, "timestamp": 1795000000, "source": "USD",
         "quotes": {"USDAZN": 1.7, "USDEUR": 0.85, "USDTRY": 42.5, "USDUSD": 1}}


def test_currencylayer_primary_source():
    calls = _mock_currencylayer([CL_OK])
    r = tools.convert_currency(20, "USD")
    assert r["ok"] and r["result"] == 34.0 and r["source"] == "currencylayer.com"
    assert tools.convert_currency(10, "EUR")["result"] == 20.0  # 1.7 / 0.85 = 2
    assert tools.convert_currency(850, "TRY", "EUR")["result"] == 17.0
    assert len(calls) == 1 and calls[0].startswith("https://") and "access_key=abc123" in calls[0]
    # tətbiq yenidən başladı (yaddaş keşi silindi) — diskdəki keşdən oxunur, yeni sorğu yoxdur
    tools._rates_cache.clear()
    assert tools.convert_currency(1, "USD")["source"] == "currencylayer.com" and len(calls) == 1


def test_currencylayer_https_restricted_retries_http():
    calls = _mock_currencylayer([
        {"success": False, "error": {"code": 105, "type": "https_access_restricted"}},
        CL_OK,
    ])
    assert tools.convert_currency(1, "USD")["source"] == "currencylayer.com"
    assert calls[0].startswith("https://") and calls[1].startswith("http://")


def test_currencylayer_limit_falls_back_to_cbar():
    calls = _mock_currencylayer([{"success": False, "error": {"code": 104, "info": "monthly limit reached"}}])
    r = tools.convert_currency(20, "USD")
    assert r["ok"] and "CBAR" in r["source"] and r["result"] == 34.0
    assert sum("currencylayer" in c for c in calls) == 1  # 104-də HTTP ilə təkrar yoxdur
    os.environ.pop("CURRENCYLAYER_API_KEY")


class UsageClient(FakeClient):
    """Cavablara token istifadəsi (usage) əlavə edir."""

    def create(self, **kwargs):
        resp = super().create(**kwargs)
        resp.usage = SimpleNamespace(prompt_tokens=4000, completion_tokens=200)
        return resp


def test_anthropic_mode_user_choice_cost_and_no_temperature():
    setup_function()
    _both_keys()
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    claude = UsageClient([
        _tool_msg("c1", "add_expense", {"amount": 7, "category": "yemək"}),
        _text_msg("Qeyd etdim: 7 ₼, yemək."),
    ])
    out = ExpenseAgent(clients={"anthropic": claude}).run(
        [{"role": "user", "content": "Naharda 7 manat"}], model_choice="anthropic"
    )
    assert out["model"] == "anthropic" and out["route"]["by"] == "user"
    assert claude.calls[0]["model"] == "claude-haiku-5-5"
    assert "temperature" not in claude.calls[0]  # Claude 4.7+ üçün temperature göndərilmir
    # 2 sorğu × (4000 × $0.10 + 200 × $0.50) / 1M = $0.001
    assert abs(out["usage"]["cost_usd"] - 0.001) < 1e-9
    assert out["usage"]["prompt_tokens"] == 8000
    os.environ.pop("ANTHROPIC_API_KEY")


def test_auto_mode_never_picks_paid_model():
    _both_keys()
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    from router import choose_model
    import models

    for msg in ("salam", "Bayaq yazdığımı sil, taksini 6 et"):
        assert choose_model(msg, [], router_client=FakeClient([RuntimeError("x")]))["model"] != "anthropic"
    # pulsuz modellər limitdədir: avtomatik rejim yenə də Claude-a keçmir (fallback söndürülüb)
    models.mark_limited("gemini"); models.mark_limited("groq")
    assert choose_model("salam", [], router_client=FakeClient([]))["model"] in ("gemini", "groq")
    # yeganə açar Claude-dursa — istifadə olunur
    models.clear_cooldowns()
    os.environ.pop("GEMINI_API_KEY"); os.environ.pop("GROQ_API_KEY")
    r = choose_model("salam", [])
    assert r["model"] == "anthropic" and r["by"] == "only"
    os.environ.pop("ANTHROPIC_API_KEY")
    _both_keys()


def test_paid_fallback_only_when_enabled():
    setup_function()
    _both_keys()
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    # söndürülüb: groq limitdə → gemini, Claude-a keçmir
    groq = FakeClient([_rate_limit(), _rate_limit()])
    gemini = FakeClient([_rate_limit(), _rate_limit()])
    try:
        ExpenseAgent(clients={"groq": groq, "gemini": gemini}).run(
            [{"role": "user", "content": "salam"}], model_choice="groq"
        )
        raise AssertionError("xəta gözlənirdi")
    except RuntimeError as exc:
        assert "gözləyib" in str(exc)
    # açıqdır: Claude ehtiyat kimi işləyir
    _both_keys()
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    os.environ["ANTHROPIC_AS_FALLBACK"] = "true"
    groq = FakeClient([_rate_limit(), _rate_limit()])
    gemini = FakeClient([_rate_limit(), _rate_limit()])
    claude = FakeClient([_text_msg("Salam!")])
    out = ExpenseAgent(clients={"groq": groq, "gemini": gemini, "anthropic": claude}).run(
        [{"role": "user", "content": "salam"}], model_choice="groq"
    )
    assert out["model"] == "anthropic"
    os.environ.pop("ANTHROPIC_API_KEY"); os.environ.pop("ANTHROPIC_AS_FALLBACK")
    import models
    models.clear_cooldowns()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("✓", name)
    print("Bütün testlər keçdi.")
