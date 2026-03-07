# Bitcoin Address Sweeper

Single-page web app for sweeping Bitcoin addresses across hardware wallets, hot wallets, and paper wallets into a single transaction using PSBTs (BIP 174).

## Architecture

- **`index.html`** — Entire frontend in one file. Uses bitcoinjs-lib v7.0.0-rc.0, bip32 v4.0.0, bs58check v3.0.1, bbqr (splitQRs/joinQRs), jsQR, qrcode-generator (all ESM via esm.sh/CDN), PaperCSS for styling. Includes a donate button linking to `donate.html`. Step 2 links to `sign.html` with network params.
- **`sign.html`** — Browser-based PSBT signer for hot/paper wallets. Accepts PSBT via file upload, QR scan (BBQr), or paste (hex/base64). Signs with WIF private key using ECPair (supports P2WPKH + P2TR). Outputs signed PSBT as hex, download, or BBQr QR code. Network passed via URL params from sweeper, or auto-detected standalone.
- **`donate.html`** — PaperCSS-styled donation page with QR code, clickable Bitcoin address, and link to ₿itcoin Gift Paper Wallet.
- **`server/server.py`** — Local development server for regtest. Manages an isolated bitcoind instance (RegtestNode class) and exposes mempool.space-compatible API endpoints so the frontend code needs minimal branching.
- **`tests/test_psbt_builder.py`** — 111 unit tests using Playwright (Python sync API). Tests core functions, DOM interactions, PSBT creation, xpub derivation, output percentage/wipe behavior, and QR code display.
- **`tests/test_regtest_e2e.py`** — 99 E2E tests covering P2WPKH and P2TR (Taproot), both parallel and serial signing. Requires bitcoind/bitcoin-cli.
- **`tests/test_testnet4_e2e.py`** — 27 E2E tests on real testnet4. Parallel + serial signing with browser-based ECPair signing, funds return to main wallet. Requires a pre-funded testnet4 wallet (credentials via env vars, CLI args, or settings.json).

## Key Patterns

### Network Auto-Selection
On load, the frontend detects the environment and auto-selects the network:
- **Regtest server** (`/api/health` responds with 2s timeout) → selects regtest, `serverMode=true`
- **Static server** (localhost but no `/api/health`) → selects testnet4
- **GitHub Pages** (not localhost) → selects mainnet, removes regtest option from dropdown

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

### Xpub-Based UTXO Fetching
The fetch input accepts both plain addresses and extended public keys (xpub/zpub/vpub/tpub/ypub/upub). When an xpub is detected via `isExtendedKey()`, `fetchUtxosFromXpub()` orchestrates a full wallet scan:

**Address type inference from SLIP-132 prefix** (`XPUB_ADDRESS_TYPES` map):
- `zpub`/`vpub` → P2WPKH only (BIP84)
- `ypub`/`upub` → P2SH-P2WPKH only (BIP49)
- `xpub`/`tpub` → both P2WPKH and P2TR (BIP84 + BIP86)

**Scanning**: `scanXpubAddresses()` derives addresses along one chain (0=receive, 1=change) for one address type, fetching UTXOs with a BIP44 gap limit of 20. Batches 5 addresses in parallel with 200ms delays for mempool.space rate limiting.

**Address generation** (`pubkeyToAddress()`):
- P2WPKH: `bitcoin.crypto.hash160()` + `bitcoin.address.toBech32(hash, 0, ...)`
- P2TR: Manual BIP341 taproot tweak using `bitcoin.crypto.taggedHash('TapTweak')` + `ecc.xOnlyPointAddTweak()` + `bitcoin.address.toBech32(tweaked, 1, ...)`. Note: `bitcoin.payments.p2tr` doesn't work in the ESM build ("Not enough data" error).
- P2SH-P2WPKH: `bitcoin.payments.p2wpkh()` + `bitcoin.payments.p2sh()`

**HW wallet info pre-population**: When UTXOs come from an xpub scan, `addFetchedInput()` receives an `hwInfo` parameter `{ xpub, path, pubkey }` that pre-fills the HW wallet fields. The section stays collapsed but shows a ✔️ prefix on the toggle text. The prefix is captured in a `hwPrefix` variable and used in both the initial set and the click handler so it persists through expand/collapse.

**Derivation path**: For standard account-level xpubs (depth 3), the full path is constructed as `m/{purpose}'/{coinType}'/0'/{chain}/{index}`. For non-standard depths, only the relative path `{chain}/{index}` is stored.

**Network validation**: Mainnet xpubs are rejected when testnet/regtest is selected and vice versa.

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

