"""Corpus setup-tool tests. No network: the one function that opens a socket
is replaced with a fake that serves bytes from memory, so everything above
it — scheme check, redirect policy, caps, hashing, extraction — runs for
real."""
import hashlib
import pathlib
import io
import os
import tarfile
import time
import urllib.error
from pathlib import Path

import pytest

import plugins.mtg.server.setup_corpus as sc

ORIGIN = "https://corpus.example/asset.tar.gz"


# --- fake transport ----------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: bytes, headers=None, chunk_delay=0.0):
        self._stream = io.BytesIO(body)
        self.headers = headers if headers is not None else {}
        self._delay = chunk_delay
        self.closed = False

    def read(self, size=-1):
        if self._delay:
            time.sleep(self._delay)
        return self._stream.read(size)

    def close(self):
        self.closed = True


class _Transport:
    """Records every request and answers from a url -> outcome table."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        outcome = self.routes.get(request.full_url)
        if outcome is None:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _redirect(url, location, code=302):
    return urllib.error.HTTPError(url, code, "Found", {"Location": location}, None)


def _install(monkeypatch, transport, **kwargs):
    monkeypatch.setattr(sc, "_urlopen", transport)
    body = kwargs.pop("body", None)
    if body is not None:
        kwargs.setdefault("expected_sha256", hashlib.sha256(body).hexdigest())
    return sc.install_corpus(url=kwargs.pop("url", ORIGIN), **kwargs)


# --- fixtures ----------------------------------------------------------------

def _tar_gz(entries, extra_members=()):
    """entries: {name: bytes}; extra_members: (TarInfo, fileobj|None) pairs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        for info, fh in extra_members:
            tar.addfile(info, fh)
    return buf.getvalue()


