"""SIGNATURE: an ECDSA P-256 signature of sha256(MANIFEST.json) (docs/specs/proof-bundle.md §4).

In production the key never leaves Nebius KMS (`AsymmetricCryptoService.SignHash`); tests and
dev use `FakeSigner`, a throwaway local key. Every signature is checked against the public key
before it is stored, so a KMS returning something unexpected leaves the bundle unsigned rather
than carrying a signature nobody can verify.
"""

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    Prehashed,
    decode_dss_signature,
    encode_dss_signature,
)
from nebius.aio.channel import Channel
from nebius.aio.cli_config import Config
from nebius.api.nebius.kms.v1 import (
    AsymmetricCryptoServiceClient,
    AsymmetricGetPublicKeyRequest,
    AsymmetricGetPublicKeyResponse,
    AsymmetricSignHashRequest,
    AsymmetricSignHashResponse,
)
from nebius.sdk import SDK

ALGORITHM = "ecdsa_nist_p256_sha_256"  # the only one bundles use (Nebius KMS key spec)
SIGNATURE_SCHEMA = "p2p.signature/v1"
KMS_TIMEOUT_S = 30.0
_PREHASHED = ec.ECDSA(Prehashed(hashes.SHA256()))
_COORD_BYTES = 32  # P-256
PUBLISHED_KEY = Path(__file__).resolve().parents[2] / "docs" / "bundle-public-key.pem"


class SigningError(Exception):
    """No usable signature (KMS unavailable, wrong key type, invalid signature)."""


class Signer(Protocol):
    key_id: str

    async def sign_digest(self, digest: bytes) -> bytes:
        """DER (or raw r||s) ECDSA signature of a 32-byte sha256 digest."""
        ...

    async def public_key_pem(self) -> str: ...


class FakeSigner:
    """A throwaway in-memory P-256 key (tests and dev)."""

    def __init__(self, key_id: str = "fake-local-key") -> None:
        self.key_id = key_id
        self._key = ec.generate_private_key(ec.SECP256R1())

    async def sign_digest(self, digest: bytes) -> bytes:
        return self._key.sign(digest, _PREHASHED)

    async def public_key_pem(self) -> str:
        return (
            self._key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode("ascii")
        )


class KmsClient(Protocol):
    """The two calls of nebius.api.nebius.kms.v1.AsymmetricCryptoServiceClient we use."""

    def sign_hash(
        self, request: AsymmetricSignHashRequest
    ) -> Awaitable[AsymmetricSignHashResponse]: ...
    def get_public_key(
        self, request: AsymmetricGetPublicKeyRequest
    ) -> Awaitable[AsymmetricGetPublicKeyResponse]: ...


class NebiusKmsSigner:
    """Signs with the KMS asymmetric key `p2p-bundle-signing` (ecdsa_nist_p256_sha_256)."""

    def __init__(self, client: KmsClient, key_id: str, *, timeout_s: float = KMS_TIMEOUT_S):
        self.key_id = key_id
        self._client = client
        self._timeout_s = timeout_s
        self._public_key: str | None = None

    @classmethod
    def from_sdk(cls, sdk: Channel, key_id: str) -> "NebiusKmsSigner":
        return cls(AsymmetricCryptoServiceClient(sdk), key_id)

    async def sign_digest(self, digest: bytes) -> bytes:
        request = AsymmetricSignHashRequest(key_id=self.key_id, hash=digest)
        try:
            response = await asyncio.wait_for(self._client.sign_hash(request), self._timeout_s)
        except Exception as err:  # SDK, transport and auth errors alike: no signature
            raise SigningError(f"KMS SignHash failed: {type(err).__name__}") from err
        self._same_key(response.key_id)
        return bytes(response.signature)

    def _same_key(self, key_id: str) -> None:
        if key_id and key_id != self.key_id:
            raise SigningError("KMS answered for another key")

    async def public_key_pem(self) -> str:
        if self._public_key is None:
            request = AsymmetricGetPublicKeyRequest(key_id=self.key_id)
            try:
                response = await asyncio.wait_for(
                    self._client.get_public_key(request), self._timeout_s
                )
            except Exception as err:
                raise SigningError(f"KMS GetPublicKey failed: {type(err).__name__}") from err
            self._same_key(response.key_id)
            pem = str(response.public_key)
            try:
                _public_key(pem.encode())
            except ValueError as err:
                raise SigningError(str(err)) from None
            self._public_key = pem
        return self._public_key


def to_der(signature: bytes) -> bytes:
    """KMS may return DER or raw r||s; bundles always store DER (what openssl expects)."""
    if len(signature) == 2 * _COORD_BYTES:
        r = int.from_bytes(signature[:_COORD_BYTES], "big")
        s = int.from_bytes(signature[_COORD_BYTES:], "big")
        return encode_dss_signature(r, s)
    try:
        decode_dss_signature(signature)
    except ValueError as err:
        raise SigningError("the signature is neither DER nor raw r||s") from err
    return signature


def _public_key(pem: bytes) -> ec.EllipticCurvePublicKey:
    try:
        key = serialization.load_pem_public_key(pem)
    except (ValueError, UnsupportedAlgorithm) as err:
        raise ValueError("the public key is not a PEM public key") from err
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("the public key is not an ECDSA P-256 key")
    return key


