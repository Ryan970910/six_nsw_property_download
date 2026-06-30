from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DownloadConfig:
    base_csv_url: str = "http://maps.six.nsw.gov.au/csv/current/property"
    request_timeout_seconds: int = 30
    retries: int = 3
    backoff_seconds: float = 1.0
