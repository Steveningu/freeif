"""Independent attester.

Holds the secret (preimage) for each promo. The merchant only ever sees the
crypto-condition (a SHA-256 hash), so the merchant cannot release or withhold
refunds. The attester reveals the fulfillment only when Gemini resolves YES.

In this demo it is a separate module with its own XRPL account and its own
secret file. In production it would be a separate service or a multisig of
neutral signers.
"""
import hashlib
import json
import os

SECRETS = "attester_secrets.json"


def _load():
    try:
        return json.load(open(SECRETS))
    except FileNotFoundError:
        return {}


def new_condition(campaign_id: str) -> str:
    """Create a PREIMAGE-SHA-256 crypto-condition. Returns only the condition."""
    pre = os.urandom(32)
    s = _load()
    s[campaign_id] = pre.hex()
    json.dump(s, open(SECRETS, "w"))
    return condition_for(pre)


def condition_for(pre: bytes) -> str:
    # DER: A0 25 | 80 20 <sha256(preimage)> | 81 01 20 (cost = 32 bytes)
    return ("A0258020" + hashlib.sha256(pre).hexdigest() + "810120").upper()


def fulfillment(campaign_id: str) -> str:
    """Revealed only after a YES resolution."""
    pre = bytes.fromhex(_load()[campaign_id])
    return ("A0228020" + pre.hex()).upper()
