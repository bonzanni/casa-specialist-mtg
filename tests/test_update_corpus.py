"""update_corpus, and setup_corpus's use of a GitHub release's published digest.

No network: the one function that opens a socket is replaced by a fake GitHub
that answers the three requests the tools make (the latest release, an asset's
metadata, an asset's bytes behind a redirect to a storage host), so the
transport policy, hashing, extraction, verification and swap all run for real.
"""
import hashlib
import io
import json
import sqlite3
import tempfile
import urllib.error
from pathlib import Path

import pytest

import plugins.mtg.server.mtg_server as srv
import plugins.mtg.server.setup_corpus as sc
import plugins.mtg.server.update_corpus as uc
from tests.test_setup_corpus import _FakeResponse, _real_corpus_bytes, _tar_gz

API = "https://api.github.com/repos/owner/corpus"
STORAGE = "https://objects.example/blob/"
NAME = "corpus.tar.gz"


def _corpus(rules: str, cards: str) -> bytes:
    """A real minimal corpus whose meta rows name its rules and card dates."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "c.sqlite"
        path.write_bytes(_real_corpus_bytes(f"{rules}/{cards}"))
        con = sqlite3.connect(path)
        con.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("cr_effective_date", rules),
            ("scryfall_updated_at", f"{cards}T09:02:14.000+00:00")])
        con.commit()
        con.close()
        return path.read_bytes()


def _archive(corpus: bytes) -> bytes:
    sidecar = hashlib.sha256(corpus).hexdigest() + "\n"
    return _tar_gz({sc.CORPUS_NAME: corpus, sc.SIDECAR_NAME: sidecar.encode()})


class _FakeGitHub:
    """One repository's release assets by id; one release is the latest."""

    def __init__(self, api=API, storage=STORAGE):
        self.api, self.storage = api, storage
        self.assets = {}      # id -> (metadata dict, archive bytes)
        self.latest = None    # release JSON
        self.requests = []

    def publish(self, asset_id, tag, blob, *, digest="published", name=NAME,
                latest=True):
        repo = self.api.split("/repos/", 1)[1]
        meta = {
            "url": f"{self.api}/releases/assets/{asset_id}", "id": asset_id,
            "name": name,
            "browser_download_url":
                f"https://github.com/{repo}/releases/download/{tag}/{name}",
            "digest": ("sha256:" + hashlib.sha256(blob).hexdigest()
                       if digest == "published" else digest),
        }
        self.assets[asset_id] = (meta, blob)
        if latest:
            self.latest = {"tag_name": tag, "assets": [meta]}
        return meta

    def __call__(self, request, timeout):
        self.requests.append(request)
        url = request.full_url
        accept = request.get_header("Accept")
        if url == f"{self.api}/releases/latest" and self.latest is not None:
            return _FakeResponse(json.dumps(self.latest).encode())
        prefix = f"{self.api}/releases/assets/"
        if url.startswith(prefix) and int(url[len(prefix):]) in self.assets:
            asset_id = int(url[len(prefix):])
            if accept == "application/octet-stream":
                raise urllib.error.HTTPError(
                    url, 302, "Found",
                    {"Location": f"{self.storage}{asset_id}"}, None)
            return _FakeResponse(json.dumps(self.assets[asset_id][0]).encode())
        store = self.storage
        if url.startswith(store) and int(url[len(store):]) in self.assets:
            return _FakeResponse(self.assets[int(url[len(store):])][1])
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def downloads(self):
        return [r.full_url for r in self.requests
                if r.full_url.startswith(self.storage)]


OLD = _corpus("2026-08-01", "2026-11-02")
NEW = _corpus("2026-11-06", "2026-11-09")
OLD_BLOB, NEW_BLOB = _archive(OLD), _archive(NEW)


@pytest.fixture()
def github(monkeypatch):
    fake = _FakeGitHub()
    monkeypatch.setattr(sc, "_urlopen", fake)
    monkeypatch.setenv(sc.URL_ENV, f"{API}/releases/assets/1")
    monkeypatch.delenv(sc.SHA256_ENV, raising=False)
    monkeypatch.delenv(sc.TOKEN_ENV, raising=False)
    return fake


@pytest.fixture()
def installed(github, tmp_path):
    """Release v1 (asset 1) installed through setup_corpus, as casa does."""
    data_dir = tmp_path / "data"
    github.publish(1, "v1", OLD_BLOB)
    assert "corpus installed" in sc.run({}, data_dir=data_dir)
    github.requests.clear()
    return data_dir


def _state(data_dir):
    return {p.name: p.read_bytes() for p in sorted(Path(data_dir).iterdir())}


# --- setup_corpus: the checksum is optional for a GitHub release asset -----

