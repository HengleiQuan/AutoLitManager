import os
from typing import Dict, Optional

import requests


class ElsevierApiClient:
    BASE_URL = "https://api.elsevier.com/content/article/doi/{doi}"

    def __init__(
        self,
        api_key: Optional[str] = None,
        insttoken: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("ELSEVIER_API_KEY")
        self.insttoken = insttoken or os.environ.get("ELSEVIER_INSTTOKEN")
        if not self.api_key:
            raise RuntimeError("Missing ELSEVIER_API_KEY.")

        self.session = requests.Session()
        self.session.headers.update({"X-ELS-APIKey": self.api_key})
        if self.insttoken:
            self.session.headers.update({"X-ELS-Insttoken": self.insttoken})

    def retrieve_article_json(self, doi: str) -> Dict:
        response = self.session.get(
            self.BASE_URL.format(doi=doi),
            headers={"Accept": "application/json"},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def retrieve_article_xml(self, doi: str) -> str:
        response = self.session.get(
            self.BASE_URL.format(doi=doi),
            headers={"Accept": "text/xml"},
            timeout=60,
        )
        response.raise_for_status()
        return response.text

    def download_object(self, url: str) -> bytes:
        response = self.session.get(url, timeout=60)
        response.raise_for_status()
        return response.content
