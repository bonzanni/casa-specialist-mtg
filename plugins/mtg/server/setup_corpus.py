#!/usr/bin/env python3
"""Install a prebuilt corpus into the plugin's data directory.

Reached only through the `setup_corpus` and `update_corpus` MCP tools, which
import it lazily. Nothing on the query path imports it, so that path's "no
network, no writes" property stays a fact about what the process can reach
rather than a promise about how it is called.

The operator says where the corpus lives and, unless it is a GitHub release
asset whose published digest stands in, what it must hash to; this
repository names no location and ships no corpus.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

CORPUS_NAME = "corpus.sqlite"
SIDECAR_NAME = CORPUS_NAME + ".sha256"
# Which published release the installed corpus came from, when that is known.
# Missing means "unknown", which is what every corpus installed before this
# file existed reports.
RELEASE_NAME = "corpus.release.json"

BUNDLED_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA"

# Where the operator's answers arrive. The environment is the delivery path
# the plugin declares in .mcp.json; the same three are accepted as tool
# arguments so the tool works, and can be tested, outside a casa deployment.
URL_ENV = "CASA_PLUGIN_MTG_CORPUS_URL"
SHA256_ENV = "CASA_PLUGIN_MTG_CORPUS_SHA256"
TOKEN_ENV = "CASA_PLUGIN_MTG_CORPUS_TOKEN"

# An English-only corpus is ~46 MB and the Italian-alias build is roughly
# double; these leave room for both and still stop far short of filling the
# small disks these deployments run on.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 900.0
SOCKET_TIMEOUT_S = 30.0
MAX_REDIRECTS = 5
# A GitHub API answer (a release with its notes and asset list) is a few KB.
MAX_JSON_BYTES = 4 * 1024 * 1024
_CHUNK = 65536

_OCTET = "application/octet-stream"
_GITHUB_JSON = "application/vnd.github+json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_REDIRECT_CODES = frozenset((301, 302, 303, 307, 308))


class SetupError(Exception):
    """A refusal the operator can act on. Never leaves a corpus behind."""


def default_data_dir() -> Path:
    """The corpus directory, resolved at call time.

    The corpus must land in the writable plugin-data directory Claude Code
    sets, NOT in the installed plugin tree: under casa that tree is
    content-checksummed, and a 46 MB file appearing inside it makes the next
    registry reload treat the plugin as corrupt. A server started by hand,
    with no such directory, falls back to the plugin's own data/.

    The server resolves the same pair for reading and passes the result in,
    so in a real deployment there is one answer rather than two.
    """
    external = os.environ.get(PLUGIN_DATA_ENV, "").strip()
    return Path(external) if external else BUNDLED_DATA_DIR


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are resolved by hand below. urllib's own handler replays the
    headers it was given at whatever host the remote end names, and where the
    Authorization header goes is precisely the decision that must not be
    delegated to the other end of the connection."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _urlopen(request, timeout):
    """The single point where this module touches the network."""
    return _OPENER.open(request, timeout=timeout)


def _https_host(url: str) -> str:
    """Return the lowercased host, refusing anything but https.

    file:, http:, ftp: and the rest are rejected by allow-list rather than
    by naming the dangerous ones — the set of dangerous schemes is not
    enumerable, and a corpus arriving in the clear cannot be trusted even
    with a hash to check it against, because the hash arrives the same way.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise SetupError(
            f"corpus URL must use https, got {parts.scheme or 'no'} scheme")
    if not parts.hostname:
        raise SetupError("corpus URL has no host")
    return parts.hostname.lower()


def _build_request(url: str, token: str | None, origin_host: str,
                   accept: str = _OCTET):
    host = _https_host(url)
    # Accept matters, not just politeness: GitHub's authenticated release-asset
    # API URL is the stable token-authenticated route for a private release,
    # and without this header it answers with the asset's METADATA JSON. That
    # JSON downloads happily and then fails the sha256 check, which reads as a
    # corrupted asset rather than the wrong endpoint. The metadata is asked
    # for on purpose only by get_json, which passes the JSON media type.
    request = urllib.request.Request(url, headers={
        "User-Agent": "casa-mtg-setup",
        "Accept": accept,
    })
    # The token authenticates the operator to the host they named. A release
    # asset normally redirects to a separate storage host, so the hop itself
    # is expected — but the credential stops at the host change. Enforced
    # here, at the one place a header is attached, so the guarantee does not
    # rest on the redirect loop below staying correct.
    if token and host == origin_host:
        request.add_header("Authorization", f"Bearer {token}")
    return request


