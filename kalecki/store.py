"""Encrypted vault. All tables are serialised to parquet, zipped in memory, and sealed with
AES-256-GCM; the key lives only in the system keychain. No plaintext ever touches disk.

File layout: MAGIC(8) | VERSION(1) | NONCE(12) | ciphertext+tag. AAD binds the version."""
from __future__ import annotations

import base64
import io
import os
import secrets
import tempfile
import zipfile
from pathlib import Path

import polars as pl
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"KALECKI\x00"
VERSION = 1
AAD = b"kaleckiPNL-v1"
KEY_ENTRY = "vault_key"


class KeyStore:
    """Thin wrapper over keyring so tests can swap it for a dict."""

    def __init__(self, service: str = "kaleckiPNL"):
        self.service = service

    def get(self, name: str) -> str | None:
        import keyring
        return keyring.get_password(self.service, name)

    def set(self, name: str, value: str) -> None:
        import keyring
        keyring.set_password(self.service, name, value)

    def delete(self, name: str) -> None:
        import keyring
        try:
            keyring.delete_password(self.service, name)
        except keyring.errors.PasswordDeleteError:
            pass


class MemoryKeyStore(KeyStore):
    def __init__(self):
        self.d: dict[str, str] = {}

    def get(self, name):
        return self.d.get(name)

    def set(self, name, value):
        self.d[name] = value

    def delete(self, name):
        self.d.pop(name, None)


def vault_key(ks: KeyStore) -> bytes:
    """The 256-bit vault key; created on first use."""
    v = ks.get(KEY_ENTRY)
    if v:
        return base64.b64decode(v)
    key = secrets.token_bytes(32)
    ks.set(KEY_ENTRY, base64.b64encode(key).decode())
    return key


def seal(tables: dict[str, pl.DataFrame], key: bytes) -> bytes:
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, df in tables.items():
            pbuf = io.BytesIO()
            df.write_parquet(pbuf)
            z.writestr(f"{name}.parquet", pbuf.getvalue())
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, zbuf.getvalue(), AAD)
    return MAGIC + bytes([VERSION]) + nonce + ct


def unseal(blob: bytes, key: bytes) -> dict[str, pl.DataFrame]:
    if blob[:8] != MAGIC:
        raise ValueError("not a kaleckiPNL vault")
    if blob[8] != VERSION:
        raise ValueError(f"vault version {blob[8]} not supported")
    nonce, ct = blob[9:21], blob[21:]
    pt = AESGCM(key).decrypt(nonce, ct, AAD)  # raises InvalidTag on wrong key or tampering
    out = {}
    with zipfile.ZipFile(io.BytesIO(pt)) as z:
        for info in z.infolist():
            if info.filename.endswith(".parquet"):
                out[info.filename[:-8]] = pl.read_parquet(io.BytesIO(z.read(info)))
    return out


class Vault:
    def __init__(self, path: str | Path, keystore: KeyStore | None = None):
        self.path = Path(path)
        self.ks = keystore or KeyStore()

    def exists(self) -> bool:
        return self.path.exists()

    def save(self, tables: dict[str, pl.DataFrame]) -> None:
        blob = seal(tables, vault_key(self.ks))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".vault-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self) -> dict[str, pl.DataFrame]:
        return unseal(self.path.read_bytes(), vault_key(self.ks))

    def update(self, **tables: pl.DataFrame) -> dict[str, pl.DataFrame]:
        current = self.load() if self.exists() else {}
        current.update(tables)
        self.save(current)
        return current
