"""Fetch selected members of the official Mini-Dev package using HTTP ranges."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

URL = 'https://drive.usercontent.google.com/download?id=13VLWIwpw5E3d5DUkMvzw7hvHE67a4XkG&export=download&confirm=t'
DOMAINS = ('california_schools', 'financial', 'student_club')


class RangeReader(io.RawIOBase):
    def __init__(self, url):
        self.url, self.position = url, 0
        with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=30) as response:
            self.size = int(response.headers['Content-Length'])

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if self.position < 0:
            raise ValueError('negative seek')
        return self.position

    def read(self, n=-1):
        n = min(self.size-self.position, n if n >= 0 else self.size-self.position)
        if n <= 0:
            return b''
        if n > 64_000_000:
            raise ValueError('unexpected oversized archive read')
        req = urllib.request.Request(self.url, headers={'Range': f'bytes={self.position}-{self.position+n-1}'})
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.status != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {self.position}-'):
                raise RuntimeError('server did not honor bounded range request')
            data = response.read(n)
        self.position += len(data)
        return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/local/bird_mini_dev/databases'))
    parser.add_argument('--full', action='store_true', help='Download all eleven Mini-Dev databases')
    args = parser.parse_args()
    domains = DOMAINS
    if args.full:
        rows = json.loads(Path('data/local/bird_mini_dev/sqlite.jsonl').read_text())
        domains = tuple(sorted({r['db_id'] for r in rows}))
    args.output.mkdir(parents=True, exist_ok=False)
    files = []
    with zipfile.ZipFile(RangeReader(URL)) as archive:
        for info in archive.infolist():
            for domain in domains:
                prefix = f'minidev/MINIDEV/dev_databases/{domain}/'
                if not info.filename.startswith(prefix) or info.is_dir():
                    continue
                relative = Path(domain) / info.filename[len(prefix):]
                if '..' in relative.parts or info.file_size > 2_000_000_000:
                    raise ValueError('invalid archive member')
                target = args.output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info) as source, target.open('wb') as out:
                    while chunk := source.read(4 * 1024 * 1024):
                        out.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if size != info.file_size:
                    raise ValueError('incomplete archive member')
                files.append({'member': info.filename, 'path': str(relative), 'bytes': size,
                    'sha256': digest.hexdigest()})
                print(str(relative), size, flush=True)
    for domain in domains:
        if not (args.output / domain / f'{domain}.sqlite').is_file():
            raise RuntimeError(f'missing database: {domain}')
    (args.output / 'manifest.json').write_text(json.dumps({'package': URL,
        'source': 'https://github.com/bird-bench/mini_dev', 'license': 'CC-BY-SA-4.0',
        'domains': domains, 'files': files}, indent=2))


if __name__ == '__main__':
    main()
