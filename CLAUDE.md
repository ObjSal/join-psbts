# Bitcoin PSBT Builder

Single-page web app for building, signing, combining, and broadcasting multi-wallet Bitcoin PSBTs (BIP 174).

## Architecture

- **`index.html`** — Entire frontend in one file. Uses bitcoinjs-lib v7.0.0-rc.0, bip32 v4.0.0, bs58check v3.0.1 (all ESM via esm.sh), PaperCSS for styling. Includes a donate button linking to `donate.html`.
- **`donate.html`** — PaperCSS-styled donation page with QR code, clickable Bitcoin address, and link to ₿itcoin Gift Paper Wallet.
- **`server/server.py`** — Local development server for regtest. Manages an isolated bitcoind instance (RegtestNode class) and exposes mempool.space-compatible API endpoints so the frontend code needs minimal branching.
- **`tests/test_psbt_builder.py`** — 108 unit tests using Playwright (Python sync API). Tests core functions, DOM interactions, PSBT creation, xpub derivation, and output percentage/wipe behavior.
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

### Xpub Auto-Derivation (Hardware Wallet)
The HW wallet section includes an xpub field that auto-derives the compressed public key. Three functions handle this:
- **`normalizeExtendedKey(key)`** — Converts SLIP-132 prefixes (ypub/zpub/vpub/upub and multisig variants) to canonical xpub/tpub using a version-bytes map, so `bip32.fromBase58()` accepts any format.
- **`getRelativePath(fullPath, xpubDepth)`** — Extracts the unhardened child path relative to the xpub's depth (e.g., full path `m/84'/1'/0'/0/0` with depth 3 → `0/0`). Rejects hardened segments since xpub can only derive public children.
- **`derivePublicKeyFromXpub(xpubStr, fullPath)`** — Normalizes the key, parses it with bip32, derives the child node, and returns the 66-hex compressed public key.

When both xpub and path fields are filled, the pubkey field auto-populates and becomes read-only. When xpub is empty, pubkey reverts to manual entry (existing workflow preserved).

### Hardened Path Normalization
bitcoinjs-lib only recognizes `'` (apostrophe) for hardened BIP32 path segments. The `h` and `H` suffixes used by Coldcard and other hardware wallets are silently treated as unhardened, producing wrong indices in the PSBT binary. The derivation path is normalized (`h`/`H` → `'`) when reading from the DOM before writing to `bip32Derivation`.

### Output Percentage Labels & Wipe
Each output row has a value (sats) field, a small read-only percentage label below it, and a Wipe checkbox.
- **Percentage label**: `updateOutputPercentages()` displays `sats / totalInput * 100` as a small `<small>` label below the sats input. Purely informational — not editable.
- **Wipe output**: Only one output can have Wipe checked. The wipe row's value is auto-calculated as `available - sumOfOtherOutputs`. Its value field is disabled.
- **Available sats**: `getAvailableSats()` returns `totalInput - estimatedFee` using rough vsize heuristic `ceil(10.5 + 68*nIn + 31*nOut)`.
- **Fee rate required**: The fee rate field is always visible (no change mode toggle). Creating a PSBT requires a positive fee rate.
- **Percentage warning**: If outputs don't sum to ~100% of available sats and no wipe output exists, a `confirm()` dialog warns the user. Leftover becomes extra miner fee.

### Default Rows on Page Load
One default empty output row is shown on page load. No default input rows — users click "+ Add Input (manual entry)" or use "Fetch & Add UTXOs".

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
