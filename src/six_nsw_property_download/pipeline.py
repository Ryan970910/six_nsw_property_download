from __future__ import annotations

import csv
import logging
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from pathlib import Path

from .db import DEFAULT_PROPERTY_TABLE, PostgresConfig, upload_normalized_rows
from .downloader import MissingCsvError, PropertyCsvDownloader
from .transform import OUTPUT_COLUMNS, normalize_rows


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineResult:
    written_rows: int
    downloaded_files: int
    skipped_missing: int
    failed: int


@dataclass(frozen=True)
class DirectUploadResult:
    uploaded_rows: int
    staged_rows: int
    upload_batches: int
    downloaded_files: int
    skipped_missing: int
    failed: int


def write_normalized_csv(
    propids: Iterable[int],
    output_path: Path,
    *,
    raw_dir: Path | None = None,
    limit: int | None = None,
    skipped_path: Path | None = None,
    failed_path: Path | None = None,
    workers: int = 1,
    progress_interval: int = 1000,
) -> PipelineResult:
    progress_interval = max(1, progress_interval)
    logger.info("csv_pipeline_start output=%s workers=%s", output_path, workers)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)
    if skipped_path:
        skipped_path.parent.mkdir(parents=True, exist_ok=True)
    if failed_path:
        failed_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    downloaded_files = 0
    skipped_missing = 0
    failed = 0
    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()

        skipped_file = skipped_path.open("w", newline="", encoding="utf-8") if skipped_path else None
        failed_file = failed_path.open("w", newline="", encoding="utf-8") if failed_path else None
        skipped_writer = csv.DictWriter(skipped_file, fieldnames=["propid", "url", "status_code", "reason"]) if skipped_file else None
        failed_writer = csv.DictWriter(failed_file, fieldnames=["propid", "error"]) if failed_file else None
        if skipped_writer:
            skipped_writer.writeheader()
        if failed_writer:
            failed_writer.writeheader()

        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            pending: dict[Future, int] = {}
            propid_iter = iter(propids)
            exhausted = False

            while pending or not exhausted:
                while not exhausted and len(pending) < max(1, workers) * 4:
                    try:
                        propid = int(next(propid_iter))
                    except StopIteration:
                        exhausted = True
                        break
                    pending[executor.submit(download_one, propid)] = propid
                    logger.info("download_queued propid=%s pending=%s", propid, len(pending))

                for future in as_completed(pending):
                    propid = pending.pop(future)
                    try:
                        downloaded = future.result()
                    except MissingCsvError as exc:
                        skipped_missing += 1
                        logger.info("download_skipped_missing propid=%s skipped_missing=%s", exc.propid, skipped_missing)
                        if skipped_writer:
                            skipped_writer.writerow(
                                {
                                    "propid": exc.propid,
                                    "url": exc.url,
                                    "status_code": exc.status_code or "",
                                    "reason": "missing_csv",
                                }
                            )
                        break
                    except Exception as exc:
                        failed += 1
                        logger.error("download_failed_recorded propid=%s failed=%s error=%s", propid, failed, exc)
                        if failed_writer:
                            failed_writer.writerow({"propid": propid, "error": str(exc)})
                            break
                        raise

                    downloaded_files += 1
                    logger.info("transform_start propid=%s downloaded_files=%s", downloaded.propid, downloaded_files)
                    if raw_dir:
                        (raw_dir / f"{downloaded.propid}.csv").write_text(downloaded.text, encoding="utf-8")

                    normalized = normalize_rows(downloaded.text, source_file=downloaded.url)
                    logger.info("transform_success propid=%s normalized_rows=%s", downloaded.propid, len(normalized))
                    for row in normalized:
                        writer.writerow(row)
                        written += 1
                    if downloaded_files <= 10 or downloaded_files % progress_interval == 0:
                        logger.info(
                            "csv_pipeline_progress downloaded_files=%s written_rows=%s skipped_missing=%s failed=%s",
                            downloaded_files,
                            written,
                            skipped_missing,
                            failed,
                        )
                    break

                if limit is not None and written >= limit:
                    for future in pending:
                        future.cancel()
                    break

        if skipped_file:
            skipped_file.close()
        if failed_file:
            failed_file.close()

    logger.info(
        "csv_pipeline_complete written_rows=%s downloaded_files=%s skipped_missing=%s failed=%s",
        written,
        downloaded_files,
        skipped_missing,
        failed,
    )
    return PipelineResult(
        written_rows=written,
        downloaded_files=downloaded_files,
        skipped_missing=skipped_missing,
        failed=failed,
    )


