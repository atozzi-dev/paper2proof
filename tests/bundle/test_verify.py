"""verify: a third party checks a downloaded bundle without trusting anything inside it."""

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from app.bundle.build import BundleFile, Origin, assemble, pack
from app.bundle.sign import FakeSigner, sign_manifest
from app.bundle.verify import BundleError, main, read_archive, read_bundle, verify_files

META = {"run_id": "r_01jb7z00000000000000000000", "arxiv_id": "2106.09685"}
FILES = {
    "claims.json": b'[{"id": "c1"}]',
    "verdict.json": b'{"verdict": "REPRODUCED"}',
    "results/1/stdout.log": b"epoch 1\nepoch 2\n",
}


async def _bundle(
    signer: FakeSigner | None, files: dict[str, bytes] | None = None
) -> dict[str, bytes]:
    assembled = assemble(
        [BundleFile(p, d, Origin.CONTROL_PLANE) for p, d in (files or FILES).items()],
        [],
        META,
        (),
    )
    out = {**assembled.files, "MANIFEST.json": assembled.manifest}
    if signer is not None:
        out["SIGNATURE"] = await sign_manifest(signer, assembled.manifest)
    return out


@pytest.fixture
def signer() -> FakeSigner:
    return FakeSigner()


async def _pem(signer: FakeSigner) -> bytes:
    return (await signer.public_key_pem()).encode()


async def test_an_untouched_bundle_verifies(signer: FakeSigner) -> None:
    files = read_archive(pack(await _bundle(signer)))
    assert verify_files(files, await _pem(signer)) == []


async def test_tampering_with_one_file_names_it(signer: FakeSigner) -> None:
    files = await _bundle(signer)
    files["results/1/stdout.log"] = b"epoch 1\nepoch 2 (edited)\n"
    problems = verify_files(files, await _pem(signer))
    assert len(problems) == 1
    assert problems[0].startswith("results/1/stdout.log:")


async def test_a_missing_and_an_extra_file_are_named(signer: FakeSigner) -> None:
    files = await _bundle(signer)
    del files["claims.json"]
    files["code/backdoor.py"] = b"print('hi')"
    problems = verify_files(files, await _pem(signer))
    assert any(p.startswith("claims.json:") and "missing" in p for p in problems)
    assert any(p.startswith("code/backdoor.py:") and "not listed" in p for p in problems)


async def test_an_edited_manifest_breaks_the_signature(signer: FakeSigner) -> None:
    files = await _bundle(signer)
    manifest = json.loads(files["MANIFEST.json"])
    manifest["verdict"] = "REPRODUCED"
    files["MANIFEST.json"] = json.dumps(manifest).encode()
    problems = verify_files(files, await _pem(signer))
    assert any(p.startswith("SIGNATURE:") for p in problems)


async def test_unsigned_bundles_fail_unless_allowed(signer: FakeSigner) -> None:
    files = await _bundle(None)
    assert any(p.startswith("SIGNATURE:") for p in verify_files(files, await _pem(signer)))
    assert verify_files(files, None, allow_unsigned=True) == []


async def test_a_signed_bundle_needs_a_public_key(signer: FakeSigner) -> None:
    problems = verify_files(await _bundle(signer), None)
    assert any(p.startswith("SIGNATURE:") and "public key" in p for p in problems)


async def test_a_manifest_that_lists_itself_or_escapes_is_rejected(signer: FakeSigner) -> None:
    files = await _bundle(signer)
    manifest = json.loads(files["MANIFEST.json"])
    manifest["files"]["../etc/passwd"] = {"sha256": "0" * 64, "bytes": 1, "origin": "x"}
    files["MANIFEST.json"] = json.dumps(manifest).encode()
    problems = verify_files(files, await _pem(signer), allow_unsigned=True)
    assert any("../etc/passwd" in p for p in problems)


