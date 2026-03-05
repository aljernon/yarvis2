#!/usr/bin/env python3
import base64

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

"""
A simple module for encrypting and decrypting API keys for the Claude Camera app.
Uses AES encryption via Fernet (a symmetric encryption) which is easier to implement
than the RSA approach but still provides strong security.
"""

# Hardcoded salt and secret key for encryption
# In a production app, these would be stored more securely
SALT = b"\xf2\x84\xd4\x19\x88\x91\xb9\xfd\x89\x21\xbb\xd1\x53\xc6\x12\xaa"
SECRET_KEY = "claude-camera-secret-key-2025"


def get_key():
    """Generate a Fernet key from our secret key and salt"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SALT,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(SECRET_KEY.encode()))
    return key


def encrypt_data(data):
    """Encrypt a string using Fernet (AES)"""
    if isinstance(data, str):
        data = data.encode()

    fernet = Fernet(get_key())
    encrypted = fernet.encrypt(data)
    return base64.urlsafe_b64encode(encrypted).decode()


def decrypt_data(encrypted_data):
    """Decrypt data encrypted with encrypt_data"""
    encrypted_data = base64.urlsafe_b64decode(encrypted_data)
    fernet = Fernet(get_key())
    decrypted = fernet.decrypt(encrypted_data)
    return decrypted.decode()


if __name__ == "__main__":
    # Test the encryption/decryption
    test_key = "sk-ant-api12345-test"
    encrypted = encrypt_data(test_key)
    print(f"Encrypted: {encrypted}")

    decrypted = decrypt_data(encrypted)
    print(f"Decrypted: {decrypted}")

    print(f"Match: {test_key == decrypted}")

    # Generate a sample URL for the user
    print(f"\nSetup URL for the user:")
    print(
        f"https://claude-telegram-c4fccbf117d9.herokuapp.com/camera/setup?key={encrypted}"
    )
