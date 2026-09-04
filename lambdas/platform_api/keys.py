"""API key minting + API Gateway usage-plan wiring.

Key format (also enforced by the data-plane authorizer — keep in sync):
  gfy_{live|test}_{kid:12 Crockford32}_{secret:43 base64url}{crc:6 base62}
Only the SHA-256 of the full key string is stored; the plaintext is returned
exactly once from POST /keys. A deterministic NON-secret usage identifier
(gfyusage-<kid>-graphify) is registered as an API Gateway API key so the
per-key usage plan can throttle/quota without duplicating the secret.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import zlib

import boto3

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

_apigw = boto3.client("apigateway", region_name=os.environ["AWS_REGION"])


def b62_crc(payload: str) -> str:
    n = zlib.crc32(payload.encode())
    out = []
    for _ in range(6):
        out.append(_B62[n % 62])
        n //= 62
    return "".join(reversed(out))


def usage_identifier_for(kid: str) -> str:
    return f"gfyusage-{kid}-graphify"


def mint_key(mode: str = "live") -> dict:
    kid = "".join(secrets.choice(_CROCKFORD) for _ in range(12))
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")  # 43 chars
    payload = f"gfy_{mode}_{kid}_{secret}"
    full = payload + b62_crc(payload)
    return {
        "kid": kid,
        "plaintext": full,
        "key_hash": hashlib.sha256(full.encode()).digest(),
        "key_prefix": f"gfy_{mode}_{kid}",
        "last4": full[-4:],
    }


def register_usage_key(kid: str, usage_plan_id: str) -> str:
    """Create the API GW key that makes per-key throttling/quota work; returns its id."""
    created = _apigw.create_api_key(
        name=f"gfy-{kid}", value=usage_identifier_for(kid), enabled=True,
        description="graphify platform key (usage-plan identifier; not the client secret)",
    )
    _apigw.create_usage_plan_key(usagePlanId=usage_plan_id, keyId=created["id"], keyType="API_KEY")
    return created["id"]


def delete_usage_key(apigw_key_id: str) -> None:
    try:
        _apigw.delete_api_key(apiKey=apigw_key_id)
    except Exception:
        pass  # revocation is enforced by the authorizer; this is cleanup
