"""Test databases stay inside the workspace and never use household data."""
import os
import tempfile
from pathlib import Path

root=Path(__file__).resolve().parent.parent / '.cache' / 'test-temp'
root.mkdir(parents=True,exist_ok=True)
tempfile.tempdir=str(root)
os.environ['BABYCARE_DATA_DIR']=tempfile.mkdtemp(prefix='suite_')
# Staging runs against a throwaway PostgreSQL (see the "Testing and Validation" wiki page) by setting BABYCARE_TEST_DATABASE_URL; otherwise SQLite in the temp dir.
staging=os.environ.get('BABYCARE_TEST_DATABASE_URL')
if staging:os.environ['DATABASE_URL']=staging
else:os.environ.pop('DATABASE_URL',None)
os.environ.setdefault('BABYCARE_STORAGE','local')
