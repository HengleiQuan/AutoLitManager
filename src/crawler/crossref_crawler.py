import html
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from src.core.paper import Paper
from src.crawler.base import BaseCrawler


class CrossrefCrawler(BaseCrawler):
    API_BASE = "https://api.crossref.org/journals/{issn}/works"

    def fetch(self) -> List[Paper]:
        journal = self.journal_config["name"]
        issn = self.journal_config.get("issn")
        if not issn:
            raise ValueError(f"Journal '{journal}' missing `issn` for crossref source.")

        rows = int(self.journal_config.get("rows", 200))
        url = self.API_BASE.format(issn=issn)
        params = {
            "filter": "type:journal-article",
            "sort": "issued",
            "order": "desc",
            "rows": rows,
        }
        resp = requests.get(url, params=params, timeout=45)
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])

        papers: List[Paper] = []
        for item in items:
            title = self._first(item.get("title"))
            if not title:
                continue

            issue = self._build_issue_label(item)
            publish_date = self._extract_date(item)
            abstract_raw = item.get("abstract")
            authors = self._extract_authors(item.get("author") or [])

            paper = Paper(
                title=title,
                journal=journal,
                url=item.get("URL") or "",
                doi=item.get("DOI"),
                abstract=self._clean_abstract(abstract_raw),
                authors=authors,
                publish_date=publish_date,
                issue=issue,
                source="crossref",
            )
            papers.append(paper)

        return self.select_issue_batch(papers)

    def _build_issue_label(self, item: dict) -> Optional[str]:
        volume = (item.get("volume") or "").strip()
        issue = (item.get("issue") or "").strip()
        if volume and issue:
            return f"Volume {volume}, Issue {issue}"
        if volume:
            return f"Volume {volume}"
        return None

    def _extract_date(self, item: dict) -> Optional[str]:
        date_sources = [
            item.get("published-print"),
            item.get("published-online"),
            item.get("issued"),
        ]
        for source in date_sources:
            if not source:
                continue
            date_parts = source.get("date-parts") or []
            if not date_parts:
                continue
            parts = date_parts[0]
            parts = (parts + [1, 1])[:3]
            year, month, day = parts
            return f"{year:04d}-{month:02d}-{day:02d}"
        return None

    def _extract_authors(self, authors_raw: List[dict]) -> List[str]:
        authors: List[str] = []
        for author in authors_raw:
            given = (author.get("given") or "").strip()
            family = (author.get("family") or "").strip()
            full = f"{given} {family}".strip()
            if full:
                authors.append(full)
        return authors

    def _clean_abstract(self, raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        text = html.unescape(raw)
        soup = BeautifulSoup(text, "html.parser")
        cleaned = soup.get_text(" ", strip=True)
        return cleaned or None

    @staticmethod
    def _first(value):
        if isinstance(value, list):
            return value[0] if value else None
        return value
