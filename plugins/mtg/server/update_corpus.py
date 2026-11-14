#!/usr/bin/env python3
"""Check for, and on request install, a newer corpus release.

Reached only through the `update_corpus` MCP tool, which imports this module
lazily, like `setup_corpus.py`; the query path imports neither.

It works only for a corpus published as a GitHub release asset: the configured
URL names the repository, `/releases/latest` names the newest release, and
GitHub publishes each asset's sha256. Any other URL is a source pinned to one
file, and there is nothing newer to look for. Installing goes through
`setup_corpus.install_corpus`, the same verified path the first install took,
with the release's published digest as the pinned hash; any failure there
leaves the corpus in use untouched.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

try:
    from . import setup_corpus as sc
except ImportError:  # launched as a script: the server put this dir on sys.path
    import setup_corpus as sc

def _asset_name(url: str, token: str | None, record: dict | None) -> str:
    """The configured asset's file name: the newest release is searched for
    an asset of the same name, since one release can carry several corpora
    (an English one and one with Italian names, say). If the configured asset
    can no longer be asked (an old release pruned), the name recorded at the
    last install stands in, and only a record of this same source qualifies;
    a guess could swap one corpus for a different one and call it an
    update."""
    try:
        name = sc.asset_metadata(url, token).get("name")
        reason = "it has no name"
    except sc.SetupError as exc:
        name, reason = None, str(exc)
    if isinstance(name, str) and name:
        return name
    if record and record.get("asset_name"):
        return record["asset_name"]
    raise sc.SetupError(
        f"cannot tell which asset to look for: the configured asset could not "
        f"be read ({reason}) and no install from this source recorded its "
        "name; nothing changed")


def _latest(owner: str, repo: str, name: str, token: str | None) -> dict:
    release = sc.get_json(sc.latest_release_url(owner, repo), token)
    tag = release.get("tag_name")
    tag = tag if isinstance(tag, str) and tag else "(untagged)"
    assets = release.get("assets")
    found = [a for a in (assets if isinstance(assets, list) else [])
             if isinstance(a, dict) and a.get("name") == name]
    if len(found) != 1:
        raise sc.SetupError(
            f"the latest release, {tag}, has no single asset named {name}")
    asset = found[0]
    asset_id = asset.get("id")
    if not isinstance(asset_id, int) or isinstance(asset_id, bool):
        raise sc.SetupError(f"release {tag}: asset {name} has no id")
    sha256 = sc.published_sha256(asset)
    if sha256 is None:
        raise sc.SetupError(
            f"release {tag} publishes no sha256 digest for {name}, so it "
            "cannot be verified")
    return {"tag": tag, "asset_id": asset_id, "sha256": sha256,
            "url": sc.asset_api_url(owner, repo, asset_id)}


def _is_installed(record: dict | None, latest: dict) -> bool:
    """*record* is already narrowed to this source (see run)."""
    if not record:
        return False
    return (record["asset_id"] == latest["asset_id"]
            or record["archive_sha256"] == latest["sha256"])


def _now_on(corpus: Path, tag: str) -> str:
    """One line on what the new corpus covers, from its own meta rows."""
    meta: dict = {}
    try:
        con = sqlite3.connect(f"file:{corpus}?mode=ro", uri=True)
        try:
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        finally:
            con.close()
    except sqlite3.Error:
        pass
    rules = str(meta.get("cr_effective_date") or "unknown")
    cards = str(meta.get("scryfall_updated_at") or "unknown")[:10]
    return f"Now on the {rules} rules and the {cards} cards (release {tag})."


def run(args: dict | None = None, data_dir: Path | str | None = None) -> str:
    """Entry point for the `update_corpus` tool. Reports by default; installs
    only with `install` true."""
    args = args if isinstance(args, dict) else {}
    install = sc.as_bool(args.get("install", False))
    url = os.environ.get(sc.URL_ENV, "").strip()
    token = os.environ.get(sc.TOKEN_ENV, "").strip() or None
    if not url:
        raise sc.SetupError(
            f"corpus location is not configured: set {sc.URL_ENV}")
    asset = sc.github_asset(url)
    if asset is None:
        return ("The corpus source is pinned to one file rather than a GitHub "
                "release, so there is no newer release to look for; nothing "
                "changed.")
    owner, repo, _ = asset
    source = sc.source_of(owner, repo)

    data_dir = Path(sc.default_data_dir() if data_dir is None else data_dir)
    corpus = data_dir / sc.CORPUS_NAME
    record = sc.read_release(data_dir) if corpus.exists() else None
    if record is not None and record["source"] != source:
        # Asset ids and digests mean nothing across repositories: a record
        # from another source says nothing about this one.
        record = None
    name = _asset_name(url, token, record)
    latest = _latest(owner, repo, name, token)
    tag = latest["tag"]

    if corpus.exists() and _is_installed(record, latest):
        return f"Up to date: the installed corpus is release {tag}."
    if not install:
        if not corpus.exists():
            return f"No corpus is installed; release {tag} is available."
        if record is None:
            return (f"The installed release is unknown; the latest is {tag}. "
                    "Updating installs it.")
        installed = record.get("tag") or "an earlier release"
        return f"Release {tag} is available (installed: {installed})."

    sc.install_corpus(url=latest["url"], expected_sha256=latest["sha256"],
                      token=token, data_dir=data_dir, force=True,
                      release={"source": source,
                               "asset_id": latest["asset_id"],
                               "asset_name": name, "tag": tag})
    return _now_on(corpus, tag)
