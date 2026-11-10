"""End-to-end encryption between a paired phone and this laptop, through Canopy's relay.

The relay (canopy-web, then the runner) only ever carries ciphertext: it can't read the
audio or the transcript, can't pose as the phone, and can't pose as the laptop. The
phone's side is frontend/src/listen/relayCrypto.ts; both follow this spec, and
tests/relay_vectors.json pins them to the same bytes.

KEYS (all ECDH P-256; public keys are the 65-byte uncompressed point, base64url, no padding)
  * laptop static key: made once, kept in ~/.canopy-listen/relay-key.json (0600).
  * phone static key: made once in the phone's browser, non-extractable, in IndexedDB.
  * per session, the phone makes an ephemeral key.

PAIRING (once, at the laptop). The laptop's local page shows a QR code for
  https://<canopy>/listen#pair=<base64url(JSON {"r": runner_id, "k": laptop_pub,
  "s": pairing_secret, "n": laptop_name, "x": expires_unix})>
The fragment never reaches the server. The phone stores (r, k, n) and opens a session of
kind "pair"; its first frame is encrypted under the PAIR keys, derived from the 32-byte
pairing secret, and says {"type": "pair", "phone_key": phone_static_pub, "label": ...}.
The laptop, holding the same secret, adds that key to ~/.canopy-listen/phones.json and
answers {"type": "paired"}. A secret works once and for 10 minutes.

SESSION KEYS. ikm = ECDH(phone_eph, laptop_static) || ECDH(phone_static, laptop_static)
(64 bytes); okm = HKDF-SHA256(ikm, salt=utf8(session_id), info=b"canopy-listen relay v1",
length=64); up = okm[:32] (phone -> laptop), down = okm[32:] (laptop -> phone). The first
term gives a fresh key per session; the second means only the paired phone can make it.
PAIR keys: HKDF-SHA256(pairing_secret, salt=utf8(session_id), info=b"canopy-listen pair v1",
length=64), split the same way.

FRAMES (each a binary WebSocket message): 0x01 | counter (8 bytes, big-endian) |
AES-256-GCM(key, nonce = 4 zero bytes | counter, plaintext, aad = utf8(session_id)).
Each direction counts from 0 and every frame must be greater than the last (no replay).
Plaintext: 0x01 | int16 little-endian mono 16 kHz PCM (audio, phone -> laptop), or
0x02 | UTF-8 JSON (control, either way).
"""
from __future__ import annotations

import base64
import json
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 0x01
AUDIO, CONTROL = 0x01, 0x02
SESSION_INFO = b"canopy-listen relay v1"
PAIR_INFO = b"canopy-listen pair v1"
MAX_FRAME = 64 * 1024


class BadFrame(Exception):
    """A frame that doesn't decrypt, or replays. Its argument is a reason code, never content."""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_private() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def public_b64(key: ec.EllipticCurvePrivateKey) -> str:
    return b64u(key.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint))


def load_public(text: str) -> ec.EllipticCurvePublicKey:
    raw = unb64u(text)
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("not an uncompressed P-256 point")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)


def private_to_pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def private_from_pem(text: str):
    return serialization.load_pem_private_key(text.encode(), password=None)


def _hkdf(ikm: bytes, session_id: str, info: bytes) -> tuple[bytes, bytes]:
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=session_id.encode(), info=info).derive(ikm)
    return okm[:32], okm[32:]


def session_keys_laptop(laptop_static, phone_eph_pub: str, phone_static_pub: str, session_id: str):
    """(up, down) as the laptop computes them."""
    ikm = (laptop_static.exchange(ec.ECDH(), load_public(phone_eph_pub))
           + laptop_static.exchange(ec.ECDH(), load_public(phone_static_pub)))
    return _hkdf(ikm, session_id, SESSION_INFO)


def session_keys_phone(phone_eph, phone_static, laptop_pub: str, session_id: str):
    """(up, down) as the phone computes them: the same bytes."""
    laptop = load_public(laptop_pub)
    ikm = phone_eph.exchange(ec.ECDH(), laptop) + phone_static.exchange(ec.ECDH(), laptop)
    return _hkdf(ikm, session_id, SESSION_INFO)


def pair_keys(pairing_secret: bytes, session_id: str):
    if len(pairing_secret) != 32:
        raise ValueError("a pairing secret is 32 bytes")
    return _hkdf(pairing_secret, session_id, PAIR_INFO)


class Sealer:
    """One direction's sender."""

    def __init__(self, key: bytes, session_id: str):
        self.aead, self.aad, self.counter = AESGCM(key), session_id.encode(), 0

    def seal(self, kind: int, body: bytes) -> bytes:
        counter = self.counter
        self.counter += 1
        nonce = b"\x00\x00\x00\x00" + struct.pack(">Q", counter)
        return bytes([VERSION]) + struct.pack(">Q", counter) + self.aead.encrypt(nonce, bytes([kind]) + body,
                                                                                 self.aad)

    def control(self, message: dict) -> bytes:
        return self.seal(CONTROL, json.dumps(message, separators=(",", ":")).encode())


class Opener:
    """One direction's receiver: rejects anything forged, reordered or replayed."""

    def __init__(self, key: bytes, session_id: str):
        self.aead, self.aad, self.last = AESGCM(key), session_id.encode(), -1

    def open(self, frame: bytes) -> tuple[int, bytes]:
        if len(frame) < 1 + 8 + 16 + 1 or len(frame) > MAX_FRAME or frame[0] != VERSION:
            raise BadFrame("shape")
        counter = struct.unpack(">Q", frame[1:9])[0]
        if counter <= self.last:
            raise BadFrame("replay")
        nonce = b"\x00\x00\x00\x00" + frame[1:9]
        try:
            plain = self.aead.decrypt(nonce, frame[9:], self.aad)
        except Exception:
            raise BadFrame("forged") from None
        self.last = counter
        return plain[0], plain[1:]


def new_pairing_secret() -> bytes:
    return os.urandom(32)