def test_setup_uses_the_published_digest_and_records_the_release(installed):
    assert (installed / sc.CORPUS_NAME).read_bytes() == OLD
    record = sc.read_release(installed)
    assert record["tag"] == "v1" and record["asset_id"] == 1
    assert record["source"] == "owner/corpus"
    assert record["asset_name"] == NAME
    assert record["archive_sha256"] == hashlib.sha256(OLD_BLOB).hexdigest()
    assert record["installed_at"]


def test_a_hand_set_checksum_wins_over_the_published_digest(
        github, monkeypatch, tmp_path):
    github.publish(1, "v1", OLD_BLOB)
    monkeypatch.setenv(sc.SHA256_ENV, "a" * 64)
    with pytest.raises(sc.SetupError, match="sha256 mismatch"):
        sc.run({}, data_dir=tmp_path / "data")
    # The metadata was never asked for: the hand-set value is the pin.
    assert all(r.get_header("Accept") == "application/octet-stream"
               for r in github.requests)
    assert not (tmp_path / "data" / sc.CORPUS_NAME).exists()


def test_a_github_asset_without_a_published_digest_is_refused(github, tmp_path):
    github.publish(1, "v1", OLD_BLOB, digest=None)
    with pytest.raises(sc.SetupError, match="no sha256 digest"):
        sc.run({}, data_dir=tmp_path / "data")
    assert github.downloads() == []


def test_a_plain_url_still_needs_a_checksum(github, monkeypatch, tmp_path):
    monkeypatch.setenv(sc.URL_ENV, "https://corpus.example/asset.tar.gz")
    with pytest.raises(sc.SetupError, match=sc.SHA256_ENV):
        sc.run({}, data_dir=tmp_path / "data")
    assert github.requests == []


def test_a_forced_setup_from_a_plain_url_drops_the_old_release_record(
        installed, monkeypatch):
    """The record describes the corpus beside it; once that corpus is
    replaced from a source with no release, the record would be a lie."""
    plain = "https://corpus.example/asset.tar.gz"
    monkeypatch.setattr(sc, "_urlopen", lambda req, timeout: _FakeResponse(NEW_BLOB))
    sc.run({"url": plain, "sha256": hashlib.sha256(NEW_BLOB).hexdigest(),
            "force": True}, data_dir=installed)
    assert (installed / sc.CORPUS_NAME).read_bytes() == NEW
    assert not (installed / sc.RELEASE_NAME).exists()


# --- update_corpus: checking ------------------------------------------------

def test_a_pinned_source_changes_nothing(github, installed, monkeypatch):
    before = _state(installed)
    monkeypatch.setenv(sc.URL_ENV, "https://corpus.example/asset.tar.gz")
    out = uc.run({"install": True}, data_dir=installed)
    assert "pinned" in out and "nothing changed" in out
    assert github.requests == []
    assert _state(installed) == before


def test_up_to_date_does_nothing(github, installed):
    before = _state(installed)
    out = uc.run({"install": True}, data_dir=installed)
    assert out.startswith("Up to date") and "v1" in out
    assert github.downloads() == []
    assert _state(installed) == before


def test_a_check_reports_a_newer_release_and_changes_nothing(github, installed):
    github.publish(2, "v2", NEW_BLOB)
    before = _state(installed)
    out = uc.run({}, data_dir=installed)
    assert "v2" in out and "available" in out and "v1" in out
    assert github.downloads() == []
    assert _state(installed) == before


def test_a_check_without_a_record_says_the_installed_release_is_unknown(
        github, installed):
    (installed / sc.RELEASE_NAME).write_text("{not json")
    out = uc.run({}, data_dir=installed)
    assert "unknown" in out and "v1" in out
    assert github.downloads() == []


def test_the_latest_release_is_searched_for_the_configured_assets_name(
        github, installed, monkeypatch):
    github.publish(5, "v1", OLD_BLOB, name="corpus-it.tar.gz", latest=False)
    monkeypatch.setenv(sc.URL_ENV, f"{API}/releases/assets/5")
    github.publish(2, "v2", NEW_BLOB)  # the latest has only corpus.tar.gz
    with pytest.raises(sc.SetupError, match="no single asset named corpus-it"):
        uc.run({}, data_dir=installed)


def test_a_pruned_configured_asset_falls_back_to_the_recorded_name(
        github, installed):
    del github.assets[1]
    github.publish(2, "v2", NEW_BLOB)
    assert "v2" in uc.run({}, data_dir=installed)


IT = _corpus("2026-08-01", "2026-11-02-it")
IT_BLOB = _archive(IT)
IT_NEW = _corpus("2026-11-06", "2026-11-09-it")
IT_NEW_BLOB = _archive(IT_NEW)


