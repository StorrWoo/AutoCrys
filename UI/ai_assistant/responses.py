"""Shared response composition used by the UI and headless tests."""

from __future__ import annotations

import json
import re

from .artifacts import AnalysisReport
from .knowledge import KnowledgeBase
from .llm import LLMBackend


RESULT_WORDS = (
    "分析", "问题", "结果", "下一步", "质量", "风险", "指标",
    "analy", "problem", "result", "next", "quality", "risk", "metric",
)


def is_result_question(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in RESULT_WORDS)


def offline_response(
    text: str,
    knowledge: KnowledgeBase,
    report: AnalysisReport | None,
    backend: LLMBackend,
) -> str:
    if report and is_result_question(text):
        return report.to_display_text(compact=False)
    results = knowledge.search(text)
    if results:
        module, title, content = results[0]
        preview = content[:1000] + ("..." if len(content) > 1000 else "")
        return f"参考 {module} / {title}：\n{preview}"
    return backend.chat(knowledge.build_system_prompt(), text)


def grounded_user_message(prompt_context: str, user_text: str) -> str:
    return f"{prompt_context}\n\nUser: {user_text}"


def limit_text(text: str, max_chars: int = 0) -> str:
    """Clean a reply and, at the safety limit, prefer ending on a sentence boundary."""
    cleaned = re.sub(r"<think>.*?</think>", "", str(text), flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if max_chars <= 0:
        return cleaned
    if len(cleaned) <= max_chars:
        return cleaned
    content_limit = max(1, max_chars - 1)
    prefix = cleaned[:content_limit]
    boundaries = list(re.finditer(r"[。！？!?；;\n]", prefix))
    if boundaries:
        sentence_end = boundaries[-1].end()
        if sentence_end >= max(4, content_limit // 2):
            return prefix[:sentence_end].rstrip() + "…"
    return prefix.rstrip() + "…"


def _json_text(value: object, limit: int = 16000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[-limit:]


def suggestion_evidence(
    report: AnalysisReport | None,
    *,
    operation: dict | None = None,
    hca_result: dict | None = None,
    hca_cutoff: float | None = None,
    screen: dict | None = None,
    recent_logs: list[str] | None = None,
    diagnoses: list[object] | None = None,
) -> str:
    """Build grounded, operation-aware evidence for local/cloud models."""
    parts: list[str] = []
    if operation:
        meta = {key: value for key, value in operation.items() if key not in {"logs", "result"}}
        parts.append("[Last completed operation]\n" + _json_text(meta, 6000))
        if operation.get("result") is not None:
            parts.append("[Operation result]\n" + _json_text(operation.get("result"), 18000))
    else:
        parts.append("[Last completed operation]\nNone recorded")
    if screen:
        parts.append("[Current UI state]\n" + _json_text(screen, 10000))
    if hca_result:
        parts.append(
            f"[HCA state; selected cutoff={hca_cutoff}]\n"
            "AutoCrys cutoff semantics: branches merge when distance <= cutoff; "
            "a lower cutoff is stricter and a higher cutoff is more permissive.\n"
            + _json_text(hca_result, 18000)
        )
    if report:
        parts.append(report.to_prompt())
    log_lines = list(operation.get("logs", [])) if operation else []
    if not log_lines:
        log_lines = list(recent_logs or [])
    if log_lines:
        log_text = "\n".join(str(line) for line in log_lines[-250:])
        parts.append("[Log for the operation]\n" + log_text[-24000:])
    if diagnoses:
        rendered = []
        for item in diagnoses[-20:]:
            rendered.append(
                _json_text(
                    {
                        "severity": getattr(item, "severity", ""),
                        "title": getattr(item, "title", ""),
                        "detail": getattr(item, "detail", ""),
                        "suggestion": getattr(item, "suggestion", ""),
                        "matched_line": getattr(item, "matched_line", ""),
                    },
                    2000,
                )
            )
        parts.append("[Rule diagnoses]\n" + "\n".join(rendered))
    return "\n\n".join(parts)


def _hca_summary(hca_result: dict, cutoff: float | None, screen: dict) -> tuple[str, list[str]]:
    datasets = [str(item) for item in hca_result.get("datasets", [])]
    methods = dict(hca_result.get("methods", {}))
    details = [f"HCA分析了{len(datasets)}个数据集，方法为{', '.join(methods) or '未知'}。"]
    for method, payload_value in methods.items():
        payload = dict(payload_value or {})
        pairwise = [dict(item) for item in payload.get("pairwise", [])]
        if not pairwise:
            continue
        distances = [float(item["distance"]) for item in pairwise if item.get("distance") is not None]
        commons = [int(item["common_reflections"]) for item in pairwise if item.get("common_reflections") is not None]
        cc_values = [float(item["cc1"]) for item in pairwise if item.get("cc1") is not None]
        metrics = []
        if distances:
            metrics.append(f"距离范围{min(distances):.3f}–{max(distances):.3f}")
        if cc_values:
            metrics.append(f"CC1范围{min(cc_values):.3f}–{max(cc_values):.3f}")
        if commons:
            metrics.append(f"共同反射最少{min(commons)}")
        if metrics:
            details.append(f"{method}: " + "，".join(metrics) + "。")
    advice: list[str] = []
    clusters = [list(item) for item in screen.get("hca_clusters", []) if item]
    if cutoff is None:
        advice.append("先结合树状图的明显跃迁选择cutoff，再评估分组；不要在尚未选cutoff时自动合并全部数据。")
    else:
        multi = [item for item in clusters if len(item) >= 2]
        details.append(f"当前cutoff={cutoff:.4g}，界面得到{len(clusters)}组，其中{len(multi)}组可进行多数据集合并。")
        if multi:
            advice.append("只对同组且晶胞/空间群合理的数据执行合并，并在XSCALE后复查完整度、R因子和ISa。")
        else:
            advice.append("当前cutoff没有形成可合并的多数据集组；不要强行合并，可重新审视cutoff或数据兼容性。")
    return " ".join(details), advice


def _operation_result_summary(operation: dict) -> str:
    result = operation.get("result")
    if not isinstance(result, dict):
        return ""
    rows = [dict(item) for item in result.get("rows", []) if isinstance(item, dict)]
    if rows:
        names = [str(row.get("Dataset", "?")) for row in rows]
        completeness = []
        rfactors = []
        for row in rows:
            try:
                completeness.append(float(str(row.get("Completeness", "")).rstrip("%")))
            except ValueError:
                pass
            try:
                rfactors.append(float(str(row.get("Rfactor", "")).rstrip("%")))
            except ValueError:
                pass
        metrics = [f"返回{len(rows)}个数据集：{', '.join(names)}"]
        if completeness:
            metrics.append(f"完整度范围{min(completeness):.1f}%–{max(completeness):.1f}%")
        if rfactors:
            metrics.append(f"R因子范围{min(rfactors):.1f}%–{max(rfactors):.1f}%")
        return "，".join(metrics) + "。"
    merge_results = [dict(item) for item in result.get("results", []) if isinstance(item, dict)]
    if merge_results or "errors" in result:
        errors = list(result.get("errors", []))
        return f"合并完成{len(merge_results)}组，失败{len(errors)}组。"
    if "sparse_observations" in result or "peaks" in result:
        observations = dict(result.get("sparse_observations", {})).get("count")
        peaks = dict(result.get("peaks", {})).get("count")
        return f"重构返回稀疏观测{observations}个、峰候选{peaks}个。"
    if "total_peaks" in result:
        return (
            f"寻峰得到{result.get('total_peaks')}个峰，覆盖"
            f"{result.get('frames_with_peaks')}/{result.get('processed_nonzero_frames')}个非零帧。"
        )
    return ""


def compact_suggestion(
    report: AnalysisReport | None,
    max_chars: int = 0,
    *,
    operation: dict | None = None,
    hca_result: dict | None = None,
    hca_cutoff: float | None = None,
    screen: dict | None = None,
    diagnoses: list[object] | None = None,
) -> str:
    """Create an operation-aware deterministic fallback for AI suggestion."""
    screen = dict(screen or {})
    op_name = str((operation or {}).get("name", "")).strip()
    success = (operation or {}).get("success")
    judgement_parts: list[str] = []
    suggestions: list[str] = []
    if operation:
        state = "成功" if success else "失败"
        judgement_parts.append(f"上一步“{op_name or '未命名操作'}”执行{state}。")
        if not success and operation.get("error"):
            judgement_parts.append(f"错误：{operation['error']}")
        result_summary = _operation_result_summary(operation)
        if result_summary:
            judgement_parts.append(result_summary)

    operation_identity = (
        op_name + " " + str((operation or {}).get("command", ""))
    ).lower()
    is_hca = (
        "hca" in operation_identity
        or " cluster " in f" {operation_identity} "
        or (operation is None and hca_result is not None)
    )
    effective_hca = hca_result
    if is_hca and operation and isinstance(operation.get("result"), dict):
        effective_hca = operation["result"]
    if is_hca and effective_hca:
        hca_text, hca_advice = _hca_summary(effective_hca, hca_cutoff, screen)
        judgement_parts.append(hca_text)
        suggestions.extend(hca_advice)

    if diagnoses:
        serious = [item for item in diagnoses if getattr(item, "severity", "") in {"error", "warning"}]
        if serious:
            latest = serious[-1]
            judgement_parts.append(
                f"日志诊断：{getattr(latest, 'title', '')}——{getattr(latest, 'detail', '')}"
            )
            if getattr(latest, "suggestion", ""):
                suggestions.append(str(latest.suggestion))

    if report is not None:
        judgement_parts.append(report.summary.strip() or f"产物状态为{report.status}。")
        if report.findings:
            finding = report.findings[0]
            judgement_parts.append(f"产物问题：{finding.title}——{finding.detail}")
            if finding.suggestion:
                suggestions.append(finding.suggestion)
        suggestions.extend(report.next_steps[:3])
    elif not judgement_parts:
        judgement_parts.append("尚未记录到可分析的操作或结果。")
        suggestions.append("先完成一次处理、聚类、合并、重构、求解或精修，再点击“AI建议”。")

    if operation and not success and not suggestions:
        log_lines = [str(line) for line in operation.get("logs", [])]
        suspect = next((line for line in reversed(log_lines) if re.search(r"error|fail|warning", line, re.I)), "")
        suggestions.append(f"先处理日志中的最后一个异常：{suspect}" if suspect else "查看该操作日志末尾并先修复首个明确错误。")

    advice = "；".join(dict.fromkeys(item.strip() for item in suggestions if item and item.strip()))
    if not advice:
        advice = "复核上一步的关键指标与产物，再决定下一步。"
    judgement = " ".join(part for part in judgement_parts if part)
    return limit_text(f"判断：{judgement}\n建议：{advice}", max_chars)
