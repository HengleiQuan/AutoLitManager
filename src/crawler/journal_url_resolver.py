import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests


class JournalUrlResolver:
    CROSSREF_JOURNALS_API = "https://api.crossref.org/journals"

    def __init__(self, defaults: dict):
        self.defaults = defaults
        self._cache: Dict[str, dict] = {}

    def resolve_many(self, journals: List[dict]) -> List[dict]:
        resolved = []
        for idx, item in enumerate(journals, start=1):
            if not bool(item.get("enabled", True)):
                continue
            cfg = self.resolve_one(item, idx)
            resolved.append(cfg)
        return resolved

    def resolve_one(self, item: dict, index: int) -> dict:
        url = (item.get("url") or "").strip()
        if not url:
            raise ValueError(f"journals[{index}] missing `url`.")

        parsed = urlparse(url)
        source_type = (item.get("source_type") or "").strip().lower()
        if not source_type:
            source_type = self._guess_source_type(parsed)

        issue_index = int(item.get("issue_index", self.defaults.get("issue_index", 0)))
        min_issue_size = int(
            item.get("min_issue_size", self.defaults.get("min_issue_size", 1))
        )
        max_items = int(item.get("max_items", self.defaults.get("max_items", 80)))
        rows = int(item.get("rows", self.defaults.get("rows", 300)))

        if source_type == "rss":
            name = item.get("name") or self._guess_name_from_url(url)
            return {
                "id": self._to_id(name),
                "name": name,
                "publisher": item.get("publisher", self._guess_publisher(parsed)),
                "source_type": "rss",
                "url": url,
                "issue_index": issue_index,
                "min_issue_size": min_issue_size,
                "max_items": max_items,
                "enabled": True,
            }

        if source_type == "crossref":
            name = item.get("name") or self._guess_name_from_url(url)
            issn = item.get("issn")
            score = 1.0
            if not issn:
                matched = self._resolve_issn_by_name(name)
                if not matched:
                    raise RuntimeError(
                        f"Could not map journal URL to Crossref ISSN: {url} "
                        f"(guessed_name={name})"
                    )
                issn = matched["issn"]
                name = matched["title"]
                score = matched["score"]

            return {
                "id": self._to_id(name),
                "name": name,
                "publisher": item.get("publisher", self._guess_publisher(parsed)),
                "source_type": "crossref",
                "issn": issn,
                "issue_index": issue_index,
                "min_issue_size": min_issue_size,
                "rows": rows,
                "max_items": max_items,
                "enabled": True,
                "home_url": url,
                "resolution_score": score,
            }

        raise ValueError(f"Unsupported source type for URL `{url}`: {source_type}")

    def _guess_source_type(self, parsed) -> str:
        path = (parsed.path or "").lower()
        host = (parsed.netloc or "").lower()
        if "rss" in path or path.endswith(".xml"):
            return "rss"
        if "sciencedirect.com" in host and "/journal/" in path:
            return "crossref"
        return "crossref"

    def _guess_publisher(self, parsed) -> str:
        host = (parsed.netloc or "").lower()
        if "sciencedirect.com" in host or "elsevier" in host:
            return "Elsevier"
        return "unknown"

    def _guess_name_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path or ""

        m = re.search(r"/journal/([^/?#]+)", path, flags=re.I)
        if m:
            slug = m.group(1)
        else:
            parts = [x for x in path.split("/") if x]
            slug = parts[-1] if parts else parsed.netloc

        slug = slug.replace("-", " ").replace("_", " ")
        slug = re.sub(r"\s+", " ", slug).strip()
        return self._smart_title(slug)

    def _resolve_issn_by_name(self, name: str) -> Optional[dict]:
        cache_key = self._norm(name)
        if cache_key in self._cache:
            return self._cache[cache_key]

        params = {"query": name, "rows": 20}
        resp = requests.get(self.CROSSREF_JOURNALS_API, params=params, timeout=45)
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
        if not items:
            return None

        best = None
        query_norm = self._norm(name)
        for item in items:
            title = (item.get("title") or "").strip()
            issn_list = item.get("ISSN") or []
            if not title or not issn_list:
                continue
            score = SequenceMatcher(None, query_norm, self._norm(title)).ratio()
            if best is None or score > best["score"]:
                best = {
                    "title": title,
                    "issn": issn_list[0],
                    "score": score,
                }

        if best:
            self._cache[cache_key] = best
        return best

    @staticmethod
    def _norm(text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"[^a-z0-9]+", "", text)
        return text

    @staticmethod
    def _to_id(name: str) -> str:
        x = name.lower().strip()
        x = re.sub(r"[^a-z0-9]+", "_", x)
        return x.strip("_") or "journal"

    @staticmethod
    def _smart_title(text: str) -> str:
        if not text:
            return "Unknown Journal"

        keep_lower = {"and", "or", "of", "in", "on", "for", "the", "a", "an"}
        keep_upper = {"ai", "ml", "fem", "pinn", "rve", "2d", "3d"}
        words = text.split()
        out = []
        for i, w in enumerate(words):
            wl = w.lower()
            if wl in keep_upper:
                out.append(wl.upper())
            elif i > 0 and wl in keep_lower:
                out.append(wl)
            else:
                out.append(wl.capitalize())
        return " ".join(out)