def _real_corpus_bytes(payload: str = "rows") -> bytes:
    """A genuine minimal corpus: the installer verifies the file is really an
    MTG corpus before installing it, so a fixture of magic bytes plus filler
    would only ever exercise the rejection path."""
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "c.sqlite"
        con = sqlite3.connect(path)
        # The FTS tables belong here even though nothing in these tests
        # queries them: installation verifies them, because a corpus without
        # them installs cleanly and then answers "no rules match" to every
        # search. A fixture that omitted them could only ever have tested the
        # rejection path while looking like it tested the happy one.
        con.executescript("""
          CREATE TABLE rules(rule_id TEXT PRIMARY KEY, parent_id TEXT,
                             text TEXT, examples TEXT);
          CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id, text);
          CREATE TABLE glossary(term TEXT PRIMARY KEY, definition TEXT);
          CREATE TABLE cards(oracle_id TEXT PRIMARY KEY, name TEXT,
                             name_lower TEXT, type_line TEXT,
                             oracle_text TEXT, keywords TEXT);
          CREATE VIRTUAL TABLE cards_fts USING fts5(name, oracle_id UNINDEXED);
          CREATE TABLE card_aliases(printed_lower TEXT, lang TEXT, oracle_id TEXT);
          CREATE TABLE rulings(oracle_id TEXT, published_at TEXT,
                               source TEXT, comment TEXT);
          CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        con.execute("INSERT INTO meta VALUES ('payload', ?)", (payload,))
        con.commit()
        con.close()
        return path.read_bytes()


@pytest.fixture()
def asset():
    """A well-formed archive plus its own sha256, as the operator would pin it."""
    corpus = _real_corpus_bytes("pretend corpus")
    sidecar = hashlib.sha256(corpus).hexdigest() + "\n"
    blob = _tar_gz({sc.CORPUS_NAME: corpus,
                    sc.SIDECAR_NAME: sidecar.encode()})
    return blob, hashlib.sha256(blob).hexdigest(), corpus


@pytest.fixture()
def data_dir(tmp_path):
    return tmp_path / "data"


# --- happy path --------------------------------------------------------------

def test_installs_corpus_and_sidecar(monkeypatch, asset, data_dir):
    blob, digest, corpus = asset
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    out = _install(monkeypatch, transport, expected_sha256=digest, data_dir=data_dir)
    assert (data_dir / sc.CORPUS_NAME).read_bytes() == corpus
    assert (data_dir / sc.SIDECAR_NAME).read_text().strip() == \
        hashlib.sha256(corpus).hexdigest()
    assert "corpus installed" in out
    # Nothing staged survives a successful run either.
    assert not [p for p in data_dir.iterdir() if p.name.startswith(".corpus-setup-")]


def test_run_reads_operator_config_from_env(monkeypatch, asset, data_dir):
    blob, digest, _ = asset
    monkeypatch.setattr(sc, "_urlopen", _Transport({ORIGIN: _FakeResponse(blob)}))
    monkeypatch.setenv(sc.PLUGIN_DATA_ENV, str(data_dir))
    monkeypatch.setenv(sc.URL_ENV, ORIGIN)
    monkeypatch.setenv(sc.SHA256_ENV, digest)
    monkeypatch.delenv(sc.TOKEN_ENV, raising=False)
    assert "corpus installed" in sc.run({})
    assert (data_dir / sc.CORPUS_NAME).exists()


def test_run_accepts_explicit_arguments_without_any_environment(
        monkeypatch, asset, data_dir):
    blob, digest, _ = asset
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    monkeypatch.setattr(sc, "_urlopen", transport)
    for name in (sc.URL_ENV, sc.SHA256_ENV, sc.TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    sc.run({"url": ORIGIN, "sha256": digest, "token": "s3cret"},
           data_dir=data_dir)
    assert (data_dir / sc.CORPUS_NAME).exists()
    assert transport.requests[0].get_header("Authorization") == "Bearer s3cret"


def test_explicit_arguments_win_over_the_environment(monkeypatch, asset, data_dir):
    blob, digest, _ = asset
    other = "https://corpus.example/other.tar.gz"
    transport = _Transport({other: _FakeResponse(blob)})
    monkeypatch.setattr(sc, "_urlopen", transport)
    monkeypatch.setenv(sc.URL_ENV, ORIGIN)
    monkeypatch.setenv(sc.SHA256_ENV, digest)
    sc.run({"url": other}, data_dir=data_dir)
    assert [r.full_url for r in transport.requests] == [other]


def test_run_without_configuration_names_both_delivery_paths(monkeypatch, data_dir):
    monkeypatch.delenv(sc.URL_ENV, raising=False)
    monkeypatch.delenv(sc.SHA256_ENV, raising=False)
    with pytest.raises(sc.SetupError) as exc:
        sc.run({}, data_dir=data_dir)
    message = str(exc.value)
    assert sc.URL_ENV in message and sc.SHA256_ENV in message
    assert "arguments" in message


# --- where the corpus lives --------------------------------------------------

def test_data_dir_prefers_the_writable_plugin_data_directory(monkeypatch, tmp_path):
    monkeypatch.setenv(sc.PLUGIN_DATA_ENV, str(tmp_path / "plugin-data"))
    assert sc.default_data_dir() == tmp_path / "plugin-data"


def test_data_dir_falls_back_to_the_bundled_directory(monkeypatch):
    monkeypatch.delenv(sc.PLUGIN_DATA_ENV, raising=False)
    assert sc.default_data_dir() == sc.BUNDLED_DATA_DIR


def test_setup_never_writes_into_the_checksummed_plugin_tree(
        monkeypatch, asset, tmp_path):
    """Casa verifies the installed plugin tree by content; a corpus written
    into it would make the next reload read the plugin as corrupt."""
    blob, digest, _ = asset
    external = tmp_path / "plugin-data"
    monkeypatch.setattr(sc, "_urlopen", _Transport({ORIGIN: _FakeResponse(blob)}))
    monkeypatch.setenv(sc.PLUGIN_DATA_ENV, str(external))
    monkeypatch.setenv(sc.URL_ENV, ORIGIN)
    monkeypatch.setenv(sc.SHA256_ENV, digest)
    before = sorted(p.name for p in sc.BUNDLED_DATA_DIR.iterdir()) \
        if sc.BUNDLED_DATA_DIR.exists() else []
    sc.run({})
    assert (external / sc.CORPUS_NAME).exists()
    after = sorted(p.name for p in sc.BUNDLED_DATA_DIR.iterdir()) \
        if sc.BUNDLED_DATA_DIR.exists() else []
    assert before == after


# --- transport policy --------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://corpus.example/asset.tar.gz",
    "file:///etc/passwd",
    "ftp://corpus.example/asset.tar.gz",
    "/local/asset.tar.gz",
])
def test_non_https_url_is_refused(monkeypatch, data_dir, url):
    transport = _Transport({})
    with pytest.raises(sc.SetupError, match="https"):
        _install(monkeypatch, transport, url=url, data_dir=data_dir,
                 expected_sha256="0" * 64)
    assert transport.requests == []
    assert not data_dir.exists()


def test_same_host_redirect_is_followed_with_the_token(monkeypatch, asset, data_dir):
    blob, digest, _ = asset
    hop = "https://corpus.example/blobs/asset.tar.gz"
    transport = _Transport({ORIGIN: _redirect(ORIGIN, hop),
                            hop: _FakeResponse(blob)})
    _install(monkeypatch, transport, expected_sha256=digest, data_dir=data_dir,
             token="s3cret")
    assert [r.full_url for r in transport.requests] == [ORIGIN, hop]
    assert all(r.get_header("Authorization") == "Bearer s3cret"
               for r in transport.requests)


def test_cross_host_redirect_completes_but_drops_the_token(
        monkeypatch, asset, data_dir):
    """A release asset normally redirects to a separate storage host, so the
    hop has to work — but the credential stops at the host boundary."""
    blob, digest, corpus = asset
    storage = "https://assets.example/blob/1"
    transport = _Transport({ORIGIN: _redirect(ORIGIN, storage),
                            storage: _FakeResponse(blob)})
    _install(monkeypatch, transport, expected_sha256=digest, data_dir=data_dir,
             token="s3cret")
    assert (data_dir / sc.CORPUS_NAME).read_bytes() == corpus
    sent = {r.full_url: r.get_header("Authorization") for r in transport.requests}
    assert sent == {ORIGIN: "Bearer s3cret", storage: None}


def test_a_request_to_another_host_carries_no_token():
    """Guarded where the header is attached, not only in the redirect loop."""
    request = sc._build_request("https://attacker.example/a", "s3cret",
                                "corpus.example")
    assert request.get_header("Authorization") is None
    assert sc._build_request(ORIGIN, "s3cret", "corpus.example").get_header(
        "Authorization") == "Bearer s3cret"


def test_redirect_to_http_is_refused(monkeypatch, data_dir):
    downgrade = "http://corpus.example/asset.tar.gz"
    transport = _Transport({ORIGIN: _redirect(ORIGIN, downgrade)})
    with pytest.raises(sc.SetupError, match="https"):
        _install(monkeypatch, transport, expected_sha256="0" * 64,
                 data_dir=data_dir)


def test_redirect_loop_is_bounded(monkeypatch, data_dir):
    transport = _Transport({ORIGIN: _redirect(ORIGIN, ORIGIN)})
    with pytest.raises(sc.SetupError, match="redirects"):
        _install(monkeypatch, transport, expected_sha256="0" * 64,
                 data_dir=data_dir)
    assert len(transport.requests) == sc.MAX_REDIRECTS + 1


# --- streaming limits --------------------------------------------------------

def test_oversized_download_stops_mid_stream(monkeypatch, data_dir):
    blob = b"x" * (sc._CHUNK * 8)
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="byte cap"):
        _install(monkeypatch, transport, expected_sha256="0" * 64,
                 data_dir=data_dir, max_download_bytes=sc._CHUNK * 2)
    # Stopped while reading, not after: the whole body was never consumed.
    assert transport.requests and not (data_dir / sc.CORPUS_NAME).exists()


def test_declared_oversize_is_refused_before_reading(monkeypatch, data_dir):
    transport = _Transport({ORIGIN: _FakeResponse(
        b"x" * 32, headers={"Content-Length": str(10 ** 9)})})
    with pytest.raises(sc.SetupError, match="declares"):
        _install(monkeypatch, transport, expected_sha256="0" * 64,
                 data_dir=data_dir, max_download_bytes=1024)


def test_timeout_fires_while_the_body_is_still_arriving(monkeypatch, data_dir):
    # Many small chunks, each slow enough that the deadline lands mid-body.
    blob = b"x" * (sc._CHUNK * 20)
    transport = _Transport({ORIGIN: _FakeResponse(blob, chunk_delay=0.01)})
    with pytest.raises(sc.SetupError, match="exceeded"):
        _install(monkeypatch, transport, expected_sha256="0" * 64,
                 data_dir=data_dir, timeout=0.03)
    assert not (data_dir / sc.CORPUS_NAME).exists()


# --- integrity ---------------------------------------------------------------

def test_wrong_asset_hash_is_refused_before_extraction(monkeypatch, asset, data_dir):
    blob, _, _ = asset
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="sha256 mismatch"):
        _install(monkeypatch, transport, expected_sha256="a" * 64,
                 data_dir=data_dir)
    assert not (data_dir / sc.CORPUS_NAME).exists()
    assert not (data_dir / sc.SIDECAR_NAME).exists()


def test_expected_hash_must_be_a_sha256(monkeypatch, data_dir):
    transport = _Transport({})
    with pytest.raises(sc.SetupError, match="64 hexadecimal"):
        _install(monkeypatch, transport, expected_sha256="not-a-hash",
                 data_dir=data_dir)
    assert transport.requests == []


def test_sidecar_that_disagrees_with_the_corpus_is_refused(monkeypatch, data_dir):
    blob = _tar_gz({sc.CORPUS_NAME: b"the real bytes",
                    sc.SIDECAR_NAME: ("b" * 64 + "\n").encode()})
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="hashes to"):
        _install(monkeypatch, transport, body=blob, data_dir=data_dir)
    assert not (data_dir / sc.CORPUS_NAME).exists()


def test_sidecar_that_is_not_a_sha256_is_refused(monkeypatch, data_dir):
    blob = _tar_gz({sc.CORPUS_NAME: b"bytes", sc.SIDECAR_NAME: b"whatever\n"})
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="does not contain a sha256"):
        _install(monkeypatch, transport, body=blob, data_dir=data_dir)


def test_archive_missing_the_corpus_is_refused(monkeypatch, data_dir):
    blob = _tar_gz({sc.SIDECAR_NAME: ("c" * 64 + "\n").encode()})
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="missing corpus.sqlite"):
        _install(monkeypatch, transport, body=blob, data_dir=data_dir)


# --- archive safety ----------------------------------------------------------

def test_traversal_member_is_refused(monkeypatch, data_dir, tmp_path):
    blob = _tar_gz({"../escaped": b"nope", sc.CORPUS_NAME: b"bytes"})
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="escapes the target directory"):
        _install(monkeypatch, transport, body=blob, data_dir=data_dir)
    assert not (tmp_path / "escaped").exists()


def test_leading_dot_slash_names_still_resolve(monkeypatch, asset, data_dir):
    """`tar czf x.tar.gz .` prefixes every name with ./ — same archive."""
    corpus = _real_corpus_bytes()
    sidecar = hashlib.sha256(corpus).hexdigest() + "\n"
    blob = _tar_gz({"./" + sc.CORPUS_NAME: corpus,
                    "./" + sc.SIDECAR_NAME: sidecar.encode()})
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    _install(monkeypatch, transport, body=blob, data_dir=data_dir)
    assert (data_dir / sc.CORPUS_NAME).read_bytes() == corpus


def test_absolute_member_is_refused(monkeypatch, data_dir):
    blob = _tar_gz({"/etc/cron.d/pwn": b"nope", sc.CORPUS_NAME: b"bytes"})
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="plain relative path"):
        _install(monkeypatch, transport, body=blob, data_dir=data_dir)


def test_symlink_member_is_refused(monkeypatch, data_dir):
    link = tarfile.TarInfo(sc.CORPUS_NAME)
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    blob = _tar_gz({}, extra_members=[(link, None)])
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="is a link"):
        _install(monkeypatch, transport, body=blob, data_dir=data_dir)


def test_hardlink_member_is_refused(monkeypatch, data_dir):
    link = tarfile.TarInfo("hard")
    link.type = tarfile.LNKTYPE
    link.linkname = sc.CORPUS_NAME
    blob = _tar_gz({}, extra_members=[(link, None)])
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="is a link"):
        _install(monkeypatch, transport, body=blob, data_dir=data_dir)


def test_device_member_is_refused(monkeypatch, data_dir):
    dev = tarfile.TarInfo("zero")
    dev.type = tarfile.CHRTYPE
    dev.devmajor, dev.devminor = 1, 5
    blob = _tar_gz({}, extra_members=[(dev, None)])
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="not a regular file"):
        _install(monkeypatch, transport, body=blob, data_dir=data_dir)


def test_decompression_bomb_is_refused_before_it_is_written(monkeypatch, data_dir):
    # Compresses to a few KB; the declared size is what the cap is measured
    # against, so nothing is written to reach the refusal.
    bomb = b"\0" * (4 * 1024 * 1024)
    blob = _tar_gz({sc.CORPUS_NAME: bomb, sc.SIDECAR_NAME: b"x"})
    assert len(blob) < 64 * 1024
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="expands past"):
        _install(monkeypatch, transport, body=blob, data_dir=data_dir,
                 max_extracted_bytes=1024)
    assert not (data_dir / sc.CORPUS_NAME).exists()


# --- overwrite and cleanup ---------------------------------------------------

def test_existing_corpus_is_not_replaced_without_force(monkeypatch, asset, data_dir):
    blob, digest, _ = asset
    data_dir.mkdir(parents=True)
    (data_dir / sc.CORPUS_NAME).write_bytes(b"the corpus in use")
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError, match="already exists"):
        _install(monkeypatch, transport, expected_sha256=digest, data_dir=data_dir)
    assert (data_dir / sc.CORPUS_NAME).read_bytes() == b"the corpus in use"
    assert transport.requests == []  # refused before anything is downloaded


def test_force_replaces_an_existing_corpus(monkeypatch, asset, data_dir):
    blob, digest, corpus = asset
    data_dir.mkdir(parents=True)
    (data_dir / sc.CORPUS_NAME).write_bytes(b"the corpus in use")
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    _install(monkeypatch, transport, expected_sha256=digest, data_dir=data_dir,
             force=True)
    assert (data_dir / sc.CORPUS_NAME).read_bytes() == corpus


@pytest.mark.parametrize("force", [False, True])
def test_a_failed_install_leaves_nothing_behind(monkeypatch, asset, data_dir, force):
    blob, _, _ = asset
    if force:
        data_dir.mkdir(parents=True)
        (data_dir / sc.CORPUS_NAME).write_bytes(b"the corpus in use")
    transport = _Transport({ORIGIN: _FakeResponse(blob)})
    with pytest.raises(sc.SetupError):
        _install(monkeypatch, transport, expected_sha256="f" * 64,
                 data_dir=data_dir, force=force)
    survivors = sorted(p.name for p in data_dir.iterdir()) if data_dir.exists() else []
    assert survivors == ([sc.CORPUS_NAME] if force else [])
    if force:
        assert (data_dir / sc.CORPUS_NAME).read_bytes() == b"the corpus in use"


# --- tool wiring -------------------------------------------------------------

def test_setup_corpus_is_an_argument_free_tool():
    import plugins.mtg.server.mtg_server as srv
    definition = {d["name"]: d for d in srv._tool_defs()}["setup_corpus"]
    # Casa runs the tool with no arguments, so nothing may be required; the
    # same inputs are still offered for a hand-run outside casa.
    assert definition["inputSchema"]["required"] == []
    assert set(definition["inputSchema"]["properties"]) == {
        "url", "sha256", "token", "force"}


def test_read_only_tools_still_require_their_first_argument():
    import plugins.mtg.server.mtg_server as srv
    definitions = {d["name"]: d for d in srv._tool_defs()}
    assert definitions["lookup_rule"]["inputSchema"]["required"] == ["rule_id"]
    assert definitions["get_rulings"]["inputSchema"]["required"] == ["oracle_id"]


def test_query_path_import_pulls_in_no_networking(tmp_path):
    """The read-only server must not reach urllib or tarfile just by loading."""
    import subprocess
    import sys
    root = Path(__file__).resolve().parent.parent
    probe = (
        "import sys;"
        "sys.path.insert(0, %r);"
        "import plugins.mtg.server.mtg_server;"
        "print('urllib.request' in sys.modules, 'tarfile' in sys.modules,"
        " 'plugins.mtg.server.setup_corpus' in sys.modules)" % str(root)
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == "False False False"


def test_tool_call_reports_a_setup_failure_as_an_error(monkeypatch):
    import plugins.mtg.server.mtg_server as srv
    monkeypatch.delenv(sc.URL_ENV, raising=False)
    monkeypatch.delenv(sc.SHA256_ENV, raising=False)
    response = srv._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "setup_corpus", "arguments": {}}})
    assert response["result"]["isError"] is True
    assert "not configured" in response["result"]["content"][0]["text"]


def test_tool_installs_and_drops_the_cached_connection(monkeypatch, asset, tmp_path):
    import plugins.mtg.server.mtg_server as srv
    blob, digest, _ = asset
    data_dir = tmp_path / "data"
    monkeypatch.setattr(sc, "_urlopen", _Transport({ORIGIN: _FakeResponse(blob)}))
    monkeypatch.setattr(srv, "DB_PATH", data_dir / sc.CORPUS_NAME)
    monkeypatch.setenv(sc.URL_ENV, ORIGIN)
    monkeypatch.setenv(sc.SHA256_ENV, digest)

    real_reset, resets = srv._reset_db, []

    def _spy():
        resets.append(True)
        real_reset()

    monkeypatch.setattr(srv, "_reset_db", _spy)
    out = srv.tool_setup_corpus({})
    # The fixture is a real corpus, so corpus_info resolves against the
    # freshly installed file rather than degrading — which is the point: the
    # cached read-only connection must have been dropped, or this line would
    # still describe the corpus that was there before.
    assert "corpus installed" in out
    assert "corpus_info: payload=pretend corpus" in out
    assert "artifact_sha256=" in out and "unavailable" not in out
    assert resets, "a replaced corpus must not keep being served from cache"
    assert os.path.exists(data_dir / sc.CORPUS_NAME)


def test_a_corpus_without_the_fts_tables_is_refused(tmp_path):
    """Base tables alone are not enough. Without rules_fts a search returns a
    confident "no rules match" for every query, and without cards_fts or
    card_aliases a fuzzy or Italian lookup raises at query time — long after
    setup reported success."""
    import sqlite3

    path = tmp_path / sc.CORPUS_NAME
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE rules(rule_id TEXT, parent_id TEXT, text TEXT, examples TEXT);"
        "CREATE TABLE glossary(term TEXT, definition TEXT);"
        "CREATE TABLE cards(oracle_id TEXT, name TEXT, name_lower TEXT,"
        "                   type_line TEXT, oracle_text TEXT);"
        "CREATE TABLE rulings(oracle_id TEXT, published_at TEXT, source TEXT,"
        "                     comment TEXT);"
        "CREATE TABLE meta(key TEXT, value TEXT);")
    con.commit()
    con.close()
    with pytest.raises(sc.SetupError, match="rules_fts|cards_fts|card_aliases"):
        sc._verify_is_corpus(path)


def test_ordinary_tables_masquerading_as_fts_are_refused(tmp_path):
    """Every column check passes, then MATCH fails and search answers "no
    rules match" to questions the corpus can answer — a confident wrong
    answer, which is the failure this component exists to prevent."""
    import sqlite3

    path = tmp_path / sc.CORPUS_NAME
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE rules(rule_id, parent_id, text, examples);"
        "CREATE TABLE glossary(term, definition);"
        "CREATE TABLE cards(oracle_id, name, name_lower, type_line, oracle_text);"
        "CREATE TABLE rulings(oracle_id, published_at, source, comment);"
        "CREATE TABLE meta(key, value);"
        "CREATE TABLE rules_fts(rule_id, text);"      # not virtual
        "CREATE TABLE cards_fts(name, oracle_id);"    # not virtual
        "CREATE TABLE card_aliases(printed_lower, lang, oracle_id);")
    con.commit()
    con.close()
    with pytest.raises(sc.SetupError, match="not an FTS table"):
        sc._verify_is_corpus(path)


def test_an_fts_table_with_the_wrong_columns_is_refused(tmp_path):
    import sqlite3

    path = tmp_path / sc.CORPUS_NAME
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE rules(rule_id, parent_id, text, examples);"
        "CREATE TABLE glossary(term, definition);"
        "CREATE TABLE cards(oracle_id, name, name_lower, type_line, oracle_text);"
        "CREATE TABLE rulings(oracle_id, published_at, source, comment);"
        "CREATE TABLE meta(key, value);"
        "CREATE VIRTUAL TABLE rules_fts USING fts5(wrong_column);"
        "CREATE VIRTUAL TABLE cards_fts USING fts5(name, oracle_id UNINDEXED);"
        "CREATE TABLE card_aliases(printed_lower, lang, oracle_id);")
    con.commit()
    con.close()
    with pytest.raises(sc.SetupError, match="rules_fts"):
        sc._verify_is_corpus(path)


def test_a_replacement_never_pairs_a_new_corpus_with_an_old_sidecar(
        monkeypatch, asset, data_dir):
    """The two renames are not atomic, and the comment claimed the window
    degrades to `unknown` without arranging for it: the OLD sidecar survived
    until the second rename, so an interruption left the new corpus beside
    stale provenance — which the server reports as fact, in every citation.

    Absent provenance is handled. Wrong provenance is not.
    """
    import os as _os

    blob, digest, corpus = asset
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / sc.CORPUS_NAME).write_bytes(b"an older corpus")
    (data_dir / sc.SIDECAR_NAME).write_text("0" * 64 + "\n")
    stale = (data_dir / sc.SIDECAR_NAME).read_text()

    seen = []
    real_replace = _os.replace

    def watched(src, dst):
        real_replace(src, dst)
        seen.append({
            "corpus": (data_dir / sc.CORPUS_NAME).read_bytes()
                      if (data_dir / sc.CORPUS_NAME).exists() else None,
            "sidecar": (data_dir / sc.SIDECAR_NAME).read_text()
                       if (data_dir / sc.SIDECAR_NAME).exists() else None,
        })

    monkeypatch.setattr(sc.os, "replace", watched)
    _install(monkeypatch, _Transport({ORIGIN: _FakeResponse(blob)}),
             body=blob, data_dir=data_dir, force=True)

    for state in seen:
        if state["corpus"] == corpus:
            assert state["sidecar"] != stale, (
                "the new corpus was visible beside the old sidecar; an "
                "interruption there ships wrong provenance")


def test_a_corpus_without_mana_cost_still_installs(tmp_path):
    """_REQUIRED_SCHEMA is deliberately a subset so a column addition cannot
    make a perfectly good corpus unloadable. This is the first time that
    promise is exercised -- a pinned corpus in the field has no mana_cost."""
    import sqlite3

    path = tmp_path / sc.CORPUS_NAME
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE rules(rule_id, parent_id, text, examples);"
        "CREATE TABLE glossary(term, definition);"
        # no mana_cost column, exactly as every corpus built before it existed
        "CREATE TABLE cards(oracle_id, name, name_lower, type_line, oracle_text);"
        "CREATE TABLE rulings(oracle_id, published_at, source, comment);"
        "CREATE TABLE meta(key, value);"
        "CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id, text);"
        "CREATE VIRTUAL TABLE cards_fts USING fts5(name, oracle_id UNINDEXED);"
        "CREATE TABLE card_aliases(printed_lower, lang, oracle_id);")
    con.commit()
    con.close()
    sc._verify_is_corpus(path)      # must not raise
    assert "mana_cost" not in str(sc._REQUIRED_SCHEMA["cards"])
