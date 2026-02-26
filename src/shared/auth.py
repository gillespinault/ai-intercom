from __future__ import annotations

import hashlib
import hmac
import time

MAX_TIMESTAMP_DRIFT = 60  # seconds


def normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Normalize header keys to title-case for verify_request.

    Starlette lowercases all header keys (e.g. 'x-intercom-timestamp'),
    but verify_request expects 'X-Intercom-Timestamp'.
    """
    return {key.title(): value for key, value in headers.items()}


def sign_request(body: bytes, machine_id: str, token: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signing_input = body + timestamp.encode()
    signature = hmac.new(token.encode(), signing_input, hashlib.sha256).hexdigest()
    return {
        "X-Intercom-Machine": machine_id,
        "X-Intercom-Timestamp": timestamp,
        "X-Intercom-Signature": f"sha256={signature}",
    }


def verify_request(body: bytes, headers: dict[str, str], token: str) -> bool:
    timestamp_str = headers.get("X-Intercom-Timestamp", "")
    signature = headers.get("X-Intercom-Signature", "")

    if not timestamp_str or not signature:
        return False

    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return False

    if abs(time.time() - timestamp) > MAX_TIMESTAMP_DRIFT:
        return False

    if not signature.startswith("sha256="):
        return False

    expected_sig = signature[7:]
    signing_input = body + timestamp_str.encode()
    computed = hmac.new(token.encode(), signing_input, hashlib.sha256).hexdigest()

    return hmac.compare_digest(computed, expected_sig)
