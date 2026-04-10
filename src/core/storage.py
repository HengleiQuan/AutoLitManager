import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

from src.core.paper import Paper


class PaperStore:
    """
    Lightweight local storage to keep deduplicated paper history.
    """

    def __init__(self, db_file: str = "data/db/papers.jsonl"):
        self.db_path = Path(db_file)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def load_all(self) -> Dict[str, dict]:
        if not self.db_path.exists():
            return {}
        records: Dict[str, dict] = {}
        with self.db_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                uid = obj.get("uid")
                if uid:
                    records[uid] = obj
        return records

    def upsert_many(self, papers: Iterable[Paper]) -> Dict[str, int]:
        existing = self.load_all()
        now = datetime.now().isoformat(timespec="seconds")
        inserted = 0
        updated = 0

        for paper in papers:
            uid = paper.uid()
            paper.added_at = paper.added_at or now
            payload = paper.to_dict()
            if uid in existing:
                existing[uid].update(payload)
                updated += 1
            else:
                existing[uid] = payload
                inserted += 1

        with self.db_path.open("w", encoding="utf-8") as f:
            for record in existing.values():
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return {"inserted": inserted, "updated": updated, "total": len(existing)}

    def load_by_uids(self, uids: List[str]) -> List[dict]:
        all_records = self.load_all()
        return [all_records[uid] for uid in uids if uid in all_records]
