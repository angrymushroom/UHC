"""Acquisition tests with mocked HTTP and temporary files; no external connections."""
import gzip
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from src import ingest, io_utils

BASE = "https://transparency-in-coverage.uhc.com/"

def client_for(chunks):
    """Create a mocked HTTP session yielding the supplied chunks."""
    response = Mock(status_code=200, headers={})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = iter(chunks)
    client = Mock()
    client.get.return_value = response
    return client

def test_selection_preserves_order_and_deduplicates(tmp_path):
    """Use temporary JSON to check Index suffix filtering, full-URL deduplication, original order and failure when too few matches exist."""
    snapshot = tmp_path / "listing.json"
    snapshot.write_text(json.dumps({"blobs": [
        {"name": "price.json.gz", "downloadUrl": BASE + "price"},
        {"name": "z_index.json", "downloadUrl": BASE + "z"},
        {"name": "z_index.json", "downloadUrl": BASE + "z"},
        {"name": "a_index.json", "downloadUrl": BASE + "a"}]}))
    selected = ingest.select_indexes(snapshot, 2)
    assert [r["original_filename"] for r in selected] == ["z_index.json", "a_index.json"]
    assert [r["selection_rank"] for r in selected] == [1, 2]
    with pytest.raises(ValueError):
        ingest.select_indexes(snapshot, 3)

def test_cache_deduplication_and_frozen_selection(tmp_path, monkeypatch):
    """Mock duplicate tasks and caching: request each URL once, reuse a fixed selection, and redownload when the local size changes."""
    monkeypatch.setattr(ingest, "DATA", tmp_path)
    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    item = {"selection_rank": 1, "source_url": BASE + "index", "original_filename": "sample_index.json"}
    payload = b'{"reporting_structure":[]}'
    client = client_for([payload])
    rows, first = ingest.fetch_files(client, [item, item], "index")
    assert first == {"requested": 1, "downloaded": 1, "cached": 0, "failed": 0}
    client.get.assert_called_once()
    io_utils.save(tmp_path / "metadata/discovery/selected_indexes.csv", [item])
    monkeypatch.setattr("requests.Session.get", Mock(side_effect=AssertionError("Unexpected network request")))

    assert ingest.run(1)["indexes"]["cached"] == 1
    Path(rows[0]["local_path"]).write_bytes(b"short")
    _, repaired = ingest.fetch_files(client_for([payload]), [item], "index")
    assert repaired["downloaded"] == 1

@pytest.mark.parametrize("case", ["success", "invalid_json", "too_large", "interrupted"])
def test_download_publishes_only_complete_valid_files(tmp_path, case):
    """Simulate success, invalid JSON, excessive size and interruption. Publish only complete files and remove failed .part output."""
    payload = b'{"reporting_structure":[]}'
    if case == "invalid_json":
        payload = b"bad JSON"
    client = client_for([payload])
    if case == "interrupted":
        def interrupted():
            """Test-only generator: yield an incomplete JSON chunk, then raise to simulate an interrupted connection."""
            yield b'{"partial":'
            raise OSError("connection closed")
        client.get.return_value.iter_content.return_value = interrupted()
    path = tmp_path / "index.json"
    path.write_text("existing file")
    if case == "success":
        assert io_utils.download(client, BASE, path, 100) == len(payload)
        assert path.read_bytes() == payload
    else:
        with pytest.raises((ValueError, OSError)):
            io_utils.download(client, BASE, path, 5 if case == "too_large" else 100)
        assert path.read_text() == "existing file"
    assert not path.with_name(path.name + ".part").exists()

def test_gzip_validation_accepts_empty_array_and_rejects_truncation(tmp_path):
    """Write a valid empty-array GZ, then truncate it. Check that the empty array passes and the truncated file raises."""
    path = tmp_path / "pricing.gz"
    payload = gzip.compress(b'{"out_of_network":[]}')
    path.write_bytes(payload)
    io_utils.validate_json(path)
    path.write_bytes(payload[:-8])
    with pytest.raises((EOFError, OSError, ValueError)):
        io_utils.validate_json(path)

def test_failure_is_saved_without_signed_query(tmp_path, monkeypatch):
    """Simulate a failure containing a signed URL. Check that the ledger records failure without exposing query parameters in the message."""
    monkeypatch.setattr(ingest, "DATA", tmp_path)
    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    monkeypatch.setattr(ingest, "download", Mock(side_effect=ValueError("https://example.com/file?sig=secret")))
    rows, summary = ingest.fetch_files(Mock(), [{"source_url": BASE, "original_filename": "sample.json"}], "index")
    assert not rows and summary["failed"] == 1
    manifest = (tmp_path / "metadata/index_manifest.csv").read_text()
    assert "secret" not in manifest and "failed" in manifest

def test_fixed_pricing_selection_rejects_unreferenced_file(tmp_path, monkeypatch):
    """Mock a pricing URL not referenced by the Index. Check that validation raises before any pricing download begins."""
    monkeypatch.setattr(ingest, 'DATA', tmp_path)
    monkeypatch.setattr(ingest.time, 'sleep', lambda _: None)
    index = tmp_path / 'index.json'
    index.write_text(json.dumps({'reporting_structure': []}))
    io_utils.save(tmp_path / 'metadata/discovery/selected_indexes.csv', [
        {'source_url': BASE + 'index', 'original_filename': 'index.json'}])
    selection = tmp_path / 'pricing.csv'
    io_utils.save(selection, [{'source_url': BASE + 'unreferenced', 'file_type': 'in_network',
                             'original_filename': 'price.gz'}])
    mocked = Mock(return_value=([{'local_path': str(index)}], {'failed': 0}))
    monkeypatch.setattr(ingest, 'fetch_files', mocked)
    with pytest.raises(ValueError, match='not referenced'):
        ingest.run(1, pricing_selection=selection)
    assert mocked.call_count == 1
