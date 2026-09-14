"""Intent Router — rule-first classification (architecture §3.1).

Cheap, deterministic keyword rules run first; an LLM fallback can be added later
for ambiguous input. Returns one of the five intents.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Intent(str, Enum):
    QUERY_GENERATION = "query_generation"
    INVESTIGATION = "investigation"
    RESOURCE_GENERATION = "resource_generation"
    ADMINISTRATION = "administration"
    GENERAL_HELP = "general_help"


@dataclass
class RoutingResult:
    intent: Intent
    matched: str | None = None


# order matters: administration/destructive first, then investigation, generation
_RULES: list[tuple[Intent, re.Pattern]] = [
    (Intent.ADMINISTRATION, re.compile(
        r"\b(delete|remove|drop|revoke|grant|change\s+role|permission|quota|"
        r"service\s+account|删除|移除|权限|配额|角色)\b", re.I)),
    (Intent.INVESTIGATION, re.compile(
        r"\b(why|root\s*cause|what\s+happened|investigate|fired|triggered|"
        r"为什么|原因|排查|为何|告警.*触发|触发.*告警)\b", re.I)),
    (Intent.RESOURCE_GENERATION, re.compile(
        r"\b(create|build|add|update|schedule|set\s+up|"
        r"创建|新建|构建|生成.*(alert|dashboard|pipeline|report|view|告警|仪表盘|看板|报表))\b", re.I)),
    (Intent.QUERY_GENERATION, re.compile(
        r"\b(write|generate|convert|sql|vrl|promql|regex|query|"
        r"写|生成|转换|查询)\b", re.I)),
]


def route(text: str) -> RoutingResult:
    for intent, pat in _RULES:
        m = pat.search(text)
        if m:
            return RoutingResult(intent=intent, matched=m.group(0))
    return RoutingResult(intent=Intent.GENERAL_HELP)
