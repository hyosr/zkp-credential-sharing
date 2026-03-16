import base64


def b64url_decode_padded(s: str) -> bytes:
    """
    Decode base64 urlsafe with correct padding.
    Works with tokens that omit '=' padding.
    """
    s = (s or "").strip()
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("utf-8"))


def b64std_decode_padded(s: str) -> bytes:
    """
    Decode standard base64 (also tolerates missing padding).
    """
    s = (s or "").strip()
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s.encode("utf-8"))