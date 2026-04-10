import re
from typing import List, Optional

import feedparser

from src.core.paper import Paper
from src.crawler.base import BaseCrawler


class RSSCrawler(BaseCrawler):
    DOI_PATTERNS = [
        re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+"),
    ]

    ISSUE_PATTERNS = [
        re.compile(r"(Volume\s+\d+\s*,\s*Issue\s+\d+)", re.IGNORECASE),
        re.compile(r"(Vol\.\s*\d+\s*,\s*No\.\s*\d+)", re.IGNORECASE),
    ]

    def fetch(self) -> List[Paper]:
        url = self.journal_config["url"]
        journal = self.journal_config["name"]
        feed = feedparser.parse(url)
        papers: List[Paper] = []

        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            if not title:
                continue

            summary = entry.get("summary") or entry.get("description") or ""
            doi = self._extract_doi(entry, summary)
            publish_date = (
                entry.get("published")
                or entry.get("updated")
                or entry.get("created")
            )
            authors = self._extract_authors(entry)
            issue = self._extract_issue(entry, title, summary)

            paper = Paper(
                title=title,
                journal=journal,
                url=entry.get("link") or "",
                abstract=summary.strip() or None,
                doi=doi,
                publish_date=publish_date,
                authors=authors,
                issue=issue,
                source="rss",
            )
            papers.append(paper)

        return self.select_issue_batch(papers)

    def _extract_doi(self, entry: dict, fallback_text: str) -> Optional[str]:
        direct_fields = [
            entry.get("dc_identifier"),
            entry.get("doi"),
            entry.get("prism_doi"),
        ]
        for raw in direct_fields:
            doi = self._normalize_doi(raw)
            if doi:
                return doi

        links = entry.get("links", []) or []
        for link in links:
            href = link.get("href")
            doi = self._normalize_doi(href)
            if doi:
                return doi

        return self._normalize_doi(fallback_text)

    def _extract_issue(self, entry: dict, title: str, summary: str) -> Optional[str]:
        candidates = [
            entry.get("prism_volume") and entry.get("prism_number"),
            entry.get("issue"),
            entry.get("tags"),
            title,
            summary,
        ]
        if entry.get("prism_volume") and entry.get("prism_number"):
            return (
                f"Volume {entry.get('prism_volume')}, "
                f"Issue {entry.get('prism_number')}"
            )
        for item in candidates:
            text = ""
            if isinstance(item, list):
                text = " ".join(
                    str(x.get("term", "")) if isinstance(x, dict) else str(x)
                    for x in item
                )
            elif item:
                text = str(item)
            if not text:
                continue
            for pattern in self.ISSUE_PATTERNS:
                match = pattern.search(text)
                if match:
                    return match.group(1).strip()
        return None

    def _extract_authors(self, entry: dict) -> List[str]:
        raw_authors = entry.get("authors", []) or []
        names = []
        for author in raw_authors:
            name = author.get("name") if isinstance(author, dict) else str(author)
            if name:
                names.append(name.strip())
        return names

    def _normalize_doi(self, raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        text = str(raw).strip()
        text = text.replace("https://doi.org/", "").replace("http://doi.org/", "")
        for pattern in self.DOI_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(0)
        return None
