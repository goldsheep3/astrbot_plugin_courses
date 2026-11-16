import asyncio
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
import llm_polish

BASE = "提醒：程序设计基础 20 分钟后开始\n16:10-17:50｜西二区博才楼 6 教 427"
FACTS = ["程序设计基础", "16:10", "17:50", "西二区博才楼 6 教 427"]


def run(coro):
    return asyncio.run(coro)


def fake_generate(reply):
    calls = []

    async def generate(system_prompt, user_prompt):
        calls.append((system_prompt, user_prompt))
        if isinstance(reply, Exception):
            raise reply
        return reply

    return generate, calls


def test_build_messages_lists_facts():
    messages = llm_polish.build_messages(BASE, FACTS, "轻快一点")
    assert messages[0]["role"] == "system"
    user = messages[-1]["content"]
    assert BASE in user
    for fact in FACTS:
        assert fact in user
    assert "轻快一点" in user


def test_clean_output_strips_fences_and_quotes():
    assert llm_polish.clean_output("```text\n你好\n```") == "你好"
    assert llm_polish.clean_output("“你好”") == "你好"
    assert llm_polish.clean_output(None) == ""


def test_validate_output_accepts_faithful_rewrite():
    candidate = "提醒喵：程序设计基础 20 分钟后开始\n16:10-17:50｜西二区博才楼 6 教 427，带好水~"
    assert llm_polish.validate_output(candidate, BASE, FACTS)


def test_validate_output_rejects_missing_location():
    candidate = "提醒喵：程序设计基础 20 分钟后开始\n16:10-17:50｜博才楼"
    assert not llm_polish.validate_output(candidate, BASE, FACTS)


def test_validate_output_rejects_changed_time():
    candidate = "提醒喵：程序设计基础 20 分钟后开始\n16:00-17:50｜西二区博才楼 6 教 427"
    assert not llm_polish.validate_output(candidate, BASE, FACTS)


def test_validate_output_rejects_too_long():
    candidate = "程序设计基础 16:10 17:50 西二区博才楼 6 教 427 " + "喵" * 300
    assert not llm_polish.validate_output(candidate, BASE, FACTS, max_extra_chars=50)


def test_validate_output_rejects_empty_and_unchanged():
    assert not llm_polish.validate_output("", BASE, FACTS)
    assert not llm_polish.validate_output(BASE, BASE, FACTS)


def test_polish_text_returns_model_output():
    reply = "提醒喵：程序设计基础 20 分钟后开始\n16:10-17:50｜西二区博才楼 6 教 427，冲鸭"
    generate, calls = fake_generate(reply)
    out = run(llm_polish.polish_text(BASE, FACTS, generate))
    assert out == reply
    assert len(calls) == 1


def test_polish_text_falls_back_on_missing_fact():
    generate, _ = fake_generate("提醒喵：程序设计基础 20 分钟后开始")
    assert run(llm_polish.polish_text(BASE, FACTS, generate)) == BASE


def test_polish_text_falls_back_on_exception():
    generate, _ = fake_generate(RuntimeError("provider 挂了"))
    assert run(llm_polish.polish_text(BASE, FACTS, generate)) == BASE


def test_polish_text_falls_back_on_empty_reply():
    generate, _ = fake_generate("")
    assert run(llm_polish.polish_text(BASE, FACTS, generate)) == BASE


def test_polish_text_falls_back_on_timeout():
    async def slow(system_prompt, user_prompt):
        await asyncio.sleep(5)
        return "迟到的文案"

    out = run(llm_polish.polish_text(BASE, FACTS, slow, timeout=0.05))
    assert out == BASE


def test_polish_text_skips_blank_base():
    generate, calls = fake_generate("不该被调用")
    assert run(llm_polish.polish_text("   ", FACTS, generate)) == "   "
    assert calls == []


def test_polish_text_propagates_cancelled():
    async def cancelled(system_prompt, user_prompt):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(llm_polish.polish_text(BASE, FACTS, cancelled))
