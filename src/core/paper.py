from dataclasses import asdict, dataclass, field
import hashlib
from typing import Dict, List, Optional


@dataclass
class Paper:
    title: str
    journal: str
    url: str

    doi: Optional[str] = None
    abstract: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    publish_date: Optional[str] = None
    issue: Optional[str] = None
    source: Optional[str] = None

    title_cn: Optional[str] = None
    one_line_summary_cn: Optional[str] = None
    categories: List[str] = field(default_factory=list)
    methods: List[str] = field(default_factory=list)
    applications: List[str] = field(default_factory=list)
    related_to_focus: Optional[bool] = None
    matched_keywords: List[str] = field(default_factory=list)
    value_score: Optional[int] = None
    value_reason: Optional[str] = None
    gemini_summary_cn: Optional[str] = None
    gemini_frontier_points: List[str] = field(default_factory=list)
    gemini_interest_reason: Optional[str] = None
    figures: List[Dict[str, str]] = field(default_factory=list)
    visual_summary_cn: Optional[str] = None
    model_flow_summary_cn: Optional[str] = None
    key_figure_points: List[str] = field(default_factory=list)
    process_flow_steps: List[str] = field(default_factory=list)
    generated_diagram_path: Optional[str] = None

    processed: bool = False
    added_at: Optional[str] = None

    def uid(self) -> str:
        """Stable ID used for de-duplication."""
        if self.doi:
            return self.doi.lower().strip()

        raw = f"{self.title.lower().strip()}::{self.journal.lower().strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["uid"] = self.uid()
        return data

    @staticmethod
    def from_dict(data: Dict) -> "Paper":
        data = dict(data)
        data.pop("uid", None)
        return Paper(**data)