def _two_variants(github, data_dir, monkeypatch):
    """Release v1 carries an English and an Italian-names corpus; the
    Italian one is configured and installed. Then v1's assets are pruned
    and v2 is published with both variants again."""
    en = github.publish(1, "v1", OLD_BLOB)
    it = github.publish(3, "v1", IT_BLOB, name="corpus-it.tar.gz")
    github.latest = {"tag_name": "v1", "assets": [en, it]}
    monkeypatch.setenv(sc.URL_ENV, f"{API}/releases/assets/3")
    sc.run({}, data_dir=data_dir)
    assert (data_dir / sc.CORPUS_NAME).read_bytes() == IT
    del github.assets[1], github.assets[3]
    en2 = github.publish(2, "v2", NEW_BLOB)
    it2 = github.publish(4, "v2", IT_NEW_BLOB, name="corpus-it.tar.gz")
    github.latest = {"tag_name": "v2", "assets": [en2, it2]}


def test_a_pruned_configured_variant_is_updated_to_the_same_variant(
        github, tmp_path, monkeypatch):
    """Review repro: with the configured asset's metadata gone, the update
    used to guess `corpus.tar.gz` and replaced the Italian corpus with the
    English one, reporting success. The recorded name decides instead."""
    data_dir = tmp_path / "data"
    _two_variants(github, data_dir, monkeypatch)
    uc.run({"install": True}, data_dir=data_dir)
    assert (data_dir / sc.CORPUS_NAME).read_bytes() == IT_NEW
    record = sc.read_release(data_dir)
    assert (record["asset_name"], record["asset_id"]) == ("corpus-it.tar.gz", 4)


def test_without_a_recorded_name_a_pruned_asset_changes_nothing(
        github, tmp_path, monkeypatch):
    """No metadata and no name recorded for this source: refuse, never
    guess."""
    data_dir = tmp_path / "data"
    _two_variants(github, data_dir, monkeypatch)
    record = json.loads((data_dir / sc.RELEASE_NAME).read_text())
    record["asset_name"] = None   # as a setup with a hand-set checksum leaves it
    (data_dir / sc.RELEASE_NAME).write_text(json.dumps(record))
    before = _state(data_dir)
    with pytest.raises(sc.SetupError, match="cannot tell which asset"):
        uc.run({"install": True}, data_dir=data_dir)
    assert _state(data_dir) == before


def test_a_record_from_another_source_is_not_up_to_date(
        github, installed, monkeypatch):
    """Review repro: the configured source moved to another repository whose
    asset happens to have the same id; the update said "Up to date". Ids and
    digests mean nothing across repositories, so the record is unknown."""
    other = _FakeGitHub(api="https://api.github.com/repos/Other/corpus-b",
                        storage="https://objects.example/other/")
    other.publish(1, "w1", NEW_BLOB)

    def transport(request, timeout):
        url = request.full_url
        fake = other if url.startswith((other.api, other.storage)) else github
        return fake(request, timeout)

    monkeypatch.setattr(sc, "_urlopen", transport)
    monkeypatch.setenv(sc.URL_ENV, f"{other.api}/releases/assets/1")
    out = uc.run({}, data_dir=installed)
    assert not out.startswith("Up to date")
    assert "unknown" in out and "w1" in out
    uc.run({"install": True}, data_dir=installed)
    assert (installed / sc.CORPUS_NAME).read_bytes() == NEW
    assert sc.read_release(installed)["source"] == "other/corpus-b"


@pytest.mark.parametrize("record", [
    {"tag": "v1", "asset_id": 1},                                    # no source
    {"source": "owner/corpus", "asset_id": "1",
     "archive_sha256": "a" * 64},                                    # id as text
    {"source": "owner/corpus", "asset_id": 1, "archive_sha256": "x"},
    ["owner/corpus", 1],
])
def test_an_invalid_record_counts_as_unknown(github, installed, record):
    (installed / sc.RELEASE_NAME).write_text(json.dumps(record))
    assert sc.read_release(installed) is None
    assert "unknown" in uc.run({}, data_dir=installed)


def test_a_private_repository_gets_the_token_on_the_api_host_only(
        github, installed, monkeypatch):
    monkeypatch.setenv(sc.TOKEN_ENV, "s3cret")
    github.publish(2, "v2", NEW_BLOB)
    uc.run({"install": True}, data_dir=installed)
    for request in github.requests:
        expected = None if request.full_url.startswith(STORAGE) else "Bearer s3cret"
        assert request.get_header("Authorization") == expected, request.full_url
    assert github.downloads() == [f"{STORAGE}2"]


# --- update_corpus: installing ----------------------------------------------

