from typing import Dict, List, Tuple

from src.core.paper import Paper


class KeywordAnalyzer:
    def __init__(self, taxonomy_cfg: dict, keywords_cfg: dict):
        self.taxonomy = taxonomy_cfg.get("taxonomy", {})
        self.focus_keywords = [
            k.lower() for k in keywords_cfg.get("focus_keywords", [])
        ]

    def analyze(self, papers: List[Paper]) -> None:
        for paper in papers:
            text = self._make_text_blob(paper)
            categories = self._match_categories(text)
            focus_hits = self._match_focus_keywords(text)

            paper.categories = categories
            paper.matched_keywords = focus_hits
            paper.related_to_focus = bool(focus_hits)
            paper.value_score = self._score(paper, categories, focus_hits)
            paper.value_reason = self._reason(categories, focus_hits)

    def _make_text_blob(self, paper: Paper) -> str:
        return " ".join(
            [
                paper.title or "",
                paper.abstract or "",
                " ".join(paper.keywords or []),
            ]
        ).lower()

    def _match_categories(self, text: str) -> List[str]:
        matched = []
        for category, cfg in self.taxonomy.items():
            keywords = [k.lower() for k in cfg.get("keywords", [])]
            if any(k in text for k in keywords):
                matched.append(category)
        return matched

    def _match_focus_keywords(self, text: str) -> List[str]:
        hits = [k for k in self.focus_keywords if k in text]
        return sorted(set(hits))

    def _score(
        self, paper: Paper, categories: List[str], focus_hits: List[str]
    ) -> int:
        score = 0
        score += min(35, len(focus_hits) * 12)
        score += min(30, len(categories) * 10)
        if paper.abstract:
            score += 15
        if paper.doi:
            score += 10
        if paper.issue:
            score += 10
        return min(score, 100)

    def _reason(self, categories: List[str], focus_hits: List[str]) -> str:
        parts: List[str] = []
        if focus_hits:
            parts.append(f"命中兴趣关键词: {', '.join(focus_hits)}")
        if categories:
            parts.append(f"归类到: {', '.join(categories)}")
        if not parts:
            return "关键词匹配较弱，建议交由 ChatGPT 做补充判断。"
        return "；".join(parts)