### PSBT QR Code Display (BBQr)
After creating a PSBT, results are shown in a collapsible area with PSBT hex, Download button, and a Show/Hide QR Code toggle. The QR is rendered using `qrcode-generator@1.4.4` on a 350×350 canvas with fixed 16px pixel margins.

For large PSBTs, `bbqr` (via esm.sh) splits the data into multiple QR parts using the BBQr protocol (Coinkite), natively supported by Coldcard Q. Key settings:
- `splitQRs(data, 'P', { encoding: 'Z', maxVersion: 20 })` — PSBT type, zlib+base32, max QR version 20
- `maxVersion: 20` optimized for 350px canvas: 97 modules → ~3.3px/cell → reliable camera scanning
- Multi-part animation cycles at 250ms per frame with consistent canvas sizing
- `renderQrToCanvas(qr, canvas, fixedPixels)` uses fixed pixel margins (not cell-based) so the QR pattern boundary stays identical across frames with different module counts
- `lastPsbt` stores the created PSBT; `hidePsbtResult()` clears stale results when inputs change

### QR Code Scanning (Upload Signed PSBTs)
The "Upload Signed PSBTs" section supports both file upload and camera-based QR scanning. A PSBT accumulator array collects PSBTs from both sources into a unified visual list.

- **Camera**: `getUserMedia({ facingMode: 'environment' })` opens rear camera in a 350px video element with orange border
- **Scan loop**: `requestAnimationFrame` → draw video to hidden `#qrScanCanvas` → `jsQR(imageData)` → `handleScannedQR()` (wrapped in try-catch to prevent silent loop death)
- **Format detection**: BBQr (`B$` prefix) → `handleBBQrPart()` with progress bar; base64 PSBT → check magic bytes `70736274ff`; hex PSBT → regex + magic bytes; non-PSBT → "QR detected — not a PSBT" feedback
- **BBQr multi-part**: Deduplicates by part number, shows progress bar (`scanned/total`), calls `joinQRs(parts)` when complete
- **PSBT list**: `.psbt-list-item` cards show source badge (File/QR), label, byte count, and remove button. Styled like `.utxo-fetched` cards.
- **Combine handler**: Reads from `psbtAccumulator[]` instead of file input. Clears accumulator after successful finalize.
- **File input change handler**: Eagerly reads files into accumulator on selection, clears input for re-selection. Existing E2E tests work unchanged since `page.set_input_files()` triggers the change event.

### Hot Wallet Transaction Signer (sign.html)
Separate page linked from Step 2 of the sweeper. Lets users sign PSBTs in-browser using a WIF private key.

**Link from sweeper**: `updateSignPageLink()` dynamically sets the href with `?network=` and `&serverMode=` params. Called from `detectServer()` and the network `change` handler.

**PSBT loading**: File upload (styled label), QR scan (BBQr multi-part support), or paste textarea (hex/base64). All three methods call `loadPsbtFromBytes(data, label)` which parses with `bitcoin.Psbt.fromBuffer()` and displays info (inputs, outputs, total sats, fee).

**Signing**: `ECPair.fromWIF(wif, network)` + `psbt.signInput(i, keyPair)` with try/catch per input. ECPair provides `signSchnorr` via tiny-secp256k1, so both P2WPKH and P2TR (taproot) inputs are supported. WIF validation shows derived P2WPKH address as feedback, with network mismatch detection.

**Output**: Signed PSBT hex in collapsible `<details>`, download as `.psbt` file, BBQr QR code display (same `renderQrToCanvas` + `splitQRs` pattern as index.html).

**Network**: URL param `?network=` takes priority over auto-detection. Auto-detection matches index.html logic (`/api/health` → regtest, isLocalhost → testnet4, else mainnet). "Back to Sweeper" link preserves network params.

**BigInt**: bitcoinjs-lib v7 uses BigInt for transaction values. Accumulate with `0n`/`BigInt()`, convert to `Number()` only for `toLocaleString()` display.

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

## CLI Tools

### `tools/sign-psbt.py` — PSBT Signing
Signs a PSBT file with a WIF private key. Outputs `<name>-signed.psbt` in the same directory (overwrites if exists). Requires `pip install embit`.

```bash
python3 tools/sign-psbt.py <psbt-file> <wif>
```

## Dependencies

- Python 3 + Playwright (`pip install playwright && playwright install chromium`)
- [embit](https://github.com/nicolo-ribaudo/embit) (`pip install embit`) for `tools/sign-psbt.py`
- Bitcoin Core v30+ (bitcoind + bitcoin-cli) for E2E tests
- No npm/node required — all JS dependencies loaded via CDN
