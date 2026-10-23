"""Signatures over sha256(MANIFEST.json): a local fake key, and Nebius KMS through its client."""

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from nebius.api.nebius.kms.v1 import (
    AsymmetricGetPublicKeyRequest,
    AsymmetricGetPublicKeyResponse,
    AsymmetricSignHashRequest,
    AsymmetricSignHashResponse,
)

from app.bundle.sign import (
    ALGORITHM,
    FakeSigner,
    NebiusKmsSigner,
    SigningError,
    check_signature,
    sign_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = b'{"schema": "p2p.bundle/v1", "run_id": "r_01jb7z00000000000000000000"}'


async def test_fake_signer_round_trip() -> None:
    signer = FakeSigner()
    document = await sign_manifest(signer, MANIFEST)
    data = json.loads(document)
    assert data["schema"] == "p2p.signature/v1"
    assert data["algorithm"] == ALGORITHM
    assert data["key_id"] == signer.key_id
    assert data["manifest_sha256"] == hashlib.sha256(MANIFEST).hexdigest()
    pem = (await signer.public_key_pem()).encode()
    assert check_signature(document, MANIFEST, pem) is None


async def test_a_changed_manifest_fails_the_check() -> None:
    signer = FakeSigner()
    document = await sign_manifest(signer, MANIFEST)
    pem = (await signer.public_key_pem()).encode()
    problem = check_signature(document, MANIFEST + b" ", pem)
    assert problem is not None and "MANIFEST.json" in problem


async def test_another_key_fails_the_check() -> None:
    document = await sign_manifest(FakeSigner(), MANIFEST)
    other = (await FakeSigner().public_key_pem()).encode()
    problem = check_signature(document, MANIFEST, other)
    assert problem is not None and "signature" in problem


async def test_a_forged_digest_field_does_not_help() -> None:
    signer = FakeSigner()
    pem = (await signer.public_key_pem()).encode()
    tampered = MANIFEST.replace(b"r_01", b"r_02")
    data = json.loads(await sign_manifest(signer, MANIFEST))
    data["manifest_sha256"] = hashlib.sha256(tampered).hexdigest()
    problem = check_signature(json.dumps(data).encode(), tampered, pem)
    assert problem is not None and "signature" in problem


@pytest.mark.parametrize(
    "document",
    [b"not json", b"[]", b'{"schema": "p2p.signature/v1"}', b'{"algorithm": "rsa"}'],
)
async def test_malformed_signature_documents_are_problems(document: bytes) -> None:
    pem = (await FakeSigner().public_key_pem()).encode()
    assert check_signature(document, MANIFEST, pem) is not None


async def test_a_bad_public_key_is_a_problem_not_a_crash() -> None:
    document = await sign_manifest(FakeSigner(), MANIFEST)
    assert check_signature(document, MANIFEST, b"-----BEGIN PUBLIC KEY-----\nxx") is not None


async def test_a_signer_returning_garbage_is_refused() -> None:
    class Broken(FakeSigner):
        async def sign_digest(self, digest: bytes) -> bytes:
            return b"\x30\x06\x02\x01\x01\x02\x01\x01"

    with pytest.raises(SigningError):
        await sign_manifest(Broken(), MANIFEST)


class FakeKms:
    """Stands in for nebius AsymmetricCryptoServiceClient: signs with a FakeSigner's key."""

    def __init__(self, raw: bool = False) -> None:
        self.inner = FakeSigner(key_id="kms-key-1")
        self.raw = raw
        self.requests: list[object] = []

    async def sign_hash(self, request: AsymmetricSignHashRequest) -> AsymmetricSignHashResponse:
        self.requests.append(request)
        der = await self.inner.sign_digest(request.hash)
        if self.raw:  # some KMS return r||s instead of DER
            r, s = decode_dss_signature(der)
            der = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return AsymmetricSignHashResponse(key_id=request.key_id, signature=der)

    async def get_public_key(
        self, request: AsymmetricGetPublicKeyRequest
    ) -> AsymmetricGetPublicKeyResponse:
        self.requests.append(request)
        return AsymmetricGetPublicKeyResponse(
            key_id=request.key_id, public_key=await self.inner.public_key_pem()
        )


@pytest.mark.parametrize("raw", [False, True])
async def test_nebius_signer_signs_the_manifest_digest(raw: bool) -> None:
    kms = FakeKms(raw=raw)
    signer = NebiusKmsSigner(kms, key_id="kms-key-1")
    document = await sign_manifest(signer, MANIFEST)
    sign = next(r for r in kms.requests if isinstance(r, AsymmetricSignHashRequest))
    assert sign.key_id == "kms-key-1"
    assert sign.hash == hashlib.sha256(MANIFEST).digest()
    pem = (await signer.public_key_pem()).encode()
    assert check_signature(document, MANIFEST, pem) is None
    der = base64.b64decode(json.loads(document)["signature"])
    assert der[0] == 0x30  # stored as DER, whatever the KMS returned


async def test_nebius_signer_wraps_client_errors() -> None:
    class Down(FakeKms):
        async def sign_hash(self, request: AsymmetricSignHashRequest) -> AsymmetricSignHashResponse:
            raise ConnectionError("unavailable")

    with pytest.raises(SigningError):
        await sign_manifest(NebiusKmsSigner(Down(), key_id="kms-key-1"), MANIFEST)


def test_the_published_key_is_public_only() -> None:
    path = ROOT / "docs" / "bundle-public-key.pem"
    if not path.exists():
        pytest.skip("docs/bundle-public-key.pem is exported by the human (PAIR)")
    text = path.read_text(encoding="ascii")
    assert "PRIVATE" not in text
    assert text.startswith("-----BEGIN PUBLIC KEY-----")


@pytest.fixture
def kms_cli(monkeypatch: pytest.MonkeyPatch) -> FakeKms:
    """The CLI with its SDK, CLI profile and KMS client replaced (no credentials, no network)."""
    import app.bundle.sign as sign

    kms = FakeKms()

    class Sdk:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def close(self) -> None:
            pass

    monkeypatch.setattr(sign, "SDK", Sdk)
    monkeypatch.setattr(sign, "Config", lambda **kwargs: None)
    monkeypatch.setattr(
        sign.NebiusKmsSigner, "from_sdk", classmethod(lambda cls, sdk, key_id: cls(kms, key_id))
    )
    return kms


SIGN = ["sign", "--key-id", "kms-key-1", "--run-id", "r_01jb7z00000000000000000000", "--yes"]


async def _unsigned_bundle(tmp_path: Path, tamper: bool = False) -> Path:
    from app.bundle.build import BundleFile, Origin, assemble, pack

    meta = {"run_id": "r_01jb7z00000000000000000000", "verdict": "REPRODUCED"}
    files = [BundleFile("claims.json", b"[]", Origin.CONTROL_PLANE)]
    assembled = assemble(files, [], meta, ())
    files = {**assembled.files, "MANIFEST.json": assembled.manifest}
    if tamper:
        files["claims.json"] = b"[1]"
    path = tmp_path / "unsigned.tar.gz"
    path.write_bytes(pack(files))
    return path


def test_cli_exports_the_public_key(kms_cli: FakeKms, capsys: pytest.CaptureFixture[str]) -> None:
    from app.bundle.sign import main

    assert main(["public-key", "--key-id", "kms-key-1"]) == 0
    assert capsys.readouterr().out.startswith("-----BEGIN PUBLIC KEY-----")


async def test_cli_signs_a_bundle_that_verifies(kms_cli: FakeKms, tmp_path: Path) -> None:
    import asyncio

    from app.bundle.sign import main
    from app.bundle.verify import main as verify

    source, out = await _unsigned_bundle(tmp_path), tmp_path / "signed.tar.gz"
    key = tmp_path / "key.pem"  # the fake KMS key, never the published one in docs/
    key.write_text(await kms_cli.inner.public_key_pem())
    argv = [*SIGN, str(source), "-o", str(out), "--public-key", str(key)]
    code = await asyncio.to_thread(main, argv)
    assert code == 0
    assert verify([str(out), "--public-key", str(key)]) == 0


async def test_cli_never_signs_a_tampered_bundle(kms_cli: FakeKms, tmp_path: Path) -> None:
    import asyncio

    from app.bundle.sign import main

    source, out = await _unsigned_bundle(tmp_path, tamper=True), tmp_path / "signed.tar.gz"
    code = await asyncio.to_thread(main, [*SIGN, str(source), "-o", str(out)])
    assert code == 1 and not out.exists()
    assert not any(isinstance(r, AsymmetricSignHashRequest) for r in kms_cli.requests)


async def test_cli_never_replaces_a_signature(kms_cli: FakeKms, tmp_path: Path) -> None:
    """A signed bundle edited and re-manifested must not get a fresh signature."""
    import asyncio

    from app.bundle.build import pack
    from app.bundle.sign import main
    from app.bundle.verify import read_archive

    source = await _unsigned_bundle(tmp_path)
    files = read_archive(source.read_bytes())
    files["SIGNATURE"] = await sign_manifest(FakeSigner(), files["MANIFEST.json"])
    source.write_bytes(pack(files))
    out = tmp_path / "signed.tar.gz"
    code = await asyncio.to_thread(main, [*SIGN, str(source), "-o", str(out)])
    assert code == 1 and not out.exists()


async def test_cli_checks_the_run_id(kms_cli: FakeKms, tmp_path: Path) -> None:
    import asyncio

    from app.bundle.sign import main

    source, out = await _unsigned_bundle(tmp_path), tmp_path / "signed.tar.gz"
    other = ["sign", "--key-id", "kms-key-1", "--run-id", "r_" + "9" * 26, "--yes"]
    assert await asyncio.to_thread(main, [*other, str(source), "-o", str(out)]) == 1
    assert not out.exists()


async def test_a_pinned_key_that_differs_from_kms_refuses_to_sign() -> None:
    pinned = (await FakeSigner().public_key_pem()).encode()
    with pytest.raises(SigningError):
        await sign_manifest(FakeSigner(), MANIFEST, pinned)


async def test_kms_answering_for_another_key_is_refused() -> None:
    class Swapped(FakeKms):
        async def sign_hash(self, request: AsymmetricSignHashRequest) -> AsymmetricSignHashResponse:
            response = await super().sign_hash(request)
            return AsymmetricSignHashResponse(key_id="other", signature=response.signature)

    with pytest.raises(SigningError):
        await sign_manifest(NebiusKmsSigner(Swapped(), key_id="kms-key-1"), MANIFEST)


async def test_kms_public_key_must_be_p256() -> None:
    class NotAKey(FakeKms):
        async def get_public_key(
            self, request: AsymmetricGetPublicKeyRequest
        ) -> AsymmetricGetPublicKeyResponse:
            return AsymmetricGetPublicKeyResponse(key_id=request.key_id, public_key="nope")

    with pytest.raises(SigningError):
        await NebiusKmsSigner(NotAKey(), key_id="kms-key-1").public_key_pem()


async def test_cli_refuses_a_manifest_without_a_valid_verdict(
    kms_cli: FakeKms, tmp_path: Path
) -> None:
    import asyncio

    from app.bundle.build import BundleFile, Origin, assemble, pack
    from app.bundle.sign import main

    meta = {"run_id": "r_01jb7z00000000000000000000", "verdict": "REPRODUCED\x1b[2K"}
    assembled = assemble([BundleFile("a.json", b"{}", Origin.CONTROL_PLANE)], [], meta, ())
    source, out = tmp_path / "b.tar.gz", tmp_path / "signed.tar.gz"
    source.write_bytes(pack({**assembled.files, "MANIFEST.json": assembled.manifest}))
    assert await asyncio.to_thread(main, [*SIGN, str(source), "-o", str(out)]) == 1
    assert not out.exists()


def test_printable_drops_control_characters() -> None:
    from app.bundle.verify import printable

    assert printable("a\x1b[31mb\rc\u202e") == "a?[31mb?c?"
