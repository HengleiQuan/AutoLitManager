from src.crawler.base import BaseCrawler
from src.crawler.crossref_crawler import CrossrefCrawler
from src.crawler.rss_crawler import RSSCrawler


def build_crawler(journal_config: dict) -> BaseCrawler:
    source_type = (journal_config.get("source_type") or "").lower()
    if source_type == "rss":
        return RSSCrawler(journal_config)
    if source_type == "crossref":
        return CrossrefCrawler(journal_config)
    raise ValueError(f"Unsupported source_type: {source_type}")
