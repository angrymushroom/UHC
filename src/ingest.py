"""Select and download UHC Index and pricing files; persist selections and download outcomes."""
import argparse
import json
import time
from pathlib import Path

import ijson
import requests

from src.io_utils import DATA, SETTINGS, download, error_message, local_name, now, read_csv, save, session, url_id
from src.io_utils import file_references

def select_indexes(snapshot, count=1000):
    """Select distinct Index URLs in listing order; raise if fewer than the requested count exist."""
    selected = []
    seen = set()
    with snapshot.open("rb") as handle:

        for item in ijson.items(handle, "blobs.item"):

            if not item["name"].endswith("_index.json"):
                continue
            url = item["downloadUrl"]

            if url in seen:
                continue

            seen.add(url)

            selected.append({"selection_rank": len(selected) + 1, "source_url": url,
                             "original_filename": item["name"], "listed_size_bytes": item.get("size")})

            if len(selected) == count:
                break
    if len(selected) != count:
        raise ValueError(f"Expected {count} Indexes, found {len(selected)} in {snapshot}; inspect the fresh listing before continuing")
    return selected

def select_pricing(snapshot, references):
    """Select 3 IN and 5 OON files from referenced URLs within the size limit; raise on insufficient candidates."""
    candidates = {}
    with snapshot.open("rb") as handle:
        for item in ijson.items(handle, "blobs.item"):
            url = item["downloadUrl"]
            for kind in ("in_network", "out_of_network"):
                size = item.get("size")

                if (url, kind) in references and isinstance(size, int) and 0 < size <= SETTINGS["limits"]["sample_blob_bytes"]:
                    candidates[url] = {"source_url": url, "file_type": kind,
                                       "original_filename": item["name"], "listed_size_bytes": size}

    ordered = sorted(candidates.values(), key=lambda row: (row["listed_size_bytes"], row["source_url"]))
    chosen = []
    for label in ("OHPH-Acupuncture-Massage-Naturopath_", "OHPH-ST_", "Optum-Health-Behavioral-Services--OHBS-_"):
        match = None

        for row in ordered:
            if row["file_type"] == "in_network" and label in row["original_filename"]:
                match = row
                break
        if match is None:
            raise ValueError("No eligible Index-referenced IN file for " + label)
        chosen.append(match)
    oon = []
    for row in ordered:
        if row["file_type"] == "out_of_network":
            oon.append(row)
    if len(oon) < 5:
        raise ValueError("Need at least five eligible OON files")

    chosen.extend(oon[-4:])
    chosen.append(oon[0])
    return chosen

def fetch_files(client, files, kind):
    """Download distinct URLs or reuse valid cached files; persist outcomes and return successful rows and counts."""
    manifest = DATA / "metadata" / ("index_manifest.csv" if kind == "index" else "blob_manifest.csv")
    existing = {}
    for row in read_csv(manifest):
        existing[row["source_url"]] = row
    unique = {}
    for item in files:

        unique[item["source_url"]] = item
    summary = {"requested": len(unique), "downloaded": 0, "cached": 0, "failed": 0}
    successful = []
    for url, item in unique.items():
        file_type = "index" if kind == "index" else item["file_type"]
        identity = url_id(url)
        path = DATA / "raw" / file_type / local_name(item["original_filename"], identity)
        old = existing.get(url, {})

        if (old.get("download_status") == "success" and path.is_file()
                and path.stat().st_size == int(old["file_size_bytes"])):
            summary["cached"] += 1
            successful.append(old)
            continue
        row = {"file_id": identity, "source_url": url, "original_filename": item["original_filename"],
               "file_type": file_type, "local_path": str(path), "file_size_bytes": None,
               "download_status": "failed", "downloaded_at": None, "error_message": None}
        try:
            cap = SETTINGS["limits"]["index_bytes" if kind == "index" else "sample_blob_bytes"]
            row["file_size_bytes"] = download(client, url, path, cap)
            row["download_status"] = "success"
            row["downloaded_at"] = now()
            summary["downloaded"] += 1
            successful.append(row)
        except (requests.RequestException, ValueError, OSError, EOFError) as error:
            row["error_message"] = error_message(error)
            summary["failed"] += 1
            print(f"Failed: {item['original_filename']}: {row['error_message']}")

        existing[url] = row
        save(manifest, list(existing.values()))
        time.sleep(0.25)
    return successful, summary

def run(limit=1000, pricing_selection=None):
    """Acquire fixed selections and downloads; failed Index downloads prevent pricing selection."""
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    metadata = DATA / "metadata"
    snapshot = metadata / "discovery/uhc-listing.json"
    selection = metadata / "discovery/selected_indexes.csv"
    with session() as client:

        if not selection.exists():
            if not snapshot.exists():
                download(client, SETTINGS["source"]["listing_url"], snapshot, SETTINGS["limits"]["listing_bytes"])
        if not selection.exists():
            save(selection, select_indexes(snapshot))
        selected = read_csv(selection)
        if len(selected) < limit:
            raise ValueError("Selection contains fewer Indexes than requested")

        indexes, index_summary = fetch_files(client, selected[:limit], "index")
        report = {"run_at": now(), "indexes": index_summary}

        if pricing_selection:

            references = set()
            for index in indexes:
                document = json.loads(Path(index["local_path"]).read_text())
                for structure in document["reporting_structure"]:
                    for field, position, kind, item in file_references(structure):
                        references.add((item["location"], kind))
            if index_summary["failed"]:
                save(metadata / "ingestion_summary.json", report)
                raise ValueError("Index downloads incomplete; rerun before selecting prices")

            if not pricing_selection.exists():
                if not snapshot.exists():
                    download(client, SETTINGS["source"]["listing_url"], snapshot, SETTINGS["limits"]["listing_bytes"])
                save(pricing_selection, select_pricing(snapshot, references))
            chosen = read_csv(pricing_selection)
            if not chosen:
                raise ValueError("Pricing selection is empty")
            for item in chosen:
                if (item["source_url"], item["file_type"]) not in references:
                    raise ValueError("Pricing selection contains files not referenced by these Indexes")
            _, report["pricing"] = fetch_files(client, chosen, "pricing")
    save(metadata / "ingestion_summary.json", report)
    return report

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000, help="Number of Index files, 1–1000")
    parser.add_argument("--pricing-selection", type=Path, help="Pricing CSV: create from listing and Index references when absent")
    args = parser.parse_args()
    result = run(args.limit, args.pricing_selection)
    print(json.dumps(result, indent=2))
    if result["indexes"]["failed"] or result.get("pricing", {}).get("failed"):
        raise SystemExit(1)