def test_install_switches_to_the_newer_release_and_says_what_changed(
        github, installed, monkeypatch):
    github.publish(2, "v2", NEW_BLOB)
    monkeypatch.setattr(srv, "DB_PATH", installed / sc.CORPUS_NAME)
    srv._reset_db()
    assert "2026-08-01" in srv._corpus_info()   # a live connection to v1
    response = srv._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "update_corpus",
                                       "arguments": {"install": True}}})
    text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is False
    assert text.splitlines()[0] == (
        "Now on the 2026-11-06 rules and the 2026-11-09 cards (release v2).")
    # corpus_info describes the new corpus: the cached connection was dropped.
    assert "cr_effective_date=2026-11-06" in text
    assert (installed / sc.CORPUS_NAME).read_bytes() == NEW
    record = sc.read_release(installed)
    assert (record["tag"], record["asset_id"]) == ("v2", 2)
    assert record["archive_sha256"] == hashlib.sha256(NEW_BLOB).hexdigest()
    srv._reset_db()


def test_install_without_a_record_installs_the_latest(github, installed):
    (installed / sc.RELEASE_NAME).unlink()
    github.publish(2, "v2", NEW_BLOB)
    uc.run({"install": True}, data_dir=installed)
    assert (installed / sc.CORPUS_NAME).read_bytes() == NEW
    assert sc.read_release(installed)["tag"] == "v2"


def test_a_digest_mismatch_keeps_the_current_corpus(
        github, installed, monkeypatch):
    """The published digest is the pin: bytes that do not hash to it are
    refused, and the corpus, sidecar and record in place stay exactly as
    they were."""
    monkeypatch.setattr(srv, "DB_PATH", installed / sc.CORPUS_NAME)
    github.publish(2, "v2", NEW_BLOB, digest="sha256:" + "b" * 64)
    before = _state(installed)
    response = srv._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "update_corpus",
                                       "arguments": {"install": True}}})
    assert response["result"]["isError"] is True
    assert "sha256 mismatch" in response["result"]["content"][0]["text"]
    assert _state(installed) == before


def test_a_release_without_a_digest_installs_nothing(github, installed):
    github.publish(2, "v2", NEW_BLOB, digest=None)
    before = _state(installed)
    with pytest.raises(sc.SetupError, match="cannot be verified"):
        uc.run({"install": True}, data_dir=installed)
    assert github.downloads() == [] and _state(installed) == before


def test_a_release_that_is_not_a_corpus_keeps_the_current_one(github, installed):
    """Same verified path as setup: a well-hashed archive that holds the
    wrong database is refused after download, before the swap."""
    junk = b"SQLite" + b" format 3\x00" + b"\x00" * 100
    blob = _tar_gz({sc.CORPUS_NAME: junk, sc.SIDECAR_NAME:
                    (hashlib.sha256(junk).hexdigest() + "\n").encode()})
    github.publish(2, "v2", blob)
    before = _state(installed)
    with pytest.raises(sc.SetupError):
        uc.run({"install": True}, data_dir=installed)
    assert _state(installed) == before


def test_update_without_a_configured_url_says_so(monkeypatch, tmp_path):
    monkeypatch.delenv(sc.URL_ENV, raising=False)
    with pytest.raises(sc.SetupError, match=sc.URL_ENV):
        uc.run({}, data_dir=tmp_path)


# --- wiring -----------------------------------------------------------------

def test_update_corpus_is_argument_free_and_only_checks_by_default():
    definition = {d["name"]: d for d in srv._tool_defs()}["update_corpus"]
    assert definition["inputSchema"]["required"] == []
    assert definition["inputSchema"]["properties"]["install"]["type"] == "boolean"


@pytest.mark.parametrize("url, expected", [
    (f"{API}/releases/assets/123", ("owner", "corpus", 123)),
    ("https://API.github.com/repos/o/r/releases/assets/9/", ("o", "r", 9)),
    ("http://api.github.com/repos/o/r/releases/assets/9", None),
    ("https://api.github.com.evil.example/repos/o/r/releases/assets/9", None),
    ("https://api.github.com/repos/o/r/releases/assets/9?x=1", None),
    ("https://github.com/o/r/releases/download/v1/corpus.tar.gz", None),
    ("https://api.github.com/repos/o/r/releases/latest", None),
])
def test_only_a_github_release_asset_api_url_is_a_release_source(url, expected):
    assert sc.github_asset(url) == expected


def test_only_the_url_is_required_of_the_operator():
    """Casa withholds a plugin until every `${VAR}` in .mcp.json resolves;
    `${VAR:-}` is optional. The checksum became optional with GitHub release
    sources, so it must not hold the plugin back."""
    root = Path(__file__).resolve().parent.parent
    env = json.loads((root / "plugins/mtg/.mcp.json").read_text())[
        "mcpServers"]["mtg"]["env"]
    assert env[sc.URL_ENV] == "${%s}" % sc.URL_ENV
    assert env[sc.SHA256_ENV] == "${%s:-}" % sc.SHA256_ENV
    assert env[sc.TOKEN_ENV] == "${%s:-}" % sc.TOKEN_ENV
