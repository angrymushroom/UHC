"""Package original Index documents and download snapshots for Snowflake loading."""
import gzip
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

import simplejson

from src.io_utils import DATA, checked_download, file_references, read_csv, write_raw_row

def prepare():
    """Validate 1,000 Index downloads and write original documents plus ledger snapshots."""
    selected = read_csv(DATA / 'metadata/discovery/selected_indexes.csv')
    urls = set()
    ranks = set()

    for row in selected:
        urls.add(row['source_url'])
        ranks.add(int(row['selection_rank']))
    if len(selected) != 1000 or len(urls) != 1000 or ranks != set(range(1, 1001)):
        raise ValueError('Expected the fixed selection of 1000 distinct Index URLs')
    manifest = {}
    for row in read_csv(DATA / 'metadata/index_manifest.csv'):
        if row['source_url'] in manifest:
            raise ValueError('Duplicate download ledger URL')
        manifest[row['source_url']] = row

    output = DATA / 'stage'
    output.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(dir=output, prefix='.indexes-') as temporary:
        with gzip.open(Path(temporary) / 'indexes.jsonl.gz', 'wb') as handle:
            for source in selected:
                downloaded = checked_download(manifest[source['source_url']])
                if (downloaded['download_status'] != 'success' or not downloaded['local_file_exists']
                        or downloaded['observed_file_size_bytes'] != int(downloaded['file_size_bytes'])):
                    raise ValueError('Index missing or changed: ' + source['original_filename'])

                with Path(downloaded['local_path']).open() as original:
                    document = simplejson.load(original, use_decimal=True)

                filenames = {}
                for structure in document['reporting_structure']:
                    for field, position, kind, reference in file_references(structure):
                        url = reference['location']

                        filenames[url] = unquote(urlsplit(url).path.rsplit('/', 1)[-1])

                write_raw_row(handle, {'source': source, 'document': document,
                                       'source_filenames': filenames})
        with gzip.open(Path(temporary) / 'downloads.jsonl.gz', 'wb') as handle:
            for name in ('index_manifest.csv', 'blob_manifest.csv'):
                for row in read_csv(DATA / 'metadata' / name):
                    write_raw_row(handle, checked_download(row))
        # Publishing two files is sequential, not an atomic batch replacement.
        for name in ('indexes.jsonl.gz', 'downloads.jsonl.gz'):
            (Path(temporary) / name).replace(output / name)
    print('Prepared raw Index input and download snapshot in data/stage; no business tables generated.')

if __name__ == '__main__':
    prepare()
