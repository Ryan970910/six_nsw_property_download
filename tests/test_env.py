from __future__ import annotations

import os

from six_nsw_property_download.env import load_env_file


def test_load_env_file_reads_quoted_values_without_overriding_existing(tmp_path) -> None:
    env_file = tmp_path / ".env.example"
    env_file.write_text(
        'PGHOST="100.124.134.29"\n'
        "PGPORT=5432\n"
        'PGUSER="banner17"\n'
        "EXISTING=from_file\n"
        "# comment\n",
        encoding="utf-8",
    )
    os.environ["EXISTING"] = "from_env"

    try:
        load_env_file(env_file)

        assert os.environ["PGHOST"] == "100.124.134.29"
        assert os.environ["PGPORT"] == "5432"
        assert os.environ["PGUSER"] == "banner17"
        assert os.environ["EXISTING"] == "from_env"
    finally:
        for key in ("PGHOST", "PGPORT", "PGUSER", "EXISTING"):
            os.environ.pop(key, None)
