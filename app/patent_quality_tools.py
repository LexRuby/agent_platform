"""专利草稿的确定性质量检查工具。

这些工具只报告文本结构和可追溯证据，不替代专利代理师判断，也不把
启发式检查包装成新颖性、创造性或可实施性的法律结论。
"""

import json
import re
from collections import Counter
from typing import Any

from agentscope.message import TextBlock
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk


_CLAIM_START_RE = re.compile(
    r"(?m)^\s*(?:权利要求\s*)?(\d+)\s*[\.、．]\s*",
)
_REFERENCE_RE = re.compile(
    r"权利要求\s*([0-9]+(?:\s*(?:[-至到、,，或和]|以及)\s*[0-9]+)*)",
)


def _result(payload: dict[str, Any]) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
    )


def _claim_blocks(text: str) -> list[tuple[int, str]]:
    matches = list(_CLAIM_START_RE.finditer(text or ""))
    return [
        (
            int(match.group(1)),
            text[
                match.end():
                matches[index + 1].start() if index + 1 < len(matches) else len(text)
            ].strip(),
        )
        for index, match in enumerate(matches)
    ] if matches else []


def _reference_numbers(expression: str) -> list[int]:
    numbers = [int(value) for value in re.findall(r"\d+", expression)]
    if len(numbers) == 2 and re.search(r"[-至到]", expression):
        start, end = numbers
        return list(range(start, end + 1)) if start <= end else numbers
    return numbers


class _ReadOnlyPatentTool(ToolBase):
    is_concurrency_safe = True
    is_read_only = True

    async def check_permissions(
        self,
        tool_input: dict,
        context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Read-only patent draft quality check.",
        )


class ValidateClaimStructure(_ReadOnlyPatentTool):
    name = "validate_claim_structure"
    description = (
        "检查中文权利要求书的编号和引用关系：缺号、重号、顺序错误、"
        "引用不存在/自身/后项权利要求，以及疑似缺少引用的从属权利要求。"
        "只做结构检查，不判断新颖性、创造性或保护范围是否适当。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "claims": {
                "type": "string",
                "description": "完整权利要求书，每项以“1.”或“权利要求1、”起始。",
            },
        },
        "required": ["claims"],
    }

    async def call(self, **kwargs: Any) -> ToolChunk:
        blocks = _claim_blocks(str(kwargs.get("claims") or ""))
        numbers = [number for number, _ in blocks]
        issues: list[dict[str, Any]] = []
        if not blocks:
            issues.append({"code": "NO_NUMBERED_CLAIMS", "message": "未识别到编号权利要求。"})
        duplicates = sorted(number for number, count in Counter(numbers).items() if count > 1)
        if duplicates:
            issues.append({"code": "DUPLICATE_NUMBERS", "claims": duplicates})
        expected = list(range(1, max(numbers) + 1)) if numbers else []
        missing = sorted(set(expected) - set(numbers))
        if missing:
            issues.append({"code": "MISSING_NUMBERS", "claims": missing})
        if numbers and numbers != sorted(numbers):
            issues.append({"code": "OUT_OF_ORDER", "claims": numbers})

        known = set(numbers)
        for number, body in blocks:
            refs = [
                ref
                for match in _REFERENCE_RE.finditer(body)
                for ref in _reference_numbers(match.group(1))
            ]
            if number > 1 and not refs and "所述" in body:
                issues.append({
                    "code": "DEPENDENCY_NOT_FOUND",
                    "claim": number,
                    "message": "未识别到从属引用；若本项为独立权利要求可忽略。",
                })
            for ref in refs:
                if ref not in known:
                    code = "REFERENCE_NOT_FOUND"
                elif ref >= number:
                    code = "SELF_OR_FORWARD_REFERENCE"
                else:
                    continue
                issues.append({"code": code, "claim": number, "reference": ref})
        return _result({
            "check": self.name,
            "claim_count": len(blocks),
            "claim_numbers": numbers,
            "issues": issues,
            "passed": not issues,
            "scope": "仅检查编号和显式引用关系；仍需人工审查清楚、简要、支持及保护范围。",
        })


