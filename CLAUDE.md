# Bitcoin PSBT Builder

Single-page web app for building, signing, combining, and broadcasting multi-wallet Bitcoin PSBTs (BIP 174).

## Architecture

- **`index.html`** — Entire frontend in one file. Uses bitcoinjs-lib v7.0.0-rc.0 (ESM via esm.sh), PaperCSS for styling, SortableJS for drag-and-drop reordering.
- **`server/server.py`** — Local development server for regtest. Manages an isolated bitcoind instance (RegtestNode class) and exposes mempool.space-compatible API endpoints so the frontend code needs minimal branching.
- **`tests/test_psbt_builder.py`** — 88 unit tests using Playwright (Python sync API). Tests core functions, DOM interactions, and PSBT creation.
- **`tests/test_regtest_e2e.py`** — 99 E2E tests covering P2WPKH and P2TR (Taproot), both parallel and serial signing. Requires bitcoind/bitcoin-cli.
- **`tests/test_testnet4_e2e.py`** — 27 E2E tests on real testnet4. Parallel + serial signing with browser-based ECPair signing, funds return to main wallet. Requires a pre-funded testnet4 wallet (credentials via env vars, CLI args, or settings.json).

## Key Patterns

### Server Mode Detection
Frontend checks `/api/health` with 2s timeout on load. If a local server responds, `serverMode=true` and regtest routes through `/api`. On GitHub Pages (no server), regtest option is hidden from the network dropdown.

### API Routing
`getMempoolBaseUrl()` returns `/api` for regtest+serverMode, mempool.space URLs otherwise. The server mirrors mempool.space paths (`/api/address/<addr>/utxo`, `/api/tx/<txid>/hex`, `/api/tx` POST, `/api/v1/fees/recommended`) so frontend fetch calls work identically for all networks.

### PSBT Signing Flows
Two approaches both work through the UI:
- **Parallel**: Each party independently signs a copy of the unsigned PSBT → upload both → Combine & Finalize merges signatures
- **Serial**: Party A signs → passes to Party B → B signs → upload single fully-signed file → Finalize (combine is no-op)

### Important: `walletprocesspsbt` requires `finalize=false`
Bitcoin Core's `walletprocesspsbt` defaults to `finalize=true`, which sets `final_scriptwitness` instead of `partial_signatures`. bitcoinjs-lib v7 can't re-finalize already-finalized inputs. Always pass `finalize=false` when signing for later UI combination:
```
walletprocesspsbt <psbt> true "DEFAULT" true false
```

### Fetched UTXO Cards
`fetchUtxos()` uses `addFetchedInput()` to create compact read-only cards with hidden `<input>` elements (txid, vout, value, scriptPubKey) that preserve PSBT creation compatibility. The full address is shown in the source label. The fetch input is cleared after fetching. No empty input row is shown on page load — users click "+ Add Input (manual entry)" for manual UTXO entry.

### UTXO Container Selectors
`fetchUtxos()` adds `.utxo-source-label` divs to `#utxoContainer` alongside `[data-utxo]` rows. Always use `querySelectorAll('#utxoContainer [data-utxo]')` (not `.children`) to iterate inputs. Same for outputs: use `querySelectorAll('#outputContainer [data-output]')`.

### Test Hook
When `window.__TEST_MODE__ = true` (set via `page.add_init_script`), internal functions are exposed on `window._fn`, the bitcoin library on `window._bitcoin`, and the ECC library on `window._ecc`. This also prevents regtest option removal when no server is detected.

### Testnet4 Browser Signing
The testnet4 E2E test signs PSBTs in the browser using ECPair (loaded from `esm.sh/ecpair@3.0.0`). `sign_psbt_in_browser()` uses try/catch per input to skip non-matching inputs, enabling multi-wallet PSBT signing with a single function. Serial signing passes the partially-signed PSBT from key C to key D.

### Testnet4 Wallet Credentials
Loaded in order: CLI args (`--wif`, `--address`) > env vars (`TESTNET4_WIF`, `TESTNET4_ADDRESS`) > `settings.json`. For Claude Code, credentials are stored in `.claude/settings.local.json` under the `env` key. The `settings.json` file is in `.gitignore`.

## Running Tests

```bash
# Unit tests (no bitcoind needed, ~15s)
python3 tests/test_psbt_builder.py

# E2E regtest tests (needs bitcoind + bitcoin-cli, ~90s)
python3 tests/test_regtest_e2e.py

# E2E testnet4 tests (needs funded wallet, ~30s)
python3 tests/test_testnet4_e2e.py

# E2E with visible browser
python3 tests/test_regtest_e2e.py --headed
python3 tests/test_testnet4_e2e.py --headed

# Recover testnet4 funds from a failed run
python3 tests/test_testnet4_e2e.py --recover
```

## Dev Server

```bash
# Static server (no regtest)
python3 -m http.server 8000

# Regtest server (starts bitcoind, mines 101 blocks)
python3 server/server.py 8000 --regtest
```

Configurations are in `.claude/launch.json`.

## Dependencies

- Python 3 + Playwright (`pip install playwright && playwright install chromium`)
- Bitcoin Core v30+ (bitcoind + bitcoin-cli) for E2E tests
- No npm/node required — all JS dependencies loaded via CDN
