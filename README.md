# NSW SIX Property Sales Downloader

Downloads per-property CSV files from NSW SIX, normalizes them to the target PostgreSQL table shape, and writes import-ready CSV output.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Run a Sample Propid

```powershell
six-property-download sample --propid 3113491 --output data\normalized\sample_3113491.csv
```

## Run From PostgreSQL

The CLI automatically reads `.env` first and then `.env.example` from the project folder. Existing PowerShell environment variables take priority.

Use either a DSN:

```powershell
$env:PGDSN = "postgresql://user:password@host:5432/database"
six-property-download db --output data\normalized\property_sales.csv --skipped-output data\normalized\skipped.csv --failed-output data\normalized\failed.csv --workers 16
```

Or individual PostgreSQL settings:

```powershell
$env:PGHOST = "localhost"
$env:PGPORT = "5432"
$env:PGDATABASE = "your_database"
$env:PGUSER = "your_user"
$env:PGPASSWORD_KEYRING_SERVICE = "banner17"
six-property-download db --output data\normalized\property_sales.csv --workers 16
```

If `PGPASSWORD` is not set, the app reads the password from Windows Credential Manager through `keyring`. The default credential service name is `banner17`, and the credential username defaults to `PGUSER`.

If your Windows credential uses a different username, set:

```powershell
$env:PGPASSWORD_KEYRING_USERNAME = "credential_username"
```

You can test the database connector and preview property IDs before downloading:

```powershell
six-property-download db-test --preview-limit 10
```

With a custom query:

```powershell
six-property-download db --propid-query "select propid from your_schema.your_table where propid is not null" --output data\normalized\property_sales.csv
```

The connector streams IDs with a server-side cursor, so it is designed for large tables such as 2,000,000 property IDs.

By default, the database commands read distinct IDs from:

`propdb_staging.nsw_property_sales_all_history.url_property_id`

You can override this with `--propid-table`, `--propid-column`, or `--propid-query`.

## Upload Normalized CSV To PostgreSQL

After downloading and transforming rows, upload the normalized CSV into the same table:

```powershell
six-property-download upload --input data\normalized\property_sales.csv
```

The upload target defaults to:

`propdb_staging.nsw_property_sales_all_history`

You can override it with `--target-table`.

During upload, `imported_at` is set dynamically to the Hong Kong timestamp when the upload command starts. Any `imported_at` value already present in the CSV is ignored.

For a large run, start with `--workers 8` or `--workers 16`. Direct parallel CSV requests are much lighter than opening Chrome tabs, easier to resume, and safer for a 2,000,000-property workload.

## Logging And Progress

Use `--log-file` to keep a detailed progress log:

```powershell
.\.venv\Scripts\six-property-download.exe db-upload --workers 16 --upload-workers 1 --upload-batch-rows 10000 --log-file logs\property_download.log --log-level INFO --progress-interval 1000
```

The log includes database ID streaming, each property ID queued for download, download start/success/missing/failure, transform start/success, upload batch submission/completion, upload copy progress, insert progress, duplicate skips, and final totals.

## Output Columns

The output matches the provided `table_info` attachment:

`id, url_property_id, address, sale_price, sale_date, area_sqm, strata_type, is_multi_property_sale, property_number, dealing_number, extraction_date, unit_num, house_num, street_name, suburb, state, postcode, source_file, downloaded_at, imported_at`

`id` and `imported_at` are left blank so PostgreSQL defaults/import logic can populate them if needed.
