import sys
from base64 import urlsafe_b64encode

from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def main() -> int:
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    sys.stdout.write(f"VAPID_PRIVATE_KEY={_b64url(private)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
