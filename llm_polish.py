"""提醒文案的 LLM 润色层。

设计原则：模板永远是事实来源。LLM 只被允许改写语气和措辞，
输出后逐项校验事实是否原样保留，任何一项缺失或异常都回落到模板文案。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence

__all__ = [
    "build_messages",
    "clean_output",
    "polish_text",
    "validate_output",
]

Generate = Callable[[str, str], Awaitable[str]]

SYSTEM_PROMPT = (
    "你是课程提醒的文案助手。用户会给你一段事实稿，"
    "你的任务只是把它说得更自然、更有人味，可以加一句贴心的提醒。"
    "硬性要求：事实稿里的课程名、时间、地点必须原样保留，一个字都不能改、不能省略、不能编造新的时间或地点。"
    "输出纯文本，不要 markdown、不要代码块、不要引号包裹、不要加解释，最多比事实稿长一点。"
)

_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
_QUOTE_PAIRS = (
    ("“", "”"),
    ("「", "」"),
    ("\"", "\""),
    ("'", "'"),
)


def build_messages(
    base_text: str,
    facts: Sequence[str],
    extra_hint: str | None = None,
) -> list[dict[str, str]]:
    """构造送给 LLM 的消息列表。``base_text`` 是模板稿，``facts`` 是必须保留的字段。"""
    must = "、".join(dict.fromkeys(f for f in facts if f))
    user_lines = [
        "事实稿：",
        base_text,
        "",
        f"必须原样保留：{must}",
    ]
    if extra_hint:
        user_lines.append(f"语气参考：{extra_hint}")
    user_lines.append("请只输出润色后的文案。")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def clean_output(text: str | None) -> str:
    """去掉代码块、包裹引号与首尾空白，返回可校验的候选文案。"""
    if not text:
        return ""
    out = _FENCE.sub("", text.strip())
    out = out.strip()
    for opener, closer in _QUOTE_PAIRS:
        if len(out) > 2 and out.startswith(opener) and out.endswith(closer):
            out = out[1:-1].strip()
            break
    return out


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def validate_output(
    candidate: str,
    base_text: str,
    facts: Iterable[str],
    max_extra_chars: int = 120,
) -> bool:
    """候选文案是否可用：事实齐全、长度合理、不是空话。"""
    candidate = clean_output(candidate)
    if not candidate:
        return False
    # 长度上限：模板稿 + 允许的润色余量，避免模型长篇大论刷屏
    if len(candidate) > len(base_text) + max_extra_chars:
        return False
    normalized = _normalize(candidate)
    for fact in facts:
        if not fact:
            continue
        if _normalize(str(fact)) not in normalized:
            return False
    # 至少得真的改写过一点，否则没必要多花一次调用
    return normalized != _normalize(base_text)


async def polish_text(
    base_text: str,
    facts: Iterable[str],
    generate: Generate,
    *,
    timeout: float = 8.0,
    max_extra_chars: int = 120,
    extra_hint: str | None = None,
) -> str:
    """用 ``generate(system_prompt, user_prompt)`` 改写文案，失败即回落模板。

    任何异常（含超时）都只返回 ``base_text``，绝不向上抛——
    提醒的送达优先级高于文案好看。
    """
    if not base_text.strip():
        return base_text
    facts = [str(fact) for fact in facts if fact]
    messages = build_messages(base_text, facts, extra_hint)
    try:
        raw = await asyncio.wait_for(
            generate(messages[0]["content"], messages[-1]["content"]),
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return base_text
    if not validate_output(raw, base_text, facts, max_extra_chars):
        return base_text
    return clean_output(raw)
