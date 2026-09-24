"""Ed25519 key generation and Solana's case-sensitive base58 representation."""

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _ALPHABET[remainder] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + encoded


def b58decode(value: str) -> bytes:
    if not isinstance(value, str) or not 1 <= len(value) <= 88:
        raise ValueError("Invalid Solana key encoding")
    number = 0
    for char in value:
        if char not in _ALPHABET:
            raise ValueError("Invalid Solana key encoding")
        number = number * 58 + _ALPHABET.index(char)
    return b"\0" * (len(value) - len(value.lstrip("1"))) + number.to_bytes(
        (number.bit_length() + 7) // 8, "big"
    )


def keypair_address(encoded: str) -> str:
    raw = b58decode(encoded)
    if len(raw) != 64:
        raise ValueError("Invalid Solana keypair")
    public = Ed25519PrivateKey.from_private_bytes(raw[:32]).public_key().public_bytes_raw()
    if public != raw[32:]:
        raise ValueError("Invalid Solana keypair")
    return b58encode(public)


def generate_keypair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    return b58encode(public), b58encode(key.private_bytes_raw() + public)