def check_signature(document: bytes, manifest: bytes, public_key_pem: bytes) -> str | None:
    """None when `document` is a valid SIGNATURE of `manifest` under the key; otherwise the
    problem. Pure: it runs in the CPU pool, and on untrusted input in `verify`."""
    try:
        data: Any = json.loads(document)
    except (ValueError, RecursionError):
        return "SIGNATURE is not JSON"
    if not isinstance(data, dict) or data.get("schema") != SIGNATURE_SCHEMA:
        return f"SIGNATURE is not a {SIGNATURE_SCHEMA} document"
    if data.get("algorithm") != ALGORITHM:
        return f"unsupported signature algorithm (expected {ALGORITHM})"
    digest = hashlib.sha256(manifest).digest()
    if data.get("manifest_sha256") != digest.hex():
        return "SIGNATURE was made for another MANIFEST.json"
    try:
        der = base64.b64decode(str(data.get("signature", "")), validate=True)
    except binascii.Error:
        return "the signature is not base64"
    try:
        key = _public_key(public_key_pem)
    except ValueError as err:
        return str(err)
    try:
        key.verify(der, digest, _PREHASHED)
    except InvalidSignature:
        return "the signature does not match MANIFEST.json under this public key"
    return None


async def signature_document(signer: Signer, digest: bytes) -> bytes:
    """The SIGNATURE document for a manifest sha256 (unchecked: see `check_signature`)."""
    der = to_der(await signer.sign_digest(digest))
    body = {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": ALGORITHM,
        "key_id": signer.key_id,
        "manifest_sha256": digest.hex(),
        "signature": base64.b64encode(der).decode("ascii"),
    }
    return json.dumps(body, indent=2, sort_keys=True).encode() + b"\n"


async def sign_manifest(signer: Signer, manifest: bytes, pinned: bytes | None = None) -> bytes:
    """Sign and check in one go, on the calling thread (tests and one-off tools; the
    orchestrator hashes and checks in the CPU pool). With `pinned`, the signature must verify
    under that published key, not only under what KMS says its key is."""
    document = await signature_document(signer, hashlib.sha256(manifest).digest())
    key = pinned if pinned is not None else (await signer.public_key_pem()).encode()
    problem = check_signature(document, manifest, key)
    if problem is not None:
        raise SigningError(problem)
    return document


async def _cli(args: argparse.Namespace) -> int:
    # Imported here: only this human-run tool talks to KMS with the CLI profile's credentials.
    from app.bundle.build import pack
    from app.bundle.verify import (
        MANIFEST,
        SIGNATURE,
        BundleError,
        printable,
        read_bundle,
        verify_files,
    )
    from app.domain.models import VerdictKind

    sdk = SDK(config_reader=Config(profile=args.profile), user_agent_prefix="paper2proof")
    try:
        signer = NebiusKmsSigner.from_sdk(sdk, args.key_id)
        if args.command == "public-key":
            print(await signer.public_key_pem(), end="")
            return 0
        try:
            files = read_bundle(args.bundle)
        except (BundleError, OSError) as err:
            print(f"ERROR {args.bundle}: {err}", file=sys.stderr)
            return 2
        if SIGNATURE in files:  # never strip a signature to sign edited content
            print("ERROR the bundle already has a SIGNATURE", file=sys.stderr)
            return 1
        problems = verify_files(files, None, allow_unsigned=True)
        for problem in problems:  # never sign content that doesn't match its manifest
            print(f"FAIL {printable(problem)}", file=sys.stderr)
        if problems:
            return 1
        manifest = json.loads(files[MANIFEST])
        if manifest.get("verdict") not in {k.value for k in VerdictKind}:
            print("ERROR MANIFEST.json has no valid verdict", file=sys.stderr)
            return 1
        if manifest.get("run_id") != args.run_id:
            print(f"ERROR the bundle is not run {args.run_id}", file=sys.stderr)
            return 1
        digest = hashlib.sha256(files[MANIFEST]).hexdigest()
        print(f"run {args.run_id} verdict {manifest.get('verdict')} MANIFEST.json {digest}")
        question = "Sign only a bundle fetched from p2p-bundles. Sign? [y/N] "
        if not args.yes and (await asyncio.to_thread(input, question)).strip().lower() != "y":
            return 1
        pinned = args.public_key.read_bytes() if args.public_key.is_file() else None
        try:
            document = await sign_manifest(signer, files[MANIFEST], pinned)
        except SigningError as err:
            print(f"ERROR {err}", file=sys.stderr)
            return 1
        args.output.write_bytes(pack({**files, SIGNATURE: document}))
        print(f"OK signed {args.output} with {args.key_id}")
        return 0
    finally:
        await sdk.close()


def main(argv: list[str] | None = None) -> int:
    """Human-run KMS tool (PAIR): export the public key, or sign an unsigned bundle that the
    control plane built (fetched from p2p-bundles) and that matches its own MANIFEST.json.

    uv run python -m app.bundle.sign public-key --key-id <id> > docs/bundle-public-key.pem
    uv run python -m app.bundle.sign sign <bundle> --run-id <r_...> --key-id <id> -o <out>
    """
    parser = argparse.ArgumentParser(description="Nebius KMS signing for Proof Bundles.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("public-key", "sign"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--key-id", required=True, help="KMS asymmetric key p2p-bundle-signing")
        cmd.add_argument("--profile", default=None, help="nebius CLI profile")
        if name == "sign":
            cmd.add_argument("bundle", type=Path)
            cmd.add_argument("--run-id", required=True, help="the run the bundle must be for")
            cmd.add_argument("-o", "--output", type=Path, required=True)
            cmd.add_argument("--public-key", type=Path, default=PUBLISHED_KEY)
            cmd.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return asyncio.run(_cli(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
