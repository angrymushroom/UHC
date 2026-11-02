"""Shared download, provenance and JSONL utilities. Configuration is read from config.toml."""
import csv
import gzip
import hashlib
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import ijson
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SETTINGS = tomllib.loads((ROOT / "config.toml").read_text())

def now():
    """Return an ISO UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()

def url_id(url):

    """Return SHA256 of the full URL, without changing it or hashing file contents."""
    return hashlib.sha256(url.encode()).hexdigest()

def read_csv(path):
    """Read CSV rows as dictionaries; return an empty list if the file is absent."""
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))

def save(path, value):
    """Write CSV or JSON to a temporary path, then replace the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("w", newline="") as handle:
        if path.suffix == ".csv":
            writer = csv.DictWriter(handle, fieldnames=list(value[0]))
            writer.writeheader()
            writer.writerows(value)
        else:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    temporary.replace(path)

def local_name(name, identity):

    """Build a safe filename using a readable name and URL identifier."""
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", Path(unquote(name)).name)
    if clean.endswith(".json.gz"):
        suffix = ".json.gz"
    else:
        suffix = ".json"
    shortened = clean[:120]
    filename = shortened.strip(".") or "file"
    if not shortened.endswith(suffix):
        filename += suffix
    return f"{identity}_{filename}"

def validate_json(path):
    """Parse JSON/GZ through EOF; raise for malformed JSON or truncated gzip."""
    with path.open("rb") as handle:

        compressed = handle.read(2) == b"\x1f\x8b"
    if compressed:
        stream = gzip.open(path, "rb")
    else:
        stream = path.open("rb")
    with stream:
        try:

            for event in ijson.parse(stream):
                pass
        except ijson.JSONError as error:
            raise ValueError(f"Invalid JSON: {error}") from error

def session():

    """Create an HTTP session with bounded retries for connections and temporary statuses."""
    client = requests.Session()
    retries = Retry(total=3, read=0, backoff_factor=1, status_forcelist=[408, 429, 500, 502, 503, 504])
    client.mount("https://", HTTPAdapter(max_retries=retries))

    client.headers.update({"Accept-Encoding": "identity", "User-Agent": "UHC-DE-Assignment/0.2"})
    return client

def download(client, url, path, max_bytes):
    """Stream to .part, validate size and JSON/GZ, then rename. Remove partial output on failure; retries restart."""
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(path.name + ".part")
    try:

        with client.get(url, stream=True, timeout=(10, 60)) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Expected a complete HTTP 200 response")
            expected = response.headers.get("Content-Length")
            if expected and int(expected) > max_bytes:
                raise ValueError("File exceeds download limit")
            size = 0
            with temporary.open("wb") as handle:

                # Byte chunks are written directly; interrupted downloads are not resumed.
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("File exceeds download limit")
                    handle.write(chunk)
            if expected and response.headers.get("Content-Encoding", "identity") == "identity" and size != int(expected):
                raise ValueError("Incomplete response body")

        validate_json(temporary)

        temporary.replace(path)
        return size
    finally:

        temporary.unlink(missing_ok=True)

def error_message(error):

    """Redact query parameters from URLs in recorded error messages."""
    return re.sub(r"(https://[^\s?]+)\?[^\s]+", r"\1?<redacted>", str(error))

def file_references(structure):
    """Return original reference occurrences from one reporting_structure; reject malformed references."""
    references = []
    for field, kind in [("in_network_files", "in_network"), ("out_of_network_files", "out_of_network"),
                        ("allowed_amount_file", "out_of_network")]:
        items = structure.get(field, [])
        if field == "allowed_amount_file" and items:
            items = [items]
        if not isinstance(items, list):
            raise ValueError(f"Expected a list of references: {field}")
        for position, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("location"), str) or not item["location"]:
                raise ValueError(f"Missing file URL: {field}")
            references.append((field, position, kind, item))
    return references

def write_raw_row(handle, row):
    """Write one Decimal-preserving JSON line; reject serialized rows over 8 MiB after object construction."""
    import simplejson

    data = simplejson.dumps(row, use_decimal=True, ensure_ascii=False,
                            allow_nan=False, separators=(",", ":")).encode("utf-8")

    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Raw record exceeds 8 MiB; split this source object more finely before loading")
    handle.write(data + b"\n")

def checked_download(row):
    """Copy a ledger row with local existence/size checks; reject a mismatched URL identifier."""
    result = row.copy()
    path = Path(row["local_path"])
    result["local_file_exists"] = path.is_file()
    result["observed_file_size_bytes"] = path.stat().st_size if path.is_file() else None
    result["checked_at"] = now()
    if row["file_id"] != url_id(row["source_url"]):
        raise ValueError("Download URL and file_id disagree")
    return result