class CheckPatentTerminology(_ReadOnlyPatentTool):
    name = "check_patent_terminology"
    description = (
        "按给定规范术语和禁用别名，检查权利要求、发明内容、具体实施方式"
        "中的缺失与术语漂移。用于跨章节统一名称，不自动推断同义词。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "documents": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "章节名到正文的映射。",
            },
            "terminology": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "canonical": {"type": "string"},
                        "aliases": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["canonical"],
                },
                "description": "规范术语及需要发现的别名；来自 Final_result/权利要求术语表。",
            },
        },
        "required": ["documents", "terminology"],
    }

    async def call(self, **kwargs: Any) -> ToolChunk:
        documents = kwargs.get("documents") or {}
        terminology = kwargs.get("terminology") or []
        issues: list[dict[str, Any]] = []
        coverage: dict[str, dict[str, int]] = {}
        for item in terminology:
            canonical = str(item.get("canonical") or "").strip()
            if not canonical:
                continue
            coverage[canonical] = {
                str(section): str(text or "").count(canonical)
                for section, text in documents.items()
            }
            missing = [section for section, count in coverage[canonical].items() if count == 0]
            if missing:
                issues.append({"code": "CANONICAL_TERM_MISSING", "term": canonical, "sections": missing})
            for alias in item.get("aliases") or []:
                alias = str(alias).strip()
                found = [section for section, text in documents.items() if alias and alias in str(text or "")]
                if found:
                    issues.append({"code": "ALIAS_USED", "canonical": canonical, "alias": alias, "sections": found})
        return _result({
            "check": self.name,
            "coverage": coverage,
            "issues": issues,
            "passed": not issues,
            "scope": "结果依赖调用方提供的术语表，未提供的同义词不会被自动识别。",
        })


class CheckClaimSupport(_ReadOnlyPatentTool):
    name = "check_claim_support"
    description = (
        "检查权利要求关键特征是否在指定说明书章节中出现，输出逐特征覆盖矩阵。"
        "用于发现说明书支持或实施展开的明显遗漏，不判断法律上的充分公开。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "features": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "integer"},
                        "feature": {"type": "string"},
                        "alternatives": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["claim", "feature"],
                },
                "description": "从权利要求逐项提取的必要/附加技术特征。",
            },
            "documents": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "待核验章节名到正文的映射，如发明内容、具体实施方式。",
            },
        },
        "required": ["features", "documents"],
    }

    async def call(self, **kwargs: Any) -> ToolChunk:
        sections = {
            str(section): str(text or "")
            for section, text in (kwargs.get("documents") or {}).items()
        }
        matrix: list[dict[str, Any]] = []
        for item in kwargs.get("features") or []:
            feature = str(item.get("feature") or "").strip()
            terms = [feature, *(str(value).strip() for value in item.get("alternatives") or [])]
            terms = [term for term in terms if term]
            hits = {
                section: [term for term in terms if term in text]
                for section, text in sections.items()
            }
            matrix.append({
                "claim": item.get("claim"),
                "feature": feature,
                "hits": hits,
                "covered": {section: bool(values) for section, values in hits.items()},
            })
        issues = [
            {
                "code": "FEATURE_NOT_COVERED",
                "claim": row["claim"],
                "feature": row["feature"],
                "sections": [section for section, covered in row["covered"].items() if not covered],
            }
            for row in matrix
            if not all(row["covered"].values())
        ]
        return _result({
            "check": self.name,
            "coverage_matrix": matrix,
            "issues": issues,
            "passed": not issues,
            "scope": "基于精确词语/给定替代表述匹配；命中不等于已充分公开，未命中也可能是改写导致。",
        })


def patent_quality_tools() -> list[ToolBase]:
    return [
        ValidateClaimStructure(),
        CheckPatentTerminology(),
        CheckClaimSupport(),
    ]