def _open_asset(url: str, token: str | None, deadline: float,
                accept: str = _OCTET):
    origin_host = _https_host(url)
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        # The wall clock covers the redirect chain too: a server that
        # bounces the request around cannot buy time the download itself
        # would not have been given.
        if time.monotonic() > deadline:
            raise SetupError("download timed out while following redirects")
        request = _build_request(current, token, origin_host, accept)
        try:
            return _urlopen(request, SOCKET_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            code = exc.code
            exc.close()
            if code not in _REDIRECT_CODES or not location:
                raise SetupError(f"download failed: HTTP {code}") from None
            # https-only applies to every hop, not just the first: a redirect
            # to http would hand the corpus and the pinned hash to the same
            # network position.
            current = urljoin(current, location)
            _https_host(current)
        except urllib.error.URLError as exc:
            raise SetupError(f"download failed: {exc.reason}") from None
    raise SetupError(f"download failed: more than {MAX_REDIRECTS} redirects")


def _download(url: str, token: str | None, dest: Path, *,
              max_bytes: int, timeout: float) -> tuple[str, int]:
    """Stream the asset to *dest*, returning (sha256 hex, byte count)."""
    deadline = time.monotonic() + timeout
    response = _open_asset(url, token, deadline)
    digest = hashlib.sha256()
    total = 0
    try:
        declared = response.headers.get("Content-Length") if response.headers else None
        if declared and declared.strip().isdigit() and int(declared) > max_bytes:
            raise SetupError(
                f"asset declares {int(declared)} bytes, over the "
                f"{max_bytes}-byte cap")
        with open(dest, "wb") as fh:
            while True:
                # Both limits are checked per chunk rather than after the
                # loop. A cap tested at the end has already let the disk
                # fill, and a stalled or endless stream never reaches the
                # end to be tested at all.
                if time.monotonic() > deadline:
                    raise SetupError(f"download exceeded {timeout:g}s")
                chunk = response.read(_CHUNK)
                # Check AFTER the read as well. A server that dribbles bytes
                # often enough to keep the socket timeout at bay can hold a
                # single blocking read open indefinitely, and an EOF arriving
                # past the deadline would otherwise be accepted as a complete,
                # successful download.
                if time.monotonic() > deadline:
                    raise SetupError(f"download exceeded {timeout:g}s")
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SetupError(
                        f"download exceeded the {max_bytes}-byte cap")
                digest.update(chunk)
                fh.write(chunk)
    finally:
        response.close()
    return digest.hexdigest(), total


def get_json(url: str, token: str | None, *,
              timeout: float = SOCKET_TIMEOUT_S * 2) -> dict:
    """GET a JSON object over the same transport the download uses: https on
    every hop, the token only on the host it was given for, a byte cap and a
    deadline."""
    deadline = time.monotonic() + timeout
    response = _open_asset(url, token, deadline, accept=_GITHUB_JSON)
    try:
        body = response.read(MAX_JSON_BYTES + 1)
    finally:
        response.close()
    if len(body) > MAX_JSON_BYTES:
        raise SetupError(f"{url} answered with more than {MAX_JSON_BYTES} bytes")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SetupError(f"{url} did not answer with JSON") from None
    if not isinstance(value, dict):
        raise SetupError(f"{url} did not answer with a JSON object")
    return value


# --- GitHub release assets ---------------------------------------------------
# A corpus published as a GitHub release asset carries two things a plain URL
# does not: a digest GitHub computed on upload, so the operator need not pin
# one by hand, and a place to look for the next release.

_GITHUB_API_HOST = "api.github.com"
_GITHUB_ASSET_PATH = re.compile(
    r"^/repos/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/releases/assets/([0-9]+)/?$")
_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$", re.IGNORECASE)
_DOWNLOAD_TAG_RE = re.compile(r"/releases/download/([^/]+)/[^/]+$")


def github_asset(url: str) -> tuple[str, str, int] | None:
    """(owner, repo, asset id) when *url* is a GitHub release-asset API URL
    (https://api.github.com/repos/<owner>/<repo>/releases/assets/<id>);
    None for anything else, which is a source pinned to one file."""
    parts = urlsplit(url)
    if (parts.scheme.lower() != "https"
            or (parts.hostname or "").lower() != _GITHUB_API_HOST
            or parts.query or parts.fragment):
        return None
    match = _GITHUB_ASSET_PATH.match(parts.path)
    if not match:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def source_of(owner: str, repo: str) -> str:
    """The canonical name of a release source, as the release record keeps
    it: GitHub owner and repository names are case-insensitive."""
    return f"{owner}/{repo}".lower()


def asset_api_url(owner: str, repo: str, asset_id: int) -> str:
    return (f"https://{_GITHUB_API_HOST}/repos/{owner}/{repo}"
            f"/releases/assets/{asset_id}")


def latest_release_url(owner: str, repo: str) -> str:
    return f"https://{_GITHUB_API_HOST}/repos/{owner}/{repo}/releases/latest"


def published_sha256(asset: dict) -> str | None:
    """The sha256 GitHub publishes for a release asset (`digest`), or None
    when the asset carries none."""
    match = _DIGEST_RE.match(str(asset.get("digest") or ""))
    return match.group(1).lower() if match else None


def asset_metadata(url: str, token: str | None) -> dict:
    """The release-asset JSON GitHub answers when the asset URL is asked for
    without the octet-stream Accept header."""
    return get_json(url, token)


def tag_of_asset(asset: dict) -> str | None:
    """The release tag, read off the asset's browser download URL
    (…/releases/download/<tag>/<name>); the asset JSON has no tag field."""
    match = _DOWNLOAD_TAG_RE.search(
        urlsplit(str(asset.get("browser_download_url") or "")).path)
    return match.group(1) if match else None


# --- the installed release record --------------------------------------------

_SOURCE_RE = re.compile(r"^[a-z0-9._-]+/[a-z0-9._-]+$")


def _valid_release(value) -> bool:
    """A record is trusted only whole: which repository (`source`), which
    asset (`asset_id`, and `asset_name` when known), and the archive's hash.
    Anything else describes no corpus anyone can compare against."""
    if not isinstance(value, dict):
        return False
    asset_id = value.get("asset_id")
    tag, name = value.get("tag"), value.get("asset_name")
    return (isinstance(value.get("source"), str)
            and bool(_SOURCE_RE.match(value["source"]))
            and isinstance(asset_id, int) and not isinstance(asset_id, bool)
            and isinstance(value.get("archive_sha256"), str)
            and bool(_SHA256_RE.match(value["archive_sha256"]))
            and (tag is None or (isinstance(tag, str) and bool(tag)))
            and (name is None or (isinstance(name, str) and bool(name))))


def read_release(data_dir: Path | str) -> dict | None:
    """The record of which release the installed corpus came from, or None
    when there is none or it is not a valid record: both mean "unknown"."""
    try:
        value = json.loads((Path(data_dir) / RELEASE_NAME).read_text("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return value if _valid_release(value) else None


def _write_release(data_dir: Path, record: dict) -> None:
    """Best effort, and only after the corpus is in place: a record that
    could not be written reads as "unknown" at the next check, which offers
    the update again, rather than turning an installed corpus into a failure
    report."""
    path = data_dir / RELEASE_NAME
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(record, sort_keys=True) + "\n", "utf-8")
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


def _safe_member_name(member: tarfile.TarInfo) -> str:
    """Vet a member and return the name to write it under.

    PurePosixPath drops the leading "./" that `tar czf x.tar.gz .` produces,
    so a well-formed archive built either way resolves to the same names.
    """
    name = member.name
    pure = PurePosixPath(name)
    if pure.is_absolute() or name.startswith("/") or "\\" in name or ":" in name:
        raise SetupError(f"archive member is not a plain relative path: {name!r}")
    if any(part == ".." for part in pure.parts):
        raise SetupError(f"archive member escapes the target directory: {name!r}")
    if member.issym() or member.islnk():
        # A link is a write to wherever it points, chosen by whoever built
        # the archive rather than by this code.
        raise SetupError(f"archive member is a link: {name!r}")
    if not member.isdir() and not member.isfile():
        raise SetupError(f"archive member is not a regular file: {name!r}")
    return pure.as_posix()


def _extract(archive: Path, dest_dir: Path, *, max_total: int) -> None:
    """Unpack the two expected files, refusing anything unsafe on the way.

    Members are read one at a time rather than through extractall, so no
    filter setting, tarfile version, or member flag decides what lands on
    disk — this does. Declared member sizes are what tarfile will read, so
    summing them bounds the expansion before a byte is written; the archive
    itself is already bounded by the download cap.
    """
    wanted = {CORPUS_NAME, SIDECAR_NAME}
    seen: set[str] = set()
    expanded = 0
    with tarfile.open(archive, mode="r:gz") as tar:
        for member in tar:
            name = _safe_member_name(member)
            if member.isdir():
                continue
            expanded += member.size
            if expanded > max_total:
                raise SetupError(
                    f"archive expands past the {max_total}-byte cap")
            if name not in wanted:
                continue
            source = tar.extractfile(member)
            if source is None:
                raise SetupError(f"archive member has no data: {name!r}")
            with open(dest_dir / name, "wb") as fh:
                shutil.copyfileobj(source, fh, _CHUNK)
            seen.add(name)
    missing = sorted(wanted - seen)
    if missing:
        raise SetupError("archive is missing " + ", ".join(missing))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_sidecar(corpus: Path, sidecar: Path) -> str:
    """Check the shipped sidecar against the shipped corpus.

    Every tool response quotes the sidecar as the corpus's provenance. If
    the two disagree, the server would cite a hash for bytes it is not
    reading, so the pair is rejected rather than half-installed.
    """
    try:
        text = sidecar.read_bytes().decode("utf-8").strip()
    except UnicodeDecodeError:
        raise SetupError(f"{SIDECAR_NAME} is not UTF-8 text") from None
    claimed = text.split()[0] if text else ""
    if not _SHA256_RE.match(claimed):
        raise SetupError(f"{SIDECAR_NAME} does not contain a sha256")
    actual = _file_sha256(corpus)
    if claimed.lower() != actual:
        raise SetupError(
            f"{SIDECAR_NAME} claims {claimed[:16]}… but {CORPUS_NAME} "
            f"hashes to {actual[:16]}…")
    _verify_is_corpus(corpus)
    return actual


# "SQLite format 3\0", split so this file does not trip the repository's own
# no-corpus scanner, which hunts for exactly that byte string.
_SQLITE_MAGIC = b"SQLite" + b" format 3\x00"

# The columns the query tools actually read. Deliberately a subset — enough to
# tell a corpus from an unrelated database, not so much that a future column
# addition upstream makes a perfectly good corpus unloadable.
_REQUIRED_SCHEMA = {
    "rules": ("rule_id", "parent_id", "text", "examples"),
    "glossary": ("term", "definition"),
    "cards": ("oracle_id", "name", "name_lower", "type_line", "oracle_text"),
    "rulings": ("oracle_id", "published_at", "source", "comment"),
    "meta": ("key", "value"),
    # As load-bearing as the base tables: without them search_rules returns a
    # truthful-looking "no rules match", and a fuzzy or Italian card lookup
    # raises at query time. A corpus missing them installs happily and then
    # lies, which is worse than refusing it here.
    "rules_fts": ("rule_id", "text"),
    "cards_fts": ("name", "oracle_id"),
    "card_aliases": ("printed_lower", "lang", "oracle_id"),
}

# These two must be real FTS tables. Ordinary tables of the same name and
# columns satisfy every column check and then make MATCH fail, so the search
# tool answers "no rules match" to questions the corpus can actually answer —
# a wrong answer delivered confidently, which is the failure this whole
# component exists to avoid.
_FTS_TABLES = ("rules_fts", "cards_fts")


def _verify_is_corpus(corpus: Path) -> None:
    """Confirm the installed file is actually an MTG corpus.

    Hashes only prove the bytes arrived intact, not that they are the right
    bytes. Without this, an operator who points at the wrong asset gets a
    cheerful "installed" and discovers the mistake later, one failed ruling at
    a time, with the setup step already reporting success.
    """
    with open(corpus, "rb") as fh:
        if fh.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
            raise SetupError(f"{CORPUS_NAME} is not a SQLite database")
    try:
        con = sqlite3.connect(f"file:{corpus}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SetupError(f"{CORPUS_NAME} is not readable: {exc}") from None
    try:
        try:
            schema = {name: (sql or "") for name, sql in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as exc:
            raise SetupError(f"{CORPUS_NAME} is not readable: {exc}") from None

        missing = set(_REQUIRED_SCHEMA) - set(schema)
        if missing:
            raise SetupError(
                f"{CORPUS_NAME} is a database but not an MTG corpus "
                f"(missing tables: {', '.join(sorted(missing))})")

        # Table NAMES alone are not enough: tables with the right names and
        # the wrong columns install cleanly and then fail on the first query,
        # with an error pointing nowhere near the real cause.
        for table, columns in _REQUIRED_SCHEMA.items():
            try:
                present = {row[1] for row in con.execute(
                    f"PRAGMA table_info({table})")}
            except sqlite3.Error as exc:
                raise SetupError(
                    f"{CORPUS_NAME} table {table!r} is unreadable: {exc}") from None
            absent = set(columns) - present
            if absent:
                raise SetupError(
                    f"{CORPUS_NAME} table {table!r} is missing "
                    f"{', '.join(sorted(absent))} — not an MTG corpus")

        # And the search tables must be real FTS tables. Ordinary tables of
        # the same name and columns pass every check above, then make MATCH
        # fail — so search answers "no rules match" to questions the corpus
        # can actually answer. A confident wrong answer is the exact failure
        # this component exists to prevent.
        for table in _FTS_TABLES:
            sql = schema.get(table, "")
            # Match the USING clause, not the whole statement: "FTS" also
            # appears in the table's own name, so a substring test accepted
            # `CREATE VIRTUAL TABLE rules_fts USING rtree(...)` — virtual,
            # wrongly shaped, and silently unsearchable.
            module = re.search(r"\bUSING\s+([A-Za-z0-9_]+)", sql, re.IGNORECASE)
            if not module or not module.group(1).lower().startswith("fts"):
                found = module.group(1) if module else "no USING clause"
                raise SetupError(
                    f"{CORPUS_NAME} table {table!r} is not an FTS table "
                    f"({found}) — searches would silently return nothing")
    finally:
        con.close()


def _kept(target: Path) -> str:
    # A corpus swapped out underneath a judge mid-ruling is worse than a
    # setup that declines: the citations it already gave stop being
    # reproducible from what is now on disk. Declining is still a success:
    # casa runs this tool again after every upgrade, and the corpus in place
    # is exactly what that run should leave alone.
    return f"kept the installed corpus at {target}; pass force to replace it"


def install_corpus(*, url: str, expected_sha256: str, token: str | None = None,
                   data_dir: Path | str | None = None, force: bool = False,
                   release: dict | None = None,
                   max_download_bytes: int = MAX_DOWNLOAD_BYTES,
                   max_extracted_bytes: int = MAX_EXTRACTED_BYTES,
                   timeout: float = DOWNLOAD_TIMEOUT_S) -> str:
    """Download, verify and install the archive at *url*.

    The one install path: `setup_corpus` and `update_corpus` both end here.
    Every check runs on a staged copy, and the corpus in use is replaced only
    once all of them have passed, so any failure leaves it untouched.
    *release* ({"source", "asset_id", "asset_name", "tag"}) is recorded
    beside the corpus after the swap; without it the previous record is
    dropped, since it would describe a corpus that is no longer there.
    """
    expected = str(expected_sha256).strip().lower()
    if not _SHA256_RE.match(expected):
        raise SetupError("corpus sha256 must be 64 hexadecimal characters")
    _https_host(url)  # refuse the scheme before anything is created on disk

    # Resolved here, not as a default argument, so the destination is read at
    # call time rather than frozen when this module was imported.
    data_dir = Path(default_data_dir() if data_dir is None else data_dir)
    target = data_dir / CORPUS_NAME
    if target.exists() and not force:
        return _kept(target)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Staged inside the destination directory so the final moves are renames
    # on one filesystem, and so a crash leaves a droppable directory rather
    # than a half-written corpus at the path the server reads.
    work = Path(tempfile.mkdtemp(prefix=".corpus-setup-", dir=data_dir))
    try:
        archive = work / "asset.tar.gz"
        actual, size = _download(url, token, archive,
                                 max_bytes=max_download_bytes, timeout=timeout)
        if actual != expected:
            raise SetupError(
                f"asset sha256 mismatch: expected {expected[:16]}…, "
                f"got {actual[:16]}…")
        staged = work / "unpacked"
        staged.mkdir()
        _extract(archive, staged, max_total=max_extracted_bytes)
        corpus_sha = _verify_sidecar(staged / CORPUS_NAME, staged / SIDECAR_NAME)
        # Corpus first, sidecar second. The reverse order would briefly
        # advertise provenance for bytes that are not there yet; this order
        # only degrades the reported hash to "unknown" until the second
        # rename lands, which the server already handles.
        # Remove any existing sidecar FIRST. The two renames are not atomic,
        # and an interruption between them leaves the new corpus beside the
        # old sidecar — stale provenance, which the server reports as fact.
        # Dropping it first makes the window report `unknown` instead, which
        # is what the server already handles and what this comment used to
        # claim without arranging for it. The release record goes the same
        # way, for the same reason, and is written last.
        for stale in (data_dir / RELEASE_NAME, data_dir / SIDECAR_NAME):
            if stale.exists():
                stale.unlink()
        os.replace(staged / CORPUS_NAME, target)
        os.replace(staged / SIDECAR_NAME, data_dir / SIDECAR_NAME)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if release is not None:
        _write_release(data_dir, {
            "source": release.get("source"),
            "asset_id": release.get("asset_id"),
            "asset_name": release.get("asset_name"),
            "tag": release.get("tag"),
            "archive_sha256": actual,
            "installed_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
        })
    return (f"corpus installed at {target}: {size} bytes downloaded, "
            f"{CORPUS_NAME} sha256 {corpus_sha[:16]}…")


_TRUE = frozenset(("1", "true", "yes", "on"))


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _input(args: dict, key: str, env_name: str) -> str:
    """An explicit argument wins; otherwise the declared environment."""
    value = args.get(key)
    if value is None:
        value = os.environ.get(env_name, "")
    return str(value).strip()


def run(args: dict | None = None, data_dir: Path | str | None = None) -> str:
    """Entry point for the `setup_corpus` tool.

    Argument-free when the environment carries the operator's answers, which
    is how casa runs it; the same three can be passed explicitly so the tool
    is usable, and testable, without a casa deployment behind it.

    The sha256 is optional for a GitHub release asset: the digest GitHub
    publishes for it is used instead. A hand-set sha256 always wins.
    """
    args = args if isinstance(args, dict) else {}
    url = _input(args, "url", URL_ENV)
    expected = _input(args, "sha256", SHA256_ENV)
    token = _input(args, "token", TOKEN_ENV) or None
    force = as_bool(args.get("force", False))
    if not url:
        raise SetupError(
            f"corpus location is not configured: no url. Set {URL_ENV} "
            f"(and {SHA256_ENV}, unless the URL is a GitHub release asset), "
            "or pass them as arguments.")

    data_dir = Path(default_data_dir() if data_dir is None else data_dir)
    if (data_dir / CORPUS_NAME).exists() and not force:
        return _kept(data_dir / CORPUS_NAME)  # before any network call

    release = None
    asset = github_asset(url)
    if asset is not None:
        release = {"source": source_of(asset[0], asset[1]),
                   "asset_id": asset[2], "asset_name": None, "tag": None}
        if not expected:
            metadata = asset_metadata(url, token)
            expected = published_sha256(metadata) or ""
            if not expected:
                raise SetupError(
                    "the release publishes no sha256 digest for this asset; "
                    f"set {SHA256_ENV} to the archive's sha256")
            release["tag"] = tag_of_asset(metadata)
            name = metadata.get("name")
            release["asset_name"] = name if isinstance(name, str) and name else None
    elif not expected:
        raise SetupError(
            f"corpus sha256 is not configured: set {SHA256_ENV}, or pass it "
            "as an argument. Only a GitHub release asset URL can do without "
            "one, because GitHub publishes its digest.")
    return install_corpus(url=url, expected_sha256=expected, token=token,
                          data_dir=data_dir, force=force, release=release)
