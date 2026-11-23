"""GeoViewer license format - the vendor keygen's copy.

This MUST stay byte-compatible with ``geoviewer/licensing.py`` in the app: the app verifies
what this signs. The format is frozen; if you ever change it, change both and re-issue keys.

Standalone: needs only ``cryptography`` (no GeoViewer code).
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# --- the vendor public key (must match vendor_private_key.pem and the app) ---------------------
_PK_A = "4eze/OE/erKh6oET"
_PK_B = "079YcpfnWSzUd3GloHeLlLMmw4Y="
_PUBKEY = base64.b64decode(_PK_A + _PK_B)
_PUBKEY_SHA256 = "f654bd527cf477f77bd9653813af7e7c555a48eb6680865cad01eec1984d17c8"

MAGIC = b"GV1"
PREFIX = "GEOV"
EDITIONS = {0: "Basic", 1: "Professional", 2: "Ultimate"}
FEAT_DATABASE = 1 << 0               # add / edit / remove the project database
FEAT_EXPORT = 1 << 1                 # export / print maps and sections
FEATURES = {FEAT_DATABASE: "database", FEAT_EXPORT: "export"}
EDITION_FEATURES = {0: 0, 1: FEAT_DATABASE, 2: FEAT_DATABASE | FEAT_EXPORT}
# how far ahead a key of each tier may be issued: Basic up to 30 years (near-perpetual),
# Professional / Ultimate up to 5 years. Every license has an expiry date.
EDITION_MAX_YEARS = {0: 30, 1: 5, 2: 5}
UNBOUND_MAX_YEARS = 1                 # an "Any computer" (not machine-locked) key: at most 1 year
EPOCH = date(2020, 1, 1)


def edition_max_years(edition: int) -> int:
    return EDITION_MAX_YEARS.get(edition, 5)


def issue_max_years(edition: int, unbound: bool) -> int:
    """How far ahead a key may be issued: the edition cap, but at most 1 year when not locked
    to a machine (an unbound long key could be shared, so it is kept short)."""
    return min(edition_max_years(edition), UNBOUND_MAX_YEARS) if unbound else edition_max_years(edition)

_C32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_C32_MAP = {c: i for i, c in enumerate(_C32)}
_C32_MAP.update({"I": 1, "L": 1, "O": 0, "U": 0})


class LicenseError(Exception):
    pass


def edition_features(edition: int) -> int:
    return EDITION_FEATURES.get(edition, 0)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def max_expiry(years: int, today: date | None = None) -> date:
    """The latest expiry a key may be issued for (Feb-29 safe)."""
    d = today or _today()
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + years)


def product_salt() -> bytes:
    return hashlib.sha256(b"geoviewer-license/" + _PUBKEY).digest()


# --- base32 (Crockford) for the machine code --------------------------------------------------
def b32_encode(data: bytes) -> str:
    bits = int.from_bytes(data, "big")
    width = (len(data) * 8 + 4) // 5
    return "".join(_C32[(bits >> (i * 5)) & 0x1F] for i in range(width - 1, -1, -1))


def b32_decode(text: str, nbytes: int) -> bytes:
    bits = 0
    for ch in text:
        if ch in "-_ ":
            continue
        up = ch.upper()
        if up not in _C32_MAP:
            raise LicenseError(f"bad character {ch!r} in the code")
        bits = (bits << 5) | _C32_MAP[up]
    return bits.to_bytes(max(nbytes, (bits.bit_length() + 7) // 8), "big")[-nbytes:]


def machine_id_from_code(code: str) -> bytes:
    cleaned = "".join(ch for ch in code if ch not in "-_ ")
    if len(cleaned) != 20:
        raise LicenseError("a machine code is 20 characters (XXXXX-XXXXX-XXXXX-XXXXX)")
    return b32_decode(cleaned, 12)[:8]


# --- the license ------------------------------------------------------------------------------
@dataclass
class License:
    name: str
    issued: date
    expires: date | None
    edition: int = 1
    features: int = FEAT_DATABASE
    machine: bytes = b""

    @property
    def edition_name(self) -> str:
        return EDITIONS.get(self.edition, f"edition {self.edition}")

    @property
    def feature_names(self) -> list[str]:
        return [n for bit, n in FEATURES.items() if self.features & bit]

    def days_left(self) -> int | None:
        return None if self.expires is None else (self.expires - _today()).days


def _pack(lic: License) -> bytes:
    name = lic.name.encode("utf-8")[:255]
    machine = lic.machine if lic.machine else b"\x00" * 8
    return (MAGIC
            + struct.pack(">HHBB", (lic.issued - EPOCH).days,
                          0 if lic.expires is None else (lic.expires - EPOCH).days,
                          lic.edition & 0xFF, lic.features & 0xFF)
            + machine + struct.pack(">B", len(name)) + name)


def _unpack(payload: bytes) -> License:
    if payload[:3] != MAGIC:
        raise LicenseError("not a GeoViewer license")
    issued_d, expires_d, edition, features = struct.unpack(">HHBB", payload[3:9])
    machine = payload[9:17]
    (nlen,) = struct.unpack(">B", payload[17:18])
    name = payload[18:18 + nlen].decode("utf-8", "replace")
    return License(name=name, issued=EPOCH + timedelta(days=issued_d),
                   expires=None if expires_d == 0 else EPOCH + timedelta(days=expires_d),
                   edition=edition, features=features,
                   machine=b"" if machine == b"\x00" * 8 else machine)


def sign_payload(private_key, lic: License) -> str:
    payload = _pack(lic)
    sig = private_key.sign(payload)
    blob = base64.b32encode(payload + sig).decode().rstrip("=")
    return PREFIX + "-" + "-".join(blob[i:i + 20] for i in range(0, len(blob), 20))


def decode(key: str) -> License:
    """Verify a key with the embedded public key (used to self-check issued keys)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key = (key or "").strip()
    body = key[len(PREFIX) + 1:] if key.upper().startswith(PREFIX + "-") else key
    blob = body.replace("-", "").replace(" ", "").replace("\n", "").upper()
    try:
        data = base64.b32decode(blob + "=" * (-len(blob) % 8))
    except Exception as e:  # noqa: BLE001
        raise LicenseError("the key is not readable") from e
    if len(data) < 64 + 18:
        raise LicenseError("the key is too short")
    payload, sig = data[:-64], data[-64:]
    if hashlib.sha256(_PUBKEY).hexdigest() != _PUBKEY_SHA256:
        raise LicenseError("the embedded public key is corrupted")
    try:
        Ed25519PublicKey.from_public_bytes(_PUBKEY).verify(sig, payload)
    except InvalidSignature as e:
        raise LicenseError("signature does not match the public key") from e
    return _unpack(payload)


# --- keypair helpers --------------------------------------------------------------------------
def load_private_key(path):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(open(path, "rb").read(), password=None)


def generate_keypair(path):
    """Write a new private key to ``path``; return the three lines to paste into the app and
    into this file (_PK_A / _PK_B / _PUBKEY_SHA256)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    open(path, "wb").write(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    b64 = base64.b64encode(raw).decode()
    return {"PK_A": b64[:16], "PK_B": b64[16:], "SHA256": hashlib.sha256(raw).hexdigest()}
