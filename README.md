# casa-mtg-judge

A Magic: The Gathering rules judge that answers from an offline corpus instead of from a model's memory, and refuses to state a ruling it cannot cite.

The repository holds two things. **`plugins/mtg/` is a standalone Claude Code plugin** — five read-only MCP tools over a local SQLite corpus of the Comprehensive Rules, Oracle card text, and rulings, plus a skill that makes the model actually use them. Running it needs no casa and no account — only a corpus, which you build (see below). The rest of the repository (`role/`, `persona/`, `manifest.json`) packages that plugin as a specialist component for [casa](https://github.com/bonzanni), a household assistant deployment; if you are not running casa, ignore those directories.

## Why an offline corpus

Trained MTG knowledge is confidently wrong at exactly the wrong moments: subrule letters shift between Comprehensive Rules releases, Oracle text is errata'd away from what is printed on the card, and "lethal damage" means something specific that paraphrase destroys. The skill (`plugins/mtg/skills/mtg-judge/SKILL.md`) treats the model's recall as intuition only — every load-bearing claim must come back from a tool call, and an answer with no citation is emitted as `tentative`, never as settled.

## Tools

| Tool | Purpose |
| --- | --- |
| `lookup_rule` | A CR rule by number, with its subrules and one hop of cross-references |
| `search_rules` | Full-text search across rule text and examples; a bare rule number short-circuits to an exact lookup |
| `lookup_term` | A glossary definition, to bridge table language to rule numbers |
| `lookup_card` | Oracle text by card name, English or Italian printed name, with fuzzy candidates on a near-miss |
| `get_rulings` | Rulings for a card, each labelled `[wotc]` (official) or `[scryfall_note]` (unofficial commentary) |
| `setup_corpus` | Post-install only: fetch and verify a corpus from a location the operator configured |

Every response ends in a `corpus_info` line naming the CR effective date, the Scryfall snapshot date, and the corpus hash, so a ruling can be traced to the exact data that produced it.

A sixth tool, `setup_corpus`, installs a corpus after the plugin is installed; it is described under [Casa integration](#casa-integration) and does nothing unless an operator has configured where the corpus comes from.

The server (`plugins/mtg/server/mtg_server.py`) is stdlib-only, and the query path is read-only by construction: SQLite is opened `mode=ro`, output is bounded, and FTS terms are quoted so a card name can never inject query syntax. Answering a question reaches no shell, no socket and no write — the code that downloads and installs a corpus lives in a separate module (`setup_corpus.py`) that the server imports only when `setup_corpus` is called, so that is a property of what the query path can reach rather than a promise about how it is used.

The corpus is read from `$CLAUDE_PLUGIN_DATA/corpus.sqlite` when that variable is set — casa sets it to a writable directory belonging to the plugin — and from the plugin's own `data/corpus.sqlite` otherwise.

## Install

### As a Claude Code plugin

```
/plugin marketplace add bonzanni/casa-mtg-judge
/plugin install mtg@casa-mtg-judge
```

Then build the corpus — the plugin ships without one, and **the installed
plugin does not include the builder**: `/plugin install` copies only
`plugins/mtg/`, while `scripts/build_corpus.py` lives at the repository root.
Clone the repository to build, then point `--out` at the installed plugin's
`data/` directory:

```bash
git clone https://github.com/bonzanni/casa-mtg-judge
cd casa-mtg-judge
pip install -r scripts/requirements.txt
python3 scripts/build_corpus.py --cr-url <URL> --out <installed-plugin>/data/corpus.sqlite
```

Until a corpus exists where the server reads it, every tool reports an error
rather than returning a wrong answer.

### Build the corpus

The corpus is **not** in this repository and is not distributed with the plugin. Build it yourself:

```bash
pip install -r scripts/requirements.txt
python3 scripts/build_corpus.py --cr-url <MagicCompRules .txt URL>
```

There is no stable URL for the rules file — the filename carries the effective date and changes with every release — so `scripts/resolve_cr_url.py` reads the current one off the [Wizards rules page](https://magic.wizards.com/en/rules):

```bash
python3 scripts/build_corpus.py --cr-url "$(python3 scripts/resolve_cr_url.py)"
```

It refuses rather than guesses: no link, several links, or an unreachable page all exit non-zero instead of building a corpus from an invented URL. Scryfall's bulk data is discovered through their API. `scripts/check_corpus_plausible.py` will tell you whether the result actually parsed, which matters most when nobody is watching the build.

This writes `plugins/mtg/data/corpus.sqlite` (~46 MB) and a `.sha256` sidecar. Add `--with-it-aliases` to also index Italian printed names, which downloads Scryfall's ~2 GB all-cards bulk file and streams it — worth it only if you ask questions in Italian.

Nothing is published from this repository — no corpus in the tree, no corpus as a release asset. Redistributing the rules and card text is the thing the Fan Content Policy prohibits, and hosting the same bytes as a release asset would be that same act with a different URL. This project therefore points at no corpus and names no location; building one is the path it supports. The setup tool described under [Casa integration](#casa-integration) fetches from wherever an operator hosts their own build, which is their arrangement to make and to keep private.

Note that **the build is not byte-reproducible**: `meta.built_at` records the build time, and SQLite page layout and FTS index ordering are not guaranteed stable between runs anyway. Two builds from identical upstream data will not produce identical files, so hashes are not comparable across builds. What you can verify is the *content* — compare row counts and the `cr_effective_date` / `scryfall_updated_at` values that every tool response reports in its `corpus_info` line.

## Rules data and attribution

This repository contains no Magic: The Gathering rules text, card text, or rulings. The build script fetches the Comprehensive Rules from Wizards of the Coast and card data from [Scryfall](https://scryfall.com/docs/api), and the corpus it produces is a local derivative of those sources, subject to their terms rather than to this repository's MIT license.

Two separate sets of terms apply, and they come from different parties. **Wizards of the Coast** owns the rules and card text; the Fan Content Policy below governs its use. **Scryfall** delivers the card data but does not license the underlying IP — its [API documentation](https://scryfall.com/docs/api) sets its own additional conditions, which as of this writing prohibit paywalling the data, implying Scryfall endorsement, and using the data for games other than Magic: The Gathering. Card data is fetched from Scryfall; Scryfall does not endorse this project. Check both sources yourself rather than relying on this summary.

> casa-mtg-judge is unofficial Fan Content permitted under the [Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy). Not approved/endorsed by Wizards. Portions of the materials used are property of Wizards of the Coast. ©Wizards of the Coast LLC.

## Scope

Casual-game rules and current Oracle text. Tournament policy, format legality, and banlists are deliberately out of scope — they change on a different cadence than the CR and are not in the corpus, so the judge declines rather than guesses.

## Development

```bash
pip install pytest ijson
python3 -m pytest tests/ -q
```

170 tests, no network and no corpus required — they build fixture databases in `tmp_path`. `tests/test_mtg_server.py` covers the JSON-RPC framing and adversarial-input contract (malformed requests, notifications, non-standard JSON constants, FTS injection, corrupt hash sidecars); `tests/test_build_corpus.py` covers CR parsing and the card-data transforms against offline fixtures; `tests/test_setup_corpus.py` covers the setup tool's transport and archive handling, with the one function that opens a socket replaced by a fake that serves bytes from memory.

Test fixtures are invented rules and invented cards in the real formats, never excerpts — the parsers care about shape, and shape is reproducible without copying anything.

## Casa integration

For a casa deployment, this repository is a `casa.specialist-component/v1` component: `role/role.yaml` defines the specialist's tool grants and response shape, `role/doctrine.md` its operating procedure, `persona/` the default "Judge" persona pack, and `manifest.json` pins the digest of every dependency. Casa installs the whole closure under one consent.

The corpus is deliberately **not** one of those dependencies. Casa's install path verifies every dependency against a digest published by the component author, and a corpus cannot be published — so instead of weakening that check, the corpus is supplied after install, from a location the operator chooses. That keeps the public repository free of rules text while letting an operator who already has a corpus install in one step.

### The setup tool

The plugin declares `casa.setupTool: setup_corpus`, a tool that downloads, verifies and installs a corpus from wherever the operator points it. It reads three variables:

| Variable | Purpose |
| --- | --- |
| `CASA_PLUGIN_MTG_CORPUS_URL` | `https://` URL of a `.tar.gz` holding `corpus.sqlite` and `corpus.sqlite.sha256` |
| `CASA_PLUGIN_MTG_CORPUS_SHA256` | sha256 the downloaded archive must have |
| `CASA_PLUGIN_MTG_CORPUS_TOKEN` | Bearer token, if the URL needs one |

**Two things about this are easy to get wrong, so they are spelled out.**

*The values do not come from `config-schema.json`.* That file declares the keys — the two non-secret values are prompted for and recorded in the install snapshot, and the token is only ever recorded as having been supplied, never as a value — but a specialist's configuration is persisted in its install snapshot and is never exported into the MCP server's process environment. The variables reach the server from `plugin-env.conf`, which casa sources at boot and which resolves `op://` references, so the token belongs there as a reference rather than a literal. Declaring the keys and not populating that file leaves all three empty and the setup tool unable to run.

*Casa does not invoke the setup tool on its own here.* Setup episodes open from a trigger-consent round, and a bundled plugin dependency may not declare triggers — so nothing dispatches `setup_corpus` automatically. Invoke it once, explicitly, after configuring the variables. The specialist's own role does not need to grant it: casa's server-level grant covers every tool on the plugin's MCP server.

All three can also be passed as tool arguments, which is how the tool is used outside casa — but a token passed that way is in the transcript, so prefer the environment.

What the tool will not do:

- speak anything but `https`, on the first request and on every redirect;
- carry the token across a host change — a release asset usually redirects to a separate storage host, so the hop is followed and the credential is not;
- keep reading past a byte cap or a wall-clock deadline, both checked while the body is arriving rather than after it has landed;
- unpack anything before the archive's sha256 matches what the operator pinned;
- extract an absolute path, a `..` component, a symlink, a hardlink, a device node, or more decompressed bytes than the cap allows;
- install a corpus whose bundled `corpus.sqlite.sha256` disagrees with the `corpus.sqlite` beside it, since that sidecar is the provenance every response quotes;
- replace an existing corpus unless asked to with `force`, because swapping one out under a judge mid-ruling makes the citations it already gave irreproducible.

The corpus lands in `$CLAUDE_PLUGIN_DATA`, the writable directory casa gives the plugin, and never inside the installed plugin tree — that tree is verified by content, and writing into it would make casa read the plugin as corrupt on the next reload.

The alternative remains available: build a corpus with `scripts/build_corpus.py` and place it in that directory by hand.
