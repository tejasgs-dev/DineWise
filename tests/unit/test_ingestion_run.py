"""End-to-end ingestion on a tiny CSV: load -> clean -> validate -> write."""

import io
import json

import pandas as pd
import pytest

from dinewise.config import Settings
from dinewise.ingestion import load, run
from dinewise.ingestion.load import MissingColumnsError, check_columns

from .test_clean import raw_row


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray .env
    return Settings(
        data_path=tmp_path / "out" / "restaurants.parquet",
        meta_path=tmp_path / "out" / "meta.json",
        raw_dir=tmp_path / "raw",
    )


@pytest.fixture
def raw_csv(tmp_path):
    rows = [
        raw_row(),
        raw_row(**{"listed_in(type)": "Delivery", "votes": "200"}),  # duplicate of the first
        raw_row(
            name="Spice Hub",
            address="3 Church Street",
            location="Church Street",
            cuisines="North Indian",
        ),
        raw_row(name="New Corner", address="9 HSR Layout", location="HSR", rate="NEW"),
    ]
    path = tmp_path / "zomato.csv"
    pd.DataFrame(rows).assign(phone="080 1234", menu_item="[]").to_csv(path, index=False)
    return path


def test_run_writes_parquet_and_meta(settings, raw_csv):
    stats = run.run(settings, raw_file=raw_csv, out=io.StringIO())
    assert stats.rows_out == 3 and stats.duplicates_removed == 1

    df = pd.read_parquet(settings.data_path)
    assert len(df) == 3 and df["id"].is_unique
    assert "phone" not in df.columns

    meta = json.loads(settings.meta_path.read_text(encoding="utf-8"))
    assert meta["row_count"] == 3
    assert [loc["name"] for loc in meta["localities"]] == ["Church Street", "HSR", "Indiranagar"]
    assert {"name": "Italian", "count": 2} in meta["cuisines"]
    assert meta["budget_thresholds"] == {"low_max": 400, "medium_max": 1000}
    assert run.read_build_id(settings.data_path) == meta["build_id"]  # DATA-39


def test_build_id_is_deterministic(settings, raw_csv):
    run.run(settings, raw_file=raw_csv, out=io.StringIO())
    first = run.read_build_id(settings.data_path)
    run.run(settings, raw_file=raw_csv, out=io.StringIO())
    assert run.read_build_id(settings.data_path) == first


def test_no_temp_files_left_behind(settings, raw_csv):
    run.run(settings, raw_file=raw_csv, out=io.StringIO())
    leftovers = [p.name for p in settings.data_path.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_failed_write_keeps_previous_outputs(settings, raw_csv, monkeypatch):  # DATA-38
    run.run(settings, raw_file=raw_csv, out=io.StringIO())
    before = settings.data_path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(run.pq, "write_table", boom)
    with pytest.raises(OSError):
        run.run(settings, raw_file=raw_csv, out=io.StringIO())
    assert settings.data_path.read_bytes() == before


def test_too_many_dropped_rows_fails(settings, tmp_path):  # DATA-40
    rows = [raw_row(name=f"P{i}", address=f"{i} Road") for i in range(3)] + [raw_row(name="")] * 2
    path = tmp_path / "bad.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    with pytest.raises(run.IngestionError, match="dropped"):
        run.run(settings, raw_file=path, out=io.StringIO(), max_drop_share=0.2)


def test_missing_columns_fail_fast(tmp_path):  # DATA-35
    path = tmp_path / "broken.csv"
    pd.DataFrame([raw_row()]).drop(columns=["rate", "cuisines"]).to_csv(path, index=False)
    with pytest.raises(MissingColumnsError) as excinfo:
        load.read_raw(path)
    assert excinfo.value.missing == ["rate", "cuisines"]


def test_extra_columns_are_ignored():  # DATA-36
    check_columns(pd.DataFrame([{**raw_row(), "surprise": "x"}]))


def test_offline_without_local_file_errors(settings):  # DATA-34
    with pytest.raises(FileNotFoundError, match="--offline"):
        load.ensure_raw_file(settings, offline=True)


def test_cli_returns_error_code_on_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "broken.csv"
    pd.DataFrame([raw_row()]).drop(columns=["rate"]).to_csv(path, index=False)
    code = run.main(
        [
            "--raw-file",
            str(path),
            "--data-path",
            str(tmp_path / "o.parquet"),
            "--meta-path",
            str(tmp_path / "m.json"),
        ]
    )
    assert code == 1
    assert "missing required columns: rate" in capsys.readouterr().err
