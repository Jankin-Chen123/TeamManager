import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet


class Vault:
    def __init__(self, key: str):
        self.fernet = Fernet(key.encode())

    def encrypt(self, value: str):
        return self.fernet.encrypt(value.encode()).decode() if value else ""

    def decrypt(self, value: str):
        return self.fernet.decrypt(value.encode()).decode() if value else ""


def hash_password(password: str):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.b64encode(salt + digest).decode()


def verify_password(password: str, stored: str):
    data = base64.b64decode(stored)
    digest = hashlib.scrypt(password.encode(), salt=data[:16], n=16384, r=8, p=1)
    return hmac.compare_digest(digest, data[16:])


def token_hash(token: str):
    return hashlib.sha256(token.encode()).hexdigest()
