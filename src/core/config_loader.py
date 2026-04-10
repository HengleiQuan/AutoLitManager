from pathlib import Path
import yaml


class ConfigLoader:
    def __init__(self, config_dir: str = "config"):
        self.config_dir = Path(config_dir)

    def load(self, name: str) -> dict:
        path = self.config_dir / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
