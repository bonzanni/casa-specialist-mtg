# casa-specialist-mtg

A Magic: The Gathering rules judge that answers from an offline corpus instead of from a model's memory, and refuses to state a ruling it cannot cite.

The repository holds two things. **`plugins/mtg/` is a standalone Claude Code plugin** — five read-only MCP tools over a local SQLite corpus of the Comprehensive Rules, Oracle card text, and rulings, plus a skill that makes the model actually use them. Running it needs no casa and no account — only a corpus, which you build (see below). The rest of the repository (`role/`, `persona/`, `manifest.json`) packages that plugin as a specialist component for [casa](https://github.com/bonzanni/ha-casa-app), a household assistant that runs as a Home Assistant app; if you are not running casa, ignore those directories.

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
| `setup_corpus` | Post-install only: fetch and verify a corpus from a location you configured |

Every response ends in a `corpus_info` line naming the CR effective date, the Scryfall snapshot date, and the corpus hash, so a ruling can be traced to the exact data that produced it.

A sixth tool, `setup_corpus`, installs a corpus after the plugin is installed; it is described under [Casa integration](#casa-integration) and does nothing until it is told where the corpus comes from.

The server (`plugins/mtg/server/mtg_server.py`) is stdlib-only, and the query path is read-only by construction: SQLite is opened `mode=ro`, output is bounded, and FTS terms are quoted so a card name can never inject query syntax. Answering a question reaches no shell, no socket and no write — the code that downloads and installs a corpus lives in a separate module (`setup_corpus.py`) that the server imports only when `setup_corpus` is called, so that is a property of what the query path can reach rather than a promise about how it is used.

The corpus is read from `$CLAUDE_PLUGIN_DATA/corpus.sqlite`, in the writable directory Claude Code gives each plugin — under casa as well as standalone — and from the plugin's own `data/corpus.sqlite` only for a server started by hand, where nothing sets that variable.

## Install

There are two routes: as a specialist on casa, or as a plugin in Claude Code. Both need a corpus you build yourself (see [Build the corpus](#build-the-corpus)).

### On casa

This needs casa 0.372.0 or later. Have a corpus reachable first: pack your build into a `.tar.gz`, put it at an `https://` URL you control, and note the archive's sha256.

```bash
tar -C plugins/mtg/data -czf corpus.tar.gz corpus.sqlite corpus.sqlite.sha256
sha256sum corpus.tar.gz
```

Then ask casa's configurator in chat:

> Install the MTG specialist from bonzanni/casa-specialist-mtg, the latest version. The corpus is at `<URL>` and its sha256 is `<SHA256>`.

Approve the consent message casa sends you. The configurator installs the specialist with its plugin and persona, adds it to the assistant's helpers, and sets the two corpus values; casa then runs `setup_corpus` by itself, and the assistant tells you in one line how that went. See [Casa integration](#casa-integration) for the details, including a URL that needs a token.

The judge has a Telegram chat of his own; when the install is done, the assistant offers to set up his bot. Ask him rules questions there: he answers with a short ruling and why, and goes into the detailed rules, card text and rulings when you ask. A rules question asked of the assistant goes to him too, and his answer appears in his chat.

To ask by voice, the judge must also be one of the voice concierge's helpers. The role declares `voice_handoff: true`, so on Casa 0.367.6 or later the install ends by asking whether Gary should be able to ask the judge too; you can also ask for it later ("let Gary ask the judge rules questions"). The concierge passes your question on and tells you the answer; a slow one comes back when the judge is done, on the device you asked.

### As a Claude Code plugin

```
/plugin marketplace add bonzanni/casa-specialist-mtg
/plugin install mtg@casa-specialist-mtg
```

Then build the corpus — the plugin ships without one, and **the installed
plugin does not include the builder**: `/plugin install` copies only
`plugins/mtg/`, while `scripts/build_corpus.py` lives at the repository root.
Clone the repository to build, then point `--out` at the directory Claude Code
gives the plugin for its data:

```bash
git clone https://github.com/bonzanni/casa-specialist-mtg
cd casa-specialist-mtg
python3 scripts/build_corpus.py --cr-url <URL> --out ~/.claude/plugins/data/mtg-casa-specialist-mtg/corpus.sqlite
```

(`$CLAUDE_CONFIG_DIR` replaces `~/.claude` if you set it.) Until a corpus exists
where the server reads it, every tool reports an error rather than returning a
wrong answer. Do not put it in the installed plugin's own `data/`: Claude Code
sets the data directory above, so the server never looks there.

### Build the corpus

The corpus is **not** in this repository and is not distributed with the plugin. Build it yourself:

```bash
python3 scripts/build_corpus.py --cr-url <MagicCompRules .txt URL>
```

There is no stable URL for the rules file — the filename carries the effective date and changes with every release — so `scripts/resolve_cr_url.py` reads the current one off the [Wizards rules page](https://magic.wizards.com/en/rules):

```bash
python3 scripts/build_corpus.py --cr-url "$(python3 scripts/resolve_cr_url.py)"
```

It refuses rather than guesses: no link, several links, or an unreachable page all exit non-zero instead of building a corpus from an invented URL. Scryfall's bulk data is discovered through their API. `scripts/check_corpus_plausible.py` will tell you whether the result actually parsed, which matters most when nobody is watching the build.

By default this writes `plugins/mtg/data/corpus.sqlite` (~46 MB) and a `.sha256` sidecar, which is the build to pack for casa; `--out` writes them elsewhere. Add `--with-it-aliases` to also index Italian printed names, which downloads Scryfall's ~2 GB all-cards bulk file and streams it — worth it only if you ask questions in Italian.

Nothing is published from this repository — no corpus in the tree, no corpus as a release asset. Redistributing the rules and card text is the thing the Fan Content Policy prohibits, and hosting the same bytes as a release asset would be that same act with a different URL. This project therefore points at no corpus and names no location; building one is the path it supports. The setup tool described under [Casa integration](#casa-integration) fetches from wherever an operator hosts their own build, which is their arrangement to make and to keep private.

Note that **the build is not byte-reproducible**: `meta.built_at` records the build time, and SQLite page layout and FTS index ordering are not guaranteed stable between runs anyway. Two builds from identical upstream data will not produce identical files, so hashes are not comparable across builds. What you can verify is the *content* — compare row counts and the `cr_effective_date` / `scryfall_updated_at` values that every tool response reports in its `corpus_info` line.

## Rules data and attribution

This repository contains no Magic: The Gathering rules text, card text, or rulings. The build script fetches the Comprehensive Rules from Wizards of the Coast and card data from [Scryfall](https://scryfall.com/docs/api), and the corpus it produces is a local derivative of those sources, subject to their terms rather than to this repository's MIT license.

Two separate sets of terms apply, and they come from different parties. **Wizards of the Coast** owns the rules and card text; the Fan Content Policy below governs its use. **Scryfall** delivers the card data but does not license the underlying IP — its [API documentation](https://scryfall.com/docs/api) sets its own additional conditions, which as of this writing prohibit paywalling the data, implying Scryfall endorsement, and using the data for games other than Magic: The Gathering. Card data is fetched from Scryfall; Scryfall does not endorse this project. Check both sources yourself rather than relying on this summary.

> casa-specialist-mtg is unofficial Fan Content permitted under the [Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy). Not approved/endorsed by Wizards. Portions of the materials used are property of Wizards of the Coast. ©Wizards of the Coast LLC.

## Scope

Casual-game rules and current Oracle text. Tournament policy, format legality, and banlists are deliberately out of scope — they change on a different cadence than the CR and are not in the corpus, so the judge declines rather than guesses.

## Development

```bash
pip install -r tests/requirements.txt
python3 -m pytest tests/ -q
```

311 tests, no network required — they build fixture databases in `tmp_path`, including genuine FTS5 indexes, so the corpus-integrity checks are exercised without a corpus. One test additionally verifies a real built corpus if one is present, and skips otherwise. `tests/test_mtg_server.py` covers the JSON-RPC framing and adversarial-input contract (malformed requests, notifications, non-standard JSON constants, FTS injection, corrupt hash sidecars); `tests/test_build_corpus.py` covers CR parsing and the card-data transforms against offline fixtures; `tests/test_setup_corpus.py` covers the setup tool's transport and archive handling, with the one function that opens a socket replaced by a fake that serves bytes from memory.

Test fixtures are invented rules and invented cards in the real formats, never excerpts — the parsers care about shape, and shape is reproducible without copying anything.

## Casa integration

For a casa deployment, this repository is a `casa.specialist-component/v1` component: `role/role.yaml` defines the specialist's tool grants and response shape, `role/doctrine.md` its operating procedure, `persona/` the default "Judge" persona pack, and `manifest.json` pins the digest of every dependency. Casa installs the whole closure under one consent.

The corpus is deliberately **not** one of those dependencies. Casa's install path verifies every dependency against a digest published by the component author, and a corpus cannot be published — so instead of weakening that check, the corpus is supplied after install, from a location you choose. That keeps the public repository free of rules text while letting anyone who already has a corpus install in one step.

### The setup tool

The plugin declares `casa.setupTool: setup_corpus`, a tool that downloads, verifies and installs a corpus from wherever you point it. It reads three variables:

| Variable | Purpose |
| --- | --- |
| `CASA_PLUGIN_MTG_CORPUS_URL` | `https://` URL of a `.tar.gz` holding `corpus.sqlite` and `corpus.sqlite.sha256` |
| `CASA_PLUGIN_MTG_CORPUS_SHA256` | sha256 the downloaded archive must have |
| `CASA_PLUGIN_MTG_CORPUS_TOKEN` | Bearer token, if the URL needs one (optional) |

The URL and the sha256 are required: `plugin-env.conf` holds them, and the configurator sets them during the install, either from what you tell it or from a matching 1Password item. Until both resolve, casa withholds the plugin and a question to the judge comes back saying which variables are missing, rather than failing quietly. Casa holds the setup until then too, and runs it by itself as soon as they resolve. The specialist's own role does not need to grant the tool: casa's server-level grant covers every tool on the plugin's MCP server.

Casa never asks for the token, because it is optional. If your URL needs one, tell the configurator to set `CASA_PLUGIN_MTG_CORPUS_TOKEN` as a 1Password reference (`op://…`). If the automatic setup already failed for lack of it, ask the assistant to have the judge run its corpus setup again once the token is set.

Upgrading to 0.10.0 needs casa 0.372.0 first: the five lookup tools are opened to guests, a contract member an older casa refuses. Upgrading to 0.9.2 likewise needed casa 0.367.6: the role declares `voice_handoff`, which an older casa refuses. Upgrading from 0.8.0 likewise needed casa 0.360.0: 0.9.0 declares its own chat and ships an avatar, which an older casa refuses. An upgrade to a new version runs the setup again; with a corpus already in place it says so and keeps that corpus. Upgrading from 0.7.0, ask the configurator to set the two values as well, even with a corpus in place: 0.7.0 kept them somewhere the plugin never read, and until they are set casa withholds the plugin. To replace a corpus, change the two values and ask the assistant to have the judge run its corpus setup with `force`.

The five lookup tools are declared `{"result": "safe", "guest": true}` under `casa.resultContract`: they return rules text, card text and rulings, never a live link or credential, so nothing is escrowed, and a guest may call them (next paragraph). Casa refuses any non-setup tool missing from that declaration before it runs, so a new tool must be added there too; `tests/test_result_contract.py` checks the declaration against the server's tool table.

### Guests

Casa 0.372.0 lets other people ask a specialist in a Telegram group you allowed: they mention the judge's bot or reply to one of his answers, and he answers there in one short message. Such a run reaches only the tools a plugin opens to guests with `"guest": true` — for the judge, the five lookups and nothing else: no memory of you, no earlier conversation, no Casa tools, and nothing anyone says there is remembered. The doctrine's `## Guest projection` tells him how to answer strangers: rulings only, grounded in the corpus; deck advice and tournament policy are declined. To enable it, switch Guest Mode on for the judge's bot in BotFather's Mini App (it is off by default), then summon him in the group yourself and say he can answer everyone there; ask the assistant "who may ask my specialists?" to see the list, or tell her to stop one.

Outside casa, pass the same three as tool arguments (`url`, `sha256`, `token`) — but a token passed that way is in the transcript, so prefer the environment.

What the tool will not do:

- speak anything but `https`, on the first request and on every redirect;
- carry the token across a host change — a release asset usually redirects to a separate storage host, so the hop is followed and the credential is not;
- keep reading past a byte cap or a wall-clock deadline, both checked while the body is arriving rather than after it has landed;
- unpack anything before the archive's sha256 matches what you pinned;
- extract an absolute path, a `..` component, a symlink, a hardlink, a device node, or more decompressed bytes than the cap allows;
- install a corpus whose bundled `corpus.sqlite.sha256` disagrees with the `corpus.sqlite` beside it, since that sidecar is the provenance every response quotes;
- replace an existing corpus unless asked to with `force`, because swapping one out under a judge mid-ruling makes the citations it already gave irreproducible.

The corpus lands in `$CLAUDE_PLUGIN_DATA`, the plugin's writable directory, and never inside the installed plugin tree — that tree is verified by content, and writing into it would make casa read the plugin as corrupt on the next reload.
