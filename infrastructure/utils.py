import hashlib


def get_file_content_hash(path: str) -> str:
    """Generate hash of file content."""
    with open(path, 'rb') as f:
        digest = hashlib.file_digest(f, 'sha256')
        return digest.hexdigest()
