"""Environment configuration shared by API, migrations and worker."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')
legacy_dir=Path.home()/'.babycare-ai'
default_dir=legacy_dir if (legacy_dir/'babycare.sqlite3').exists() else ROOT/'private_data'
DATA_DIR = Path(os.getenv('BABYCARE_DATA_DIR', str(default_dir))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
PRODUCTION = os.getenv('BABYCARE_ENV', 'development') == 'production'
if PRODUCTION and not os.getenv('BABYCARE_FERNET_KEY'):
    raise RuntimeError('Production requires BABYCARE_FERNET_KEY from a secrets manager')
def _normalize(url):
    """Supabase and Render hand out postgres:// or postgresql:// URLs; this project uses the psycopg 3 driver."""
    for prefix in ('postgres://', 'postgresql://'):
        if url.startswith(prefix):
            return 'postgresql+psycopg://' + url[len(prefix):]
    return url
DATABASE_URL = _normalize(os.getenv('DATABASE_URL', f'sqlite:///{DATA_DIR / "babycare.sqlite3"}'))
if PRODUCTION and os.getenv('BABYCARE_STORAGE', 'local').lower() == 'local' and os.getenv('BABYCARE_ALLOW_LOCAL_STORAGE') != 'true':
    # Free web hosts have ephemeral disks; originals would vanish on every deploy. Opt in explicitly for self-hosted servers with a real volume.
    raise RuntimeError('Production requires BABYCARE_STORAGE=supabase (private bucket) or BABYCARE_ALLOW_LOCAL_STORAGE=true on a host with a persistent volume')
