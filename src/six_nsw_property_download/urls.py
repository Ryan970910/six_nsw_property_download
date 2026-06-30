from __future__ import annotations

from .config import DownloadConfig


def property_csv_url(propid: int, config: DownloadConfig | None = None) -> str:
    """Build the same CSV URL as PropertySales.html for a property report."""
    cfg = config or DownloadConfig()
    folder = f"{(int(propid) // 1000) * 1000:08d}"
    return f"{cfg.base_csv_url}/{folder}/{int(propid)}.csv"