def _tar(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def _file(name: str, data: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def _link(name: str, target: str, kind: bytes) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info, None


@pytest.mark.parametrize(
    "members",
    [
        [_file("bundle/../evil")],
        [_file("/bundle/abs")],
        [_file("other/MANIFEST.json")],
        [_link("bundle/claims.json", "/etc/passwd", tarfile.SYMTYPE)],
        [_link("bundle/claims.json", "bundle/x", tarfile.LNKTYPE)],
        [_file("bundle/a"), _file("bundle/a")],
        [_file("bundle\\win")],
    ],
)
def test_unsafe_archives_are_refused(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
    with pytest.raises(BundleError):
        read_archive(_tar(members))


def test_too_many_members_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.bundle.verify.MAX_MEMBERS", 3)
    with pytest.raises(BundleError):
        read_archive(_tar([_file(f"bundle/f{i}") for i in range(4)]))


def test_oversized_content_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.bundle.verify.MAX_TOTAL_BYTES", 10)
    with pytest.raises(BundleError):
        read_archive(_tar([_file("bundle/big", b"y" * 11)]))


def test_garbage_is_refused() -> None:
    with pytest.raises(BundleError):
        read_archive(b"not a tarball")
    with pytest.raises(BundleError):
        read_archive(gzip.compress(b"\x00" * 10))


async def test_the_packed_archive_is_deterministic(signer: FakeSigner) -> None:
    files = await _bundle(None)
    assert pack(files) == pack(dict(reversed(list(files.items()))))
    with tarfile.open(fileobj=io.BytesIO(pack(files)), mode="r:gz") as tar:
        infos = tar.getmembers()
    assert [i.name for i in infos] == sorted(f"bundle/{p}" for p in files)
    assert all(i.mtime == 0 and i.uid == 0 and i.gid == 0 and i.isreg() for i in infos)


async def test_cli_reports_the_tampered_file(
    signer: FakeSigner, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = tmp_path / "key.pem"
    key.write_bytes(await _pem(signer))
    files = await _bundle(signer)
    good = tmp_path / "good.tar.gz"
    good.write_bytes(pack(files))
    assert main([str(good), "--public-key", str(key)]) == 0
    assert "OK" in capsys.readouterr().out

    files["claims.json"] = b"[]"
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(pack(files))
    assert main([str(bad), "--public-key", str(key)]) == 1
    out = capsys.readouterr().out
    assert "FAIL claims.json:" in out


async def test_cli_reads_an_extracted_directory(signer: FakeSigner, tmp_path: Path) -> None:
    key = tmp_path / "key.pem"
    key.write_bytes(await _pem(signer))
    root = tmp_path / "x" / "bundle"
    for path, data in (await _bundle(signer)).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    assert read_bundle(root).keys() == (await _bundle(signer)).keys()
    assert main([str(root), "--public-key", str(key)]) == 0
    (root / "verdict.json").write_bytes(b'{"verdict": "NOT_REPRODUCED"}')
    assert main([str(root), "--public-key", str(key)]) == 1


def test_cli_usage_errors_are_exit_2(tmp_path: Path) -> None:
    assert main([str(tmp_path / "missing.tar.gz"), "--allow-unsigned"]) == 2
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_a_decompression_bomb_stops_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.bundle.verify.MAX_STREAM_BYTES", 64 * 1024)
    info = tarfile.TarInfo("bundle/a")
    info.pax_headers = {"comment": "x" * (256 * 1024)}  # read whole by tarfile: capped first
    with pytest.raises(BundleError, match="decompressed"):
        read_archive(_tar([(info, b"")]))


async def test_directories_with_links_are_refused(signer: FakeSigner, tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    for path, data in (await _bundle(signer)).items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    target = tmp_path / "outside.txt"
    target.write_text("secret")
    try:
        (root / "link.txt").symlink_to(target)
    except OSError:
        pytest.skip("this account cannot create symbolic links")
    with pytest.raises(BundleError):
        read_bundle(root)


def test_directory_reads_keep_to_the_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.bundle.verify.MAX_TOTAL_BYTES", 10)
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "MANIFEST.json").write_bytes(b"{}")
    (root / "big").write_bytes(b"z" * 20)
    with pytest.raises(BundleError):
        read_bundle(root)
