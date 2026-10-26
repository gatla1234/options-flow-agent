from __future__ import annotations

import os

from dotenv import load_dotenv
from thetadata import ThetaClient

load_dotenv()

_client: ThetaClient | None = None


def get_client() -> ThetaClient:
    global _client
    if _client is None:
        api_key = os.getenv("THETADATA_API_KEY")
        if not api_key:
            raise RuntimeError("THETADATA_API_KEY is missing from .env")
        _client = ThetaClient(api_key=api_key)
    return _client
