from __future__ import annotations

import re
from typing import Dict, Iterable, List


class FigurePrioritizer:
    SLOT_ORDER = (
        "abstract_overview",
        "model_method",
        "result_experiment",
        "other",
    )
    SLOT_LABELS = {
        "abstract_overview": "摘要/概览图",
        "model_method": "模型/方法图",
        "result_experiment": "结果/实验图",
        "other": "补充图",
    }
    ABSTRACT_STRONG = (
        "graphical abstract",
        "visual abstract",
        "overview",
        "overall framework",
        "conceptual illustration",
        "summary illustration",
        "schematic overview",
    )
    ABSTRACT_WEAK = (
        "schematic",
        "schematics",
        "illustration",
        "domain schematic",
        "domain schematics",
        "overview of",
        "general framework",
    )
    MODEL_STRONG = (
        "framework",
        "workflow",
        "flow-chart",
        "flowchart",
        "pipeline",
        "architecture",
        "training framework",
        "modeling strategy",
        "constitutive model",
        "algorithm",
        "method",
        "operator",
    )
    MODEL_WEAK = (
        "model",
        "strategy",
        "setup",
        "grid",
        "discretization",
        "discretisation",
        "geometry",
        "network",
        "backbone",
        "schematic",
        "diagram",
    )
    RESULT_STRONG = (
        "result",
        "results",
        "experiment",
        "experimental",
        "validation",
        "comparison",
        "performance",
        "error",
        "response",
        "stress-strain",
        "stress strain",
        "fracture",
        "crack",
        "failure",
        "microstructure",
        "accuracy",
        "benchmark",
        "test",
    )
    RESULT_WEAK = (
        "simulation",
        "predicted",
        "prediction",
        "field",
        "map",
        "curve",
        "load",
        "displacement",
        "strain",
        "stress",
        "temperature",
        "velocity",
        "contour",
        "response",
    )

    @classmethod
    def prioritize(
        cls,
        figures: Iterable[Dict[str, str]],
        limit: int | None = None,
    ) -> List[Dict[str, str]]:
        source = [dict(item) for item in figures if isinstance(item, dict)]
        if not source:
            return []

        annotated = [
            cls._annotate(figure, index=index, total=len(source))
            for index, figure in enumerate(source)
        ]
        selected = cls._select_diverse_subset(
            annotated,
            limit=min(limit or len(annotated), len(annotated)),
        )
        return [cls._strip_internal_fields(item) for item in selected]

    @classmethod
    def prioritize_papers(cls, papers) -> int:
        updated = 0
        for paper in papers:
            figures = getattr(paper, "figures", None) or []
            if not figures:
                continue
            prioritized = cls.prioritize(figures, limit=len(figures))
            if prioritized != figures:
                paper.figures = prioritized
                updated += 1
        return updated

    @classmethod
    def _annotate(cls, figure: Dict[str, str], index: int, total: int) -> Dict[str, str]:
        item = dict(figure)
        text = cls._normalized_text(item)
        slot_scores = {slot: 0 for slot in cls.SLOT_ORDER}
        reasons = {slot: [] for slot in cls.SLOT_ORDER}

        def add(slot: str, score: int, reason: str) -> None:
            slot_scores[slot] += score
            reasons[slot].append(reason)

        if cls._contains_any(text, cls.ABSTRACT_STRONG):
            add("abstract_overview", 8, "图注含 overview/graphical abstract 强信号")
        if cls._contains_any(text, cls.ABSTRACT_WEAK):
            add("abstract_overview", 4, "图注偏概览或示意")

        if cls._contains_any(text, cls.MODEL_STRONG):
            add("model_method", 8, "图注含 framework/workflow/model 强信号")
        if cls._contains_any(text, cls.MODEL_WEAK):
            add("model_method", 4, "图注偏方法或结构示意")

        if cls._contains_any(text, cls.RESULT_STRONG):
            add("result_experiment", 8, "图注含 results/experiment/validation 强信号")
        if cls._contains_any(text, cls.RESULT_WEAK):
            add("result_experiment", 4, "图注偏结果或响应展示")

        if index == 0:
            add("abstract_overview", 4, "首图优先视作摘要附近概览图")
            add("model_method", 1, "首图也常承载总体方法示意")
        elif index == 1:
            add("model_method", 3, "第二图优先视作模型或方法图")
        elif index >= 2:
            add("result_experiment", 3, "后续图优先视作结果或实验图")

        if total == 1:
            add("model_method", 2, "仅有一张图时默认兼作方法主图")

        if item.get("caption"):
            add("other", 1, "存在图注说明")

        ranked_slots = sorted(
            cls.SLOT_ORDER,
            key=lambda slot: (slot_scores[slot], -cls.SLOT_ORDER.index(slot)),
            reverse=True,
        )
        best_slot = ranked_slots[0]
        second_score = slot_scores[ranked_slots[1]] if len(ranked_slots) > 1 else 0
        confidence = slot_scores[best_slot] - second_score

        item["_slot_scores"] = slot_scores
        item["_slot_reasons"] = reasons
        item["_source_index"] = index
        item["_confidence"] = confidence
        item["role_hint"] = best_slot
        item["role_hint_cn"] = cls.SLOT_LABELS[best_slot]
        item["selection_reason"] = "；".join(reasons[best_slot][:3]) or "按位置默认推断"
        item["priority_group"] = str(cls.SLOT_ORDER.index(best_slot) + 1)
        return item

    @classmethod
    def _select_diverse_subset(
        cls,
        annotated: List[Dict[str, str]],
        limit: int,
    ) -> List[Dict[str, str]]:
        if limit <= 0:
            return []

        selected: List[Dict[str, str]] = []
        used = set()
        desired_slots = list(cls.SLOT_ORDER[:3])

        for slot in desired_slots:
            candidate = cls._best_for_slot(annotated, used, slot)
            if candidate is None:
                continue
            used.add(candidate["_source_index"])
            picked = dict(candidate)
            picked["display_role"] = slot
            picked["display_role_cn"] = cls.SLOT_LABELS[slot]
            if picked["role_hint"] != slot:
                picked["selection_reason"] = (
                    f"{picked['selection_reason']}；为满足展示顺序补足“{cls.SLOT_LABELS[slot]}”"
                )
            selected.append(picked)
            if len(selected) >= limit:
                break

        if len(selected) < limit:
            remaining = [
                item for item in annotated if item["_source_index"] not in used
            ]
            remaining.sort(
                key=lambda item: (
                    -cls._best_slot_score(item),
                    -int(item.get("_confidence") or 0),
                    item["_source_index"],
                )
            )
            for item in remaining:
                picked = dict(item)
                picked["display_role"] = picked["role_hint"]
                picked["display_role_cn"] = picked["role_hint_cn"]
                selected.append(picked)
                used.add(item["_source_index"])
                if len(selected) >= limit:
                    break

        selected.sort(
            key=lambda item: (
                cls.SLOT_ORDER.index(str(item.get("display_role") or item.get("role_hint") or "other")),
                item["_source_index"],
            )
        )
        for rank, item in enumerate(selected, start=1):
            item["priority_rank"] = str(rank)
            item["detected_role_hint"] = str(item.get("role_hint") or "")
            item["detected_role_hint_cn"] = str(item.get("role_hint_cn") or "")
            item["display_role"] = str(item.get("display_role") or item.get("role_hint") or "other")
            item["display_role_cn"] = str(
                item.get("display_role_cn") or item.get("role_hint_cn") or cls.SLOT_LABELS["other"]
            )
            item["role_hint"] = item["display_role"]
            item["role_hint_cn"] = item["display_role_cn"]
        return selected

    @classmethod
    def _best_for_slot(
        cls,
        annotated: List[Dict[str, str]],
        used: set,
        slot: str,
    ) -> Dict[str, str] | None:
        candidates = [
            item
            for item in annotated
            if item["_source_index"] not in used and int(item["_slot_scores"].get(slot, 0)) > 0
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                -int(item["_slot_scores"].get(slot, 0)),
                item["_source_index"],
                -int(item.get("_confidence") or 0),
            )
        )
        return candidates[0]

    @staticmethod
    def _best_slot_score(item: Dict[str, str]) -> int:
        slot_scores = item.get("_slot_scores") or {}
        if not isinstance(slot_scores, dict):
            return 0
        return max(int(value) for value in slot_scores.values()) if slot_scores else 0

    @staticmethod
    def _normalized_text(figure: Dict[str, str]) -> str:
        bits = [
            str(figure.get("label") or ""),
            str(figure.get("caption") or ""),
            str(figure.get("alt") or ""),
        ]
        return " ".join(bits).strip().lower()

    @classmethod
    def _contains_any(cls, text: str, keywords: Iterable[str]) -> bool:
        return any(cls._contains_keyword(text, keyword) for keyword in keywords)

    @staticmethod
    def _contains_keyword(text: str, keyword: str) -> bool:
        phrase = str(keyword or "").strip().lower()
        if not phrase:
            return False
        if " " in phrase or "-" in phrase:
            return phrase in text
        return re.search(rf"\b{re.escape(phrase)}\b", text) is not None

    @staticmethod
    def _strip_internal_fields(item: Dict[str, str]) -> Dict[str, str]:
        cleaned = {}
        for key, value in item.items():
            if str(key).startswith("_"):
                continue
            cleaned[key] = value
        return cleaned
