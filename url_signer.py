#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylance: disable=reportMissingImports, reportMissingModuleSource, reportGeneralTypeIssues
# type: ignore

import hashlib
import hmac
import secrets
import os
from config import get_settings

Config = get_settings()

class KeyManager:
    SECRET_FILE = "data/media_digest.key"
    signing_key = None

    @classmethod
    def get_or_create_signing_key(cls) -> str:
        """
        Get existing signing key from memory, secret file or generate a new one
        """
        if cls.signing_key is not None:
            return cls.signing_key
        
        if os.path.exists(cls.SECRET_FILE):
            with open(cls.SECRET_FILE, 'r', encoding='utf-8') as f:
                cls.signing_key = f.read().strip()
                return cls.signing_key
        
        cls.signing_key = secrets.token_hex(32)         # Generate new key if file doesn't exist
        
        with open(cls.SECRET_FILE, 'w', encoding='utf-8') as f:
            f.write(cls.signing_key)         # Save to file
        
        return cls.signing_key

def generate_media_digest(url: str) -> str:
    """
    Generate short HMAC digest (first 8 chars) for media URL using SHA1
    """
    signing_key = KeyManager.get_or_create_signing_key()
    message = url.encode('utf-8')
    key = signing_key.encode('utf-8')
    
    signature = hmac.new(key, message, hashlib.sha1)
    return signature.hexdigest()[:8]

def verify_media_digest(url: str, digest: str | None) -> bool:
    """
    Verify HMAC digest for media URL
    Returns True if digest is valid
    """    
    if not digest:
        return False
        
    expected = generate_media_digest(url)
    return hmac.compare_digest(expected, digest) 
