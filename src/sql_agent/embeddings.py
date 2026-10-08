"""Local-only sentence embeddings and content/model-addressed vector cache."""
from functools import lru_cache
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3


def unit(vector):
    values = [float(x) for x in vector]
    if not values or not all(math.isfinite(x) for x in values):
        raise ValueError('invalid embedding vector')
    norm = math.sqrt(sum(x*x for x in values))
    if not norm:
        raise ValueError('zero embedding vector')
    return [x/norm for x in values]


@lru_cache(maxsize=2)
def _load(path, signature):
    from sentence_transformers import SentenceTransformer
    digest = hashlib.sha256()
    for name, _, _ in signature:
        digest.update(name.encode())
        with (Path(path)/name).open('rb') as stream:
            for block in iter(lambda: stream.read(1024*1024), b''):
                digest.update(block)
    model = SentenceTransformer(path, device='cpu', local_files_only=True, trust_remote_code=False)
    return model, digest.hexdigest()


class LocalEmbeddings:
    def __init__(self, path, cache):
        path = Path(path).resolve()
        if not path.is_dir():
            raise ValueError('local embedding model directory required')
        signature = tuple(sorted((str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns)
                                for p in path.rglob('*') if p.is_file()
                                and '.cache' not in p.parts and p.suffix in {'.json','.txt','.safetensors','.bin'}))
        if not signature:
            raise ValueError('embedding model files missing')
        self.model, self.model_id = _load(str(path), signature)
        self.cache = Path(cache).resolve()

    def chunks(self, document, *, strategy='window'):
        if strategy not in {'window', 'structure'}:
            raise ValueError('unsupported chunking strategy')
        title = document['title']
        title_size = len(self.model.tokenizer(title, add_special_tokens=False)['input_ids'])
        size = min(200, self.model.max_seq_length - title_size - 8)
        if size < 40:
            raise ValueError('title exceeds embedding budget')
        offsets = self.model.tokenizer(document['text'], add_special_tokens=False,
                                       return_offsets_mapping=True)['offset_mapping']
        if not offsets:
            return []
        chunks = []
        start = 0
        parent_hash = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        # Paragraph boundaries are hard boundaries; oversized paragraphs retain
        # bounded overlapping token windows. This does not infer semantic rules.
        boundaries = []
        if strategy == 'structure':
            for match in re.finditer(r'\n[ \t]*\n+', document['text']):
                boundary = next((i for i, (left, _) in enumerate(offsets)
                                 if left >= match.end()), len(offsets))
                if 0 < boundary < len(offsets):
                    boundaries.append(boundary)
        while start < len(offsets):
            section_end = next((b for b in boundaries if b > start), len(offsets))
            end = min(start+size, section_end)
            left, right = offsets[start][0], offsets[end-1][1]
            chunks.append({**document, 'id': document['id']+'#'+str(len(chunks)),
                'parent_id':document['id'], 'parent_sha256':parent_hash,
                'text':document['text'][left:right], 'char_start':left, 'char_end':right,
                'chunk_tokens':end-start, 'chunker':('paragraph_window_v1_200_overlap32'
                    if strategy == 'structure' else 'token_window_v1_200_overlap32')})
            if end == len(offsets):
                break
            start = end if end == section_end else end-32
        return chunks

    def encode(self, texts):
        for text in texts:
            if len(self.model.tokenizer(text, add_special_tokens=True)['input_ids']) > self.model.max_seq_length:
                raise ValueError('embedding input exceeds model token limit')
        vectors = self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        if len(vectors) != len(texts):
            raise ValueError('embedding batch size mismatch')
        return [unit(v) for v in vectors]

    def documents(self, chunks):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.cache, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        vectors = []
        with sqlite3.connect(self.cache, timeout=10) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {'embedding_cache'}:
                raise ValueError('embedding cache must be a dedicated database')
            conn.execute('CREATE TABLE IF NOT EXISTS embedding_cache (key TEXT PRIMARY KEY, vector TEXT NOT NULL)')
            for chunk in chunks:
                text = chunk['title']+' '+chunk['text']
                key = hashlib.sha256((self.model_id+'\n'+json.dumps(chunk,sort_keys=True)).encode()).hexdigest()
                found = conn.execute('SELECT vector FROM embedding_cache WHERE key=?',(key,)).fetchone()
                vector = unit(json.loads(found[0])) if found else self.encode([text])[0]
                if not found:
                    conn.execute('INSERT OR IGNORE INTO embedding_cache VALUES (?,?)',(key,json.dumps(vector)))
                vectors.append(vector)
        return vectors
