"""A changed source must be rejected before any model request or artifact output."""
import json
from pathlib import Path
import subprocess
import sys


def test_changed_dataset_is_rejected_before_inference(tmp_path):
    source=tmp_path/'data';source.mkdir()
    (source/'tasks.json').write_text(json.dumps([{'database':'source.sqlite'}]))
    (source/'expected.json').write_text('{}')
    (source/'manifest.json').write_text(json.dumps({'sqlite_sha256':'0'*64}))
    (source/'source.sqlite').write_bytes(b'changed source')
    output=tmp_path/'results'
    result=subprocess.run([sys.executable,'-m','scripts.validation.validate_registered_dataset',
        '--data',str(source),'--output',str(output)],cwd=Path(__file__).parents[1],
        capture_output=True,text=True,timeout=10)
    assert result.returncode != 0
    assert 'source database differs from frozen import manifest' in result.stderr
    assert not output.exists()
