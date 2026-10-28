from app.core.secrets import encrypt_local_secret, resolve_secret


def encrypt_password(raw_password: str) -> str:
    return encrypt_local_secret(raw_password)


def decrypt_password(password_cipher: str) -> str:
    return resolve_secret(password_cipher)
