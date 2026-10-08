"""Bounded local cross-encoder scoring; scores are not correctness confidence."""
from functools import lru_cache
import hashlib
import math
from pathlib import Path
import time


@lru_cache(maxsize=2)
def _load(path, signature):
    from sentence_transformers import CrossEncoder
    digest = hashlib.sha256()
    for name, _, _ in signature:
        digest.update(name.encode())
        with (Path(path) / name).open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
    model = CrossEncoder(path, device='cpu', local_files_only=True, trust_remote_code=False)
    return model, digest.hexdigest()


def rerank(path, question, candidates):
    started = time.perf_counter()
    root = Path(path).resolve()
    if not root.is_dir():
        raise ValueError('local reranker model directory required')
    signature = tuple(sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                            for p in root.rglob('*') if p.is_file() and '.cache' not in p.parts
                            and p.suffix in {'.json', '.txt', '.safetensors', '.bin'}))
    if not signature:
        raise ValueError('reranker model files missing')
    model, fingerprint = _load(str(root), signature)
    candidates = candidates[:20]
    pairs = [(question, d['title'] + ' ' + d['text']) for _, d in candidates]
    limit = min(model.max_length or 512, model.tokenizer.model_max_length, 512)
    for question_text, document_text in pairs:
        encoded = model.tokenizer(question_text, document_text, truncation=False)
        if len(encoded['input_ids']) > limit:
            raise ValueError('reranker input exceeds model token limit')
    scores = [float(s) for s in model.predict(pairs, batch_size=8, show_progress_bar=False)]
    if len(scores) != len(pairs) or not all(math.isfinite(s) for s in scores):
        raise ValueError('invalid reranker scores')
    ranked = sorted(zip(scores, candidates), key=lambda item: (-item[0], item[1][1]['id']))
    return [(original_score, {**doc, 'reranker_score': score})
            for score, (original_score, doc) in ranked], {
                'method': 'cross_encoder', 'model_sha256': fingerprint,
                'candidates': len(pairs), 'seconds': time.perf_counter() - started}
