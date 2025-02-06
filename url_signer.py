import hashlib
import hmac
import secrets
import os
from config import get_settings

Config = get_settings()

SECRET_FILE = "data/media_digest.key"
_signing_key = None

def get_or_create_signing_key() -> str:
    """
    Get existing signing key from memory, secret file or generate a new one
    """
    global _signing_key
    
    if _signing_key is not None:
        return _signing_key
        
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, 'r', encoding='utf-8') as f:
            _signing_key = f.read().strip()
            return _signing_key
    
    # Generate new key if file doesn't exist
    _signing_key = secrets.token_hex(32)
    
    # Save to file
    with open(SECRET_FILE, 'w', encoding='utf-8') as f:
        f.write(_signing_key)
        
    return _signing_key

def generate_media_digest(url: str) -> str:
    """
    Generate short HMAC digest (first 8 chars) for media URL using SHA1
    """
    signing_key = get_or_create_signing_key()
    message = url.encode('utf-8')
    key = signing_key.encode('utf-8')
    
    signature = hmac.new(key, message, hashlib.sha1)
    return signature.hexdigest()[:8]

def verify_media_digest(url: str, digest: str | None) -> bool:
    """Verify media URL signature"""
    if not Config["media_url_signing_key"]:
        return True  # If no key configured, accept all requests
        
    if digest is None:
        return True  # Accept requests without digest if signing is not enforced
        
    expected_digest = generate_media_digest(url)
    return hmac.compare_digest(digest, expected_digest) 
