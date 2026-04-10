import re
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from src.api.elsevier_client import ElsevierApiClient
from src.analysis.figure_prioritizer import FigurePrioritizer
from src.core.paper import Paper


class ElsevierContentCollector:
    NS = {
        "article": "http://www.elsevier.com/xml/svapi/article/dtd",
        "ce": "http://www.elsevier.com/xml/common/dtd",
        "dc": "http://purl.org/dc/elements/1.1/",
        "prism": "http://prismstandard.org/namespaces/basic/2.0/",
    }

    def __init__(self, cfg: dict, run_dir: Path):
        self.cfg = cfg or {}
        self.run_dir = run_dir
        self.media_dir = run_dir / "article_media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.client = ElsevierApiClient(
            api_key=self.cfg.get("api_key"),
            insttoken=self.cfg.get("insttoken"),
        )

    def collect(self, papers: List[Paper]) -> Dict[str, int]:
        if not self.cfg.get("enabled", True):
            return self._stats(0, 0, 0, 0, 0, 0)

        metadata_updated = 0
        papers_with_figures = 0
        images_saved = 0
        attempted = 0
        skipped = 0

        for paper in papers:
            if not paper.doi:
                skipped += 1
                continue
            attempted += 1
            try:
                saved = self._collect_one(paper)
            except Exception:
                continue
            metadata_updated += 1
            if saved:
                papers_with_figures += 1
                images_saved += saved

        return self._stats(1, attempted, metadata_updated, papers_with_figures, images_saved, skipped)

    def _collect_one(self, paper: Paper) -> int:
        json_payload = self.client.retrieve_article_json(paper.doi)
        xml_payload = self.client.retrieve_article_xml(paper.doi)

        response_json = json_payload.get("full-text-retrieval-response", {})
        object_entries = ((response_json.get("objects") or {}).get("object") or [])
        object_map = {
            str(obj.get("@ref") or "").strip(): obj for obj in object_entries if isinstance(obj, dict)
        }

        root = ET.fromstring(xml_payload)
        abstract = self._extract_abstract(root)
        if abstract:
            paper.abstract = abstract

        authors = self._extract_authors(root)
        if authors:
            paper.authors = authors

        figures = self._extract_figures(root, object_map, paper)
        if figures:
            paper.figures = figures
        return len(figures)

    def _extract_abstract(self, root: ET.Element) -> Optional[str]:
        abstract_node = root.find(".//dc:description", self.NS)
        if abstract_node is None:
            abstract_node = root.find(".//ce:abstract", self.NS)
        if abstract_node is None:
            return None
        return self._clean_text(" ".join(abstract_node.itertext()))

    def _extract_authors(self, root: ET.Element) -> List[str]:
        creators = root.findall(".//dc:creator", self.NS)
        cleaned = [self._clean_text(node.text or "") for node in creators if (node.text or "").strip()]
        return cleaned

    def _extract_figures(
        self,
        root: ET.Element,
        object_map: Dict[str, dict],
        paper: Paper,
    ) -> List[Dict[str, str]]:
        max_images = int(self.cfg.get("max_images_per_paper", 3))
        candidates: List[Dict[str, str]] = []
        for idx, figure in enumerate(root.findall(".//ce:figure", self.NS), start=1):
            label = self._clean_text(" ".join((figure.findtext("ce:label", default="", namespaces=self.NS) or "").split()))
            caption_node = figure.find("ce:caption", self.NS)
            caption = self._clean_text(" ".join(caption_node.itertext())) if caption_node is not None else ""
            locator = ""
            link_node = figure.find("ce:link", self.NS)
            if link_node is not None:
                locator = str(link_node.attrib.get("locator") or "").strip()

            object_info = object_map.get(locator)
            if not object_info:
                continue
            url = str(object_info.get("$") or "").strip()
            mimetype = str(object_info.get("@mimetype") or "image/jpeg").strip()
            if not url.startswith("http"):
                continue
            candidates.append(
                {
                    "label": label,
                    "caption": " ".join(x for x in [label, caption] if x).strip(),
                    "source_url": url,
                    "mimetype": mimetype,
                    "source_index": str(idx),
                }
            )

        selected = FigurePrioritizer.prioritize(candidates, limit=max_images)
        if not selected:
            return []

        figures: List[Dict[str, str]] = []
        paper_dir = self.media_dir / self._safe_name(paper.uid())[:48]
        paper_dir.mkdir(parents=True, exist_ok=True)

        for display_index, figure in enumerate(selected, start=1):
            url = str(figure.get("source_url") or "").strip()
            mimetype = str(figure.get("mimetype") or "image/jpeg").strip()
            ext = self._ext_from_mimetype(mimetype)
            out_path = paper_dir / f"figure_{display_index:02d}.{ext}"
            content = self.client.download_object(url)
            out_path.write_bytes(content)
            figures.append(
                {
                    "path": out_path.relative_to(self.run_dir).as_posix(),
                    "label": str(figure.get("label") or ""),
                    "caption": str(figure.get("caption") or ""),
                    "source_url": url,
                    "role_hint": str(figure.get("display_role") or figure.get("role_hint") or ""),
                    "role_hint_cn": str(figure.get("display_role_cn") or figure.get("role_hint_cn") or ""),
                    "selection_reason": str(figure.get("selection_reason") or ""),
                    "priority_rank": str(figure.get("priority_rank") or display_index),
                    "source_index": str(figure.get("source_index") or ""),
                }
            )
        return figures

    @staticmethod
    def _ext_from_mimetype(mimetype: str) -> str:
        if "png" in mimetype.lower():
            return "png"
        if "svg" in mimetype.lower():
            return "svg"
        return "jpg"

    @staticmethod
    def _safe_name(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "paper"

    @staticmethod
    def _clean_text(text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        return text

    @staticmethod
    def _stats(
        enabled: int,
        attempted: int,
        metadata_updated: int,
        papers_with_figures: int,
        images_saved: int,
        skipped: int,
    ) -> Dict[str, int]:
        return {
            "enabled": enabled,
            "papers_attempted": attempted,
            "metadata_updated": metadata_updated,
            "papers_with_figures": papers_with_figures,
            "images_saved": images_saved,
            "papers_skipped": skipped,
        }
