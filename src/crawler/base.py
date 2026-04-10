from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timezone
import re
from typing import Dict, List, Optional, Tuple

from src.core.paper import Paper


class BaseCrawler(ABC):
    def __init__(self, journal_config: dict):
        self.journal_config = journal_config

    @abstractmethod
    def fetch(self) -> List[Paper]:
        """Fetch papers from one journal source."""

    def select_issue_batch(self, papers: List[Paper]) -> List[Paper]:
        """
        Select papers from latest / second-latest issue according to `issue_index`.
        If issue metadata is missing, fallback to latest papers by publish_date.
        """
        issue_index = int(self.journal_config.get("issue_index", 0))
        min_issue_size = int(self.journal_config.get("min_issue_size", 1))
        max_items = self.journal_config.get("max_items")
        if not papers:
            return []

        grouped: Dict[str, List[Paper]] = defaultdict(list)
        issue_dates: Dict[str, datetime] = {}

        for paper in papers:
            issue_key = (paper.issue or "").strip() or "unknown"
            grouped[issue_key].append(paper)
            paper_date = self._safe_date(paper.publish_date)
            if paper_date:
                current = issue_dates.get(issue_key)
                issue_dates[issue_key] = max(current, paper_date) if current else paper_date

        # If every paper has unknown issue, fallback to publish-date sorting.
        if set(grouped.keys()) == {"unknown"}:
            papers_sorted = sorted(
                papers,
                key=lambda p: self._safe_date(p.publish_date) or datetime.min,
                reverse=True,
            )
            if max_items:
                return papers_sorted[: int(max_items)]
            return papers_sorted

        issue_order: List[Tuple[str, Tuple[int, int, datetime], int]] = []
        for issue_key, issue_papers in grouped.items():
            if issue_key == "unknown":
                continue
            fallback = max(
                (
                    self._safe_date(p.publish_date) or datetime.min
                    for p in issue_papers
                ),
                default=datetime.min,
            )
            issue_dt = issue_dates.get(issue_key, fallback)
            vol_num, issue_num = self._parse_issue_numbers(issue_key)
            issue_order.append(
                (
                    issue_key,
                    (vol_num, issue_num, issue_dt),
                    len(issue_papers),
                )
            )

        if min_issue_size > 1:
            filtered = [x for x in issue_order if x[2] >= min_issue_size]
            if filtered:
                issue_order = filtered

        issue_order.sort(key=lambda x: x[1], reverse=True)
        if issue_index >= len(issue_order):
            issue_index = 0

        target_issue_key = issue_order[issue_index][0]
        selected = sorted(
            grouped[target_issue_key],
            key=lambda p: self._safe_date(p.publish_date) or datetime.min,
            reverse=True,
        )
        if max_items:
            selected = selected[: int(max_items)]
        return selected

    @staticmethod
    def _safe_date(raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        candidates = [
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%a, %d %b %Y %H:%M:%S %Z",
            "%a, %d %b %Y %H:%M:%S %z",
        ]
        for fmt in candidates:
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                return dt
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            return None

    @staticmethod
    def _parse_issue_numbers(issue_key: str) -> Tuple[int, int]:
        """
        Parse labels like:
        - Volume 453
        - Volume 453, Issue 2
        Unknown/other formats return (0, 0).
        """
        if not issue_key:
            return 0, 0

        vol_match = re.search(r"volume\s+(\d+)", issue_key, flags=re.I)
        issue_match = re.search(r"issue\s+(\d+)", issue_key, flags=re.I)
        vol_num = int(vol_match.group(1)) if vol_match else 0
        issue_num = int(issue_match.group(1)) if issue_match else 0
        return vol_num, issue_num
