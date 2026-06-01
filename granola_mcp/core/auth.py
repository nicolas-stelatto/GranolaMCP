"""
Granola access-token retrieval.

Granola encrypted the local cache in March 2026. The desktop app stores its
WorkOS access token in `supabase.json.enc`, encrypted with AES-256-GCM. The key
is a DEK in `storage.dek`, itself wrapped with AES-128-CBC by Electron's
safeStorage (key in macOS Keychain).

This module decrypts that chain and returns the bearer token used against the
internal Granola REST API (`https://api.granola.ai`).
"""

import base64
import json
import os
from typing import Optional


GRANOLA_DIR = os.path.expanduser("~/Library/Application Support/Granola")
KEYCHAIN_SERVICE = "Granola Safe Storage"
KEYCHAIN_ACCOUNT = "Granola Key"


class GranolaAuthError(Exception):
    """Erro ao decriptar / obter o token do Granola."""


def _safestorage_aes_key() -> bytes:
    """Deriva a chave AES-128 a partir do item no macOS Keychain.

    Electron `safeStorage` no macOS arma uma senha aleatoria no Keychain e
    deriva via PBKDF2-HMAC-SHA1 (salt='saltysalt', 1003 iter, dkLen=16). Esse
    eh o mesmo esquema que o Chromium OSCrypt usa.
    """
    import keyring
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    pw = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    if pw is None:
        raise GranolaAuthError(
            f"Keychain item nao encontrado: svc={KEYCHAIN_SERVICE!r} acct={KEYCHAIN_ACCOUNT!r}. "
            f"O Granola Desktop precisa ter rodado pelo menos uma vez nesse usuario."
        )
    return PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1003
    ).derive(pw.encode("utf-8"))


def _read_dek(dek_path: Optional[str] = None) -> bytes:
    """Le e decripta `storage.dek`, retornando o DEK raw (32 bytes)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    path = dek_path or os.path.join(GRANOLA_DIR, "storage.dek")
    if not os.path.exists(path):
        raise GranolaAuthError(f"storage.dek nao encontrado em {path}")

    blob = open(path, "rb").read()
    if blob[:3] != b"v10":
        raise GranolaAuthError(
            f"storage.dek com prefixo inesperado {blob[:3]!r} (esperado 'v10')"
        )

    aes_key = _safestorage_aes_key()
    decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(b" " * 16)).decryptor()
    padded = decryptor.update(blob[3:]) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    # plaintext eh string base64 de 32 bytes
    dek = base64.b64decode(plaintext.decode("ascii"))
    if len(dek) != 32:
        raise GranolaAuthError(f"DEK com tamanho inesperado: {len(dek)} (esperado 32)")
    return dek


def _decrypt_json_enc(path: str, dek: bytes) -> dict:
    """Decripta um arquivo `.enc` (formato `[IV 12B][ciphertext][tag 16B]`)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not os.path.exists(path):
        raise GranolaAuthError(f"Arquivo nao encontrado: {path}")
    blob = open(path, "rb").read()
    iv, tag, ciphertext = blob[:12], blob[-16:], blob[12:-16]
    decryptor = Cipher(algorithms.AES(dek), modes.GCM(iv, tag)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    return json.loads(plaintext.decode("utf-8"))


def get_access_token() -> str:
    """Retorna o access_token (Bearer) atualmente armazenado pelo Granola Desktop.

    Caminho: Keychain -> storage.dek (DEK) -> supabase.json.enc -> workos_tokens.access_token.

    Quando o token expira (~1h), basta abrir o app Granola — ele refresha em
    segundos e escreve um novo no supabase.json.enc.
    """
    dek = _read_dek()
    data = _decrypt_json_enc(os.path.join(GRANOLA_DIR, "supabase.json.enc"), dek)

    raw = data.get("workos_tokens")
    if raw is None:
        raise GranolaAuthError(
            "Campo 'workos_tokens' ausente no supabase.json (login Granola incompleto?)"
        )
    # workos_tokens vem como JSON string (double-encoded)
    if isinstance(raw, str):
        try:
            tokens = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GranolaAuthError(f"workos_tokens nao eh JSON valido: {exc}") from exc
    elif isinstance(raw, dict):
        tokens = raw
    else:
        raise GranolaAuthError(f"workos_tokens com tipo inesperado: {type(raw).__name__}")

    token = tokens.get("access_token")
    if not token:
        raise GranolaAuthError("access_token ausente em workos_tokens")
    return token
