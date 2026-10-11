"""Small XRPL decoders registered as DuckDB functions (see run.py: register_udfs).

- xrpl_currency_code: 40-hex currency codes -> readable text ("524C555344..." -> "RLUSD")
- nft_issuer / nft_taxon / nft_sequence: fields packed inside an NFTokenID
"""

import hashlib

ALPHABET = b"rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"


def encode_account_id(account_id: bytes) -> str:
    """20-byte AccountID -> classic r-address (base58check, version byte 0x00)."""
    payload = b"\x00" + account_id
    payload += hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(payload, "big")
    out = bytearray()
    while n:
        n, r = divmod(n, 58)
        out.append(ALPHABET[r])
    out.extend(ALPHABET[0:1] * (len(payload) - len(payload.lstrip(b"\x00"))))
    return bytes(reversed(out)).decode()


def currency_code(cur: str | None) -> str | None:
    """Readable currency code. Standard 3-letter codes pass through; 40-hex codes are decoded
    when they hold printable ASCII; AMM LP tokens (first byte 0x03) become "LP"."""
    if cur is None or len(cur) != 40:
        return cur
    if cur.startswith("03"):
        return "LP"
    try:
        text = bytes.fromhex(cur).rstrip(b"\x00").decode("ascii")
    except ValueError:
        return cur
    return text if text and text.isprintable() and "\x00" not in text else cur


def nft_issuer(nft_id: str | None) -> str | None:
    if not nft_id or len(nft_id) != 64:
        return None
    return encode_account_id(bytes.fromhex(nft_id[8:48]))


def nft_sequence(nft_id: str | None) -> int | None:
    if not nft_id or len(nft_id) != 64:
        return None
    return int(nft_id[56:64], 16)


def nft_taxon(nft_id: str | None) -> int | None:
    """The taxon is stored scrambled with the token sequence (XLS-20)."""
    if not nft_id or len(nft_id) != 64:
        return None
    scrambled, seq = int(nft_id[48:56], 16), int(nft_id[56:64], 16)
    return scrambled ^ ((384160001 * seq + 2459) % 4294967296)
