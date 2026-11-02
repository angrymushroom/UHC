"""Stream complete IN/OON services into raw JSONL with source URLs and array positions."""
import gzip
from pathlib import Path

import ijson

from src.io_utils import DATA, checked_download, read_csv, write_raw_row

def read_section(path, section, metadata):
    """Yield JSON events and collect top-level metadata; raise if the requested array is missing or invalid."""
    found = False

    with gzip.open(path, 'rb') as handle:
        for prefix, event, value in ijson.parse(handle):

            if prefix == section and event == 'start_array':
                found = True
            if '.' not in prefix and event in ('string', 'number', 'boolean', 'null'):
                metadata[prefix] = value

            yield prefix, event, value

    if not found:
        raise ValueError('Missing array: ' + section)

def prepare():
    """Validate selected downloads and write raw service JSONL; publish only after the complete batch succeeds."""
    selected = read_csv(DATA / 'metadata/discovery/selected_pricing.csv')
    urls = set()
    kinds = {'in_network': 0, 'out_of_network': 0}

    for row in selected:
        urls.add(row['source_url'])
        kinds[row['file_type']] += 1
    if len(urls) != 8 or len(selected) != 8 or kinds != {'in_network': 3, 'out_of_network': 5}:
        raise ValueError('Expected the fixed selection: 3 IN + 5 OON')
    manifest = {}
    for row in read_csv(DATA / 'metadata/blob_manifest.csv'):
        if row['source_url'] in manifest:
            raise ValueError('Duplicate download ledger URL')
        manifest[row['source_url']] = row
    output = DATA / 'stage/pricing.jsonl.gz'
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.part')
    try:
        with gzip.open(temporary, 'wb') as handle:
            for source in selected:
                downloaded = checked_download(manifest[source['source_url']])
                if (downloaded['download_status'] != 'success' or not downloaded['local_file_exists']
                        or downloaded['observed_file_size_bytes'] != int(downloaded['file_size_bytes'])
                        or downloaded['file_type'] != source['file_type']):
                    raise ValueError('Pricing download missing or changed: ' + source['original_filename'])
                section = source['file_type']
                metadata = {}
                counts = {}
                events = read_section(Path(downloaded['local_path']), section, metadata)
                count = 0

                # One complete service is held in memory; the output guard runs after construction.
                for position, document in enumerate(ijson.items(events, section + '.item')):
                    write_raw_row(handle, {'source_url': source['source_url'], 'section': section,
                                           'source_position': position, 'document': document})
                    count += 1
                counts[section] = count

                write_raw_row(handle, {'source_url': source['source_url'], 'section': 'header',
                                       'file_type': source['file_type'], 'document': metadata,
                                       'section_counts': counts})

        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print('Prepared data/stage/pricing.jsonl.gz; no business tables generated.')

if __name__ == '__main__':
    prepare()
