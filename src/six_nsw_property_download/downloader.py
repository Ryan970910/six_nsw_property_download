from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import DownloadConfig
from .urls import property_csv_url


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadedCsv:
    propid: int
    url: str
    text: str


class MissingCsvError(RuntimeError):
    def __init__(self, propid: int, url: str, status_code: int | None = None) -> None:
        self.propid = int(propid)
        self.url = url
        self.status_code = status_code
        status = f" HTTP {status_code}" if status_code else ""
        super().__init__(f"CSV not found for propid {propid}{status}: {url}")


class PropertyCsvDownloader:
    def __init__(self, config: DownloadConfig | None = None) -> None:
        self.config = config or DownloadConfig()

    def download(self, propid: int) -> DownloadedCsv:
        url = property_csv_url(propid, self.config)
        last_error: Exception | None = None
        logger.info("download_start propid=%s url=%s", propid, url)

        for attempt in range(1, self.config.retries + 1):
            try:
                logger.debug("download_attempt propid=%s attempt=%s url=%s", propid, attempt, url)
                request = Request(url, headers={"User-Agent": "six-nsw-property-download/0.1"})
                with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    text = response.read().decode(charset)
                logger.info("download_success propid=%s bytes=%s url=%s", propid, len(text.encode(charset)), url)
                return DownloadedCsv(propid=int(propid), url=url, text=text)
            except HTTPError as exc:
                if exc.code == 404:
                    logger.info("download_missing propid=%s status_code=%s url=%s", propid, exc.code, url)
                    raise MissingCsvError(int(propid), url, exc.code) from exc
                last_error = exc
                logger.warning(
                    "download_http_error propid=%s attempt=%s status_code=%s url=%s error=%s",
                    propid,
                    attempt,
                    exc.code,
                    url,
                    exc,
                )
                if attempt < self.config.retries:
                    time.sleep(self.config.backoff_seconds * attempt)
            except (URLError, TimeoutError) as exc:
                last_error = exc
                logger.warning(
                    "download_error propid=%s attempt=%s url=%s error=%s",
                    propid,
                    attempt,
                    url,
                    exc,
                )
                if attempt < self.config.retries:
                    time.sleep(self.config.backoff_seconds * attempt)

        logger.error("download_failed propid=%s url=%s", propid, url)
        raise RuntimeError(f"Failed to download CSV for propid {propid} from {url}") from last_error