def download_and_upload_to_db(
    propids: Iterable[int],
    db_config: PostgresConfig,
    *,
    target_table: str = DEFAULT_PROPERTY_TABLE,
    skipped_path: Path | None = None,
    failed_path: Path | None = None,
    workers: int = 1,
    upload_workers: int = 1,
    upload_batch_rows: int = 10_000,
    progress_interval: int = 1000,
) -> DirectUploadResult:
    progress_interval = max(1, progress_interval)
    upload_workers = max(1, upload_workers)
    upload_batch_rows = max(1, upload_batch_rows)
    logger.info(
        "direct_upload_pipeline_start target_table=%s download_workers=%s upload_workers=%s upload_batch_rows=%s",
        target_table,
        workers,
        upload_workers,
        upload_batch_rows,
    )
    if skipped_path:
        skipped_path.parent.mkdir(parents=True, exist_ok=True)
    if failed_path:
        failed_path.parent.mkdir(parents=True, exist_ok=True)

    downloaded_files = 0
    skipped_missing = 0
    failed = 0
    staged_rows = 0
    uploaded_rows = 0
    upload_batches = 0

    skipped_file = skipped_path.open("w", newline="", encoding="utf-8") if skipped_path else None
    failed_file = failed_path.open("w", newline="", encoding="utf-8") if failed_path else None
    skipped_writer = csv.DictWriter(skipped_file, fieldnames=["propid", "url", "status_code", "reason"]) if skipped_file else None
    failed_writer = csv.DictWriter(failed_file, fieldnames=["propid", "error"]) if failed_file else None
    if skipped_writer:
        skipped_writer.writeheader()
    if failed_writer:
        failed_writer.writeheader()

    def submit_upload_batch(
        upload_executor: ThreadPoolExecutor,
        upload_futures: dict[Future, tuple[int, int]],
        batch: list[dict[str, object]],
    ) -> None:
        nonlocal upload_batches, staged_rows
        if not batch:
            return
        upload_batches += 1
        batch_number = upload_batches
        batch_size = len(batch)
        staged_rows += batch_size
        logger.info("upload_batch_submit batch=%s rows=%s staged_rows=%s", batch_number, batch_size, staged_rows)
        future = upload_executor.submit(upload_normalized_rows, db_config, batch, target_table=target_table)
        upload_futures[future] = (batch_number, batch_size)

    def collect_completed_uploads(
        upload_futures: dict[Future, tuple[int, int]],
        *,
        wait_for_one: bool = False,
    ) -> None:
        nonlocal uploaded_rows
        if not upload_futures:
            return
        if wait_for_one:
            done, _ = wait(upload_futures, return_when=FIRST_COMPLETED)
        else:
            done = {future for future in upload_futures if future.done()}
        for future in done:
            batch_number, batch_size = upload_futures.pop(future)
            inserted = future.result()
            uploaded_rows += inserted
            logger.info(
                "upload_batch_complete batch=%s batch_rows=%s inserted_rows=%s duplicate_or_conflict_rows=%s uploaded_rows=%s",
                batch_number,
                batch_size,
                inserted,
                batch_size - inserted,
                uploaded_rows,
            )

    try:
        with (
            ThreadPoolExecutor(max_workers=max(1, workers)) as download_executor,
            ThreadPoolExecutor(max_workers=upload_workers) as upload_executor,
        ):
            pending: dict[Future, int] = {}
            upload_futures: dict[Future, tuple[int, int]] = {}
            upload_batch: list[dict[str, object]] = []
            propid_iter = iter(propids)
            exhausted = False

            while pending or not exhausted:
                while not exhausted and len(pending) < max(1, workers) * 4:
                    try:
                        propid = int(next(propid_iter))
                    except StopIteration:
                        exhausted = True
                        break
                    pending[download_executor.submit(download_one, propid)] = propid
                    logger.info("download_queued propid=%s pending=%s", propid, len(pending))

                for future in as_completed(pending):
                    propid = pending.pop(future)
                    try:
                        downloaded = future.result()
                    except MissingCsvError as exc:
                        skipped_missing += 1
                        logger.info("download_skipped_missing propid=%s skipped_missing=%s", exc.propid, skipped_missing)
                        if skipped_writer:
                            skipped_writer.writerow(
                                {
                                    "propid": exc.propid,
                                    "url": exc.url,
                                    "status_code": exc.status_code or "",
                                    "reason": "missing_csv",
                                }
                            )
                        break
                    except Exception as exc:
                        failed += 1
                        logger.error("download_failed_recorded propid=%s failed=%s error=%s", propid, failed, exc)
                        if failed_writer:
                            failed_writer.writerow({"propid": propid, "error": str(exc)})
                            break
                        raise

                    downloaded_files += 1
                    logger.info("transform_start propid=%s downloaded_files=%s", downloaded.propid, downloaded_files)
                    normalized = normalize_rows(downloaded.text, source_file=downloaded.url)
                    logger.info("transform_success propid=%s normalized_rows=%s", downloaded.propid, len(normalized))
                    upload_batch.extend(normalized)
                    logger.info(
                        "upload_batch_buffered propid=%s buffered_rows=%s batch_target_rows=%s",
                        downloaded.propid,
                        len(upload_batch),
                        upload_batch_rows,
                    )
                    while len(upload_batch) >= upload_batch_rows:
                        batch_to_upload = upload_batch[:upload_batch_rows]
                        upload_batch = upload_batch[upload_batch_rows:]
                        submit_upload_batch(upload_executor, upload_futures, batch_to_upload)
                        collect_completed_uploads(upload_futures)
                        if len(upload_futures) >= upload_workers * 2:
                            collect_completed_uploads(upload_futures, wait_for_one=True)
                    if downloaded_files <= 10 or downloaded_files % progress_interval == 0:
                        logger.info(
                            "direct_upload_pipeline_progress downloaded_files=%s buffered_rows=%s upload_batches=%s staged_rows=%s uploaded_rows=%s skipped_missing=%s failed=%s",
                            downloaded_files,
                            len(upload_batch),
                            upload_batches,
                            staged_rows,
                            uploaded_rows,
                            skipped_missing,
                            failed,
                        )
                    break
                collect_completed_uploads(upload_futures)

            submit_upload_batch(upload_executor, upload_futures, upload_batch)
            upload_batch = []
            while upload_futures:
                collect_completed_uploads(upload_futures, wait_for_one=True)
    finally:
        if skipped_file:
            skipped_file.close()
        if failed_file:
            failed_file.close()

    logger.info(
        "direct_upload_pipeline_complete uploaded_rows=%s staged_rows=%s upload_batches=%s downloaded_files=%s skipped_missing=%s failed=%s",
        uploaded_rows,
        staged_rows,
        upload_batches,
        downloaded_files,
        skipped_missing,
        failed,
    )
    return DirectUploadResult(
        uploaded_rows=uploaded_rows,
        staged_rows=staged_rows,
        upload_batches=upload_batches,
        downloaded_files=downloaded_files,
        skipped_missing=skipped_missing,
        failed=failed,
    )


def download_one(propid: int):
    return PropertyCsvDownloader().download(propid)
