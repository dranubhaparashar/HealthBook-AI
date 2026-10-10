"""Encrypted document blob storage.

Two backends, selected with BABYCARE_STORAGE:
  local     (default) files under DATA_DIR/encrypted_files, as before.
  supabase  a PRIVATE Supabase Storage bucket, accessed only from the backend with the service-role key.

Every byte handed to this module is already Fernet-encrypted by the caller (api.crypto); this module never
decrypts, never logs object names together with patient identifiers, and never produces public URLs.
Render's free web service has an ephemeral disk, so the local backend must not be used there.
"""
import os
import time
import httpx
from .config import DATA_DIR

LOCAL_DIR = DATA_DIR / 'encrypted_files'
OBJECT_PREFIX = 'documents/'


class StorageError(RuntimeError):
    """The backing store rejected the operation or is unreachable."""


class MissingObject(StorageError):
    """The requested encrypted object does not exist."""


def _safe_name(name: str) -> str:
    if not name or '/' in name or '\\' in name or name.startswith('.') or len(name) > 70:
        raise StorageError('Invalid storage object name')
    return name


class LocalStorage:
    backend = 'local'

    def __init__(self, directory=LOCAL_DIR):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            self.dir.chmod(0o700)
        except OSError:
            pass

    def write(self, name, data: bytes):
        path = self.dir / _safe_name(name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite an existing original
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)

    def read(self, name) -> bytes:
        path = self.dir / _safe_name(name)
        if not path.exists():
            raise MissingObject(name)
        return path.read_bytes()

    def delete(self, name):
        (self.dir / _safe_name(name)).unlink(missing_ok=True)

    def exists(self, name) -> bool:
        return (self.dir / _safe_name(name)).exists()

    def list(self):
        return sorted(p.name for p in self.dir.glob('*.enc'))

    def describe(self):
        return {'backend': 'local', 'location': str(self.dir)}


class SupabaseStorage:
    backend = 'supabase'

    def __init__(self, url=None, service_key=None, bucket=None, transport=None):
        self.url = (url or os.getenv('SUPABASE_URL', '')).rstrip('/')
        self.key = service_key or os.getenv('SUPABASE_SERVICE_ROLE_KEY', '')
        self.bucket = bucket or os.getenv('SUPABASE_STORAGE_BUCKET', 'medical-documents')
        if not self.url.startswith('https://') or not self.key:
            raise StorageError('SUPABASE_URL (https) and SUPABASE_SERVICE_ROLE_KEY are required for the supabase storage backend')
        self._client = httpx.Client(base_url=self.url + '/storage/v1', timeout=httpx.Timeout(60.0, connect=15.0), transport=transport,
                                    headers={'Authorization': 'Bearer ' + self.key, 'apikey': self.key})

    def _call(self, method, path, **kwargs):
        last = None
        for attempt in range(3):
            try:
                return self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:  # free-tier cold starts and brief network blips
                last = exc
                time.sleep(0.5 * (attempt + 1))
        raise StorageError('Storage service unreachable') from last

    def _object_path(self, name):
        return f'/object/{self.bucket}/{OBJECT_PREFIX}{_safe_name(name)}'

    def ensure_bucket(self):
        """Create the private bucket if needed. Never flips an existing bucket to public."""
        response = self._call('GET', f'/bucket/{self.bucket}')
        if response.status_code == 200:
            if response.json().get('public'):
                raise StorageError(f'Bucket {self.bucket} is PUBLIC; medical documents must never be stored in a public bucket')
            return False
        response = self._call('POST', '/bucket', json={'id': self.bucket, 'name': self.bucket, 'public': False, 'file_size_limit': 13 * 1024 * 1024})
        if response.status_code in (200, 201):
            return True
        if response.status_code == 409:
            return False
        raise StorageError(f'Bucket setup failed ({response.status_code})')

    def write(self, name, data: bytes):
        response = self._call('POST', self._object_path(name), content=data, headers={'Content-Type': 'application/octet-stream', 'x-upsert': 'false'})
        if response.status_code in (200, 201):
            return
        if response.status_code in (400, 409) and 'exist' in response.text.lower():
            raise StorageError('Object already exists')
        raise StorageError(f'Upload failed ({response.status_code})')

    def read(self, name) -> bytes:
        response = self._call('GET', self._object_path(name))
        if response.status_code == 200:
            return response.content
        if response.status_code in (400, 404):
            raise MissingObject(name)
        raise StorageError(f'Download failed ({response.status_code})')

    def delete(self, name):
        response = self._call('DELETE', self._object_path(name))
        if response.status_code not in (200, 204, 400, 404):
            raise StorageError(f'Delete failed ({response.status_code})')

    def exists(self, name) -> bool:
        response = self._call('POST', f'/object/list/{self.bucket}', json={'prefix': OBJECT_PREFIX, 'limit': 1, 'offset': 0, 'search': _safe_name(name)})
        if response.status_code != 200:
            raise StorageError(f'List failed ({response.status_code})')
        return any(item.get('name') == name for item in response.json())

    def list(self):
        names, offset = [], 0
        while True:
            response = self._call('POST', f'/object/list/{self.bucket}', json={'prefix': OBJECT_PREFIX, 'limit': 1000, 'offset': offset, 'sortBy': {'column': 'name', 'order': 'asc'}})
            if response.status_code != 200:
                raise StorageError(f'List failed ({response.status_code})')
            page = [item['name'] for item in response.json() if item.get('name')]
            names.extend(page)
            if len(page) < 1000:
                return sorted(names)
            offset += 1000

    def describe(self):
        return {'backend': 'supabase', 'bucket': self.bucket, 'public': False}


_instance = None


def get_storage():
    global _instance
    if _instance is None:
        backend = os.getenv('BABYCARE_STORAGE', 'local').strip().lower()
        if backend == 'supabase':
            _instance = SupabaseStorage()
        elif backend == 'local':
            _instance = LocalStorage()
        else:
            raise StorageError(f'Unknown BABYCARE_STORAGE backend: {backend}')
    return _instance


def reset_storage():
    """Tests and migration tools re-select the backend after changing environment variables."""
    global _instance
    _instance = None


def write_document(name, encrypted: bytes):
    get_storage().write(name, encrypted)


def read_document(name) -> bytes:
    return get_storage().read(name)


def delete_document(name):
    get_storage().delete(name)


def document_exists(name) -> bool:
    return get_storage().exists(name)


def list_documents():
    return get_storage().list()
