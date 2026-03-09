# Bitcoin Address Sweeper

Single-page web app for sweeping Bitcoin addresses across hardware wallets, hot wallets, and paper wallets into a single transaction using PSBTs (BIP 174).

## Architecture

- **`index.html`** — Entire frontend in one file. Uses bitcoinjs-lib v7.0.0-rc.0, bip32 v4.0.0, bs58check v3.0.1, ecpair v3.0.0, bbqr (splitQRs/joinQRs), jsQR (all ESM via esm.sh/CDN), custom `qr_generator.js`. Dark theme with CSS variables matching bitcoin-gift-paper-wallet (frosted glass cards, gradient background, Bitcoin orange accents). Step indicator wizard UI with dynamic step flow. Includes a donate button linking to `donate.html`.
- **`donate.html`** — Dark-themed donation page with QR code, clickable Bitcoin address, and link to ₿itcoin Gift Paper Wallet.
- **`qr_generator.js`** — Custom pure-JS QR code generator (shared with bitcoin-gift-paper-wallet project). Supports versions 1–20, EC levels L/M/Q/H, alphanumeric + byte + numeric modes. Replaces the `qrcode-generator@1.4.4` CDN dependency. API: `QRGenerator.generateQR(text, ecLevel)` returns a boolean[][] matrix.
- **`server/server.py`** — Local development server for regtest. Manages an isolated bitcoind instance (RegtestNode class) and exposes mempool.space-compatible API endpoints so the frontend code needs minimal branching.
- **`tests/test_psbt_builder.py`** — 178 unit tests using Playwright (Python sync API). Tests core functions, DOM interactions, PSBT creation, xpub derivation, output percentage/wipe behavior, QR code display, `isExtendedKey`, `pubkeyToAddress`, `handleScannedQR`, PSBT accumulator, WIF detection, step indicator wizard, dynamic step layout, and tip section.
- **`tests/test_regtest_e2e.py`** — 148 E2E tests covering P2WPKH and P2TR (Taproot), both parallel and serial signing, WIF fetch + inline signing E2E flow, and mixed WIF partial signing E2E flow. Requires bitcoind/bitcoin-cli.
- **`tests/test_coldcard_simulation.py`** — 44 tests simulating Coldcard signing behavior using bitcoin-cli `walletprocesspsbt`. Tests parallel signing, serial mixed WIF+CC signing, and website PSBT format via Playwright. No physical Coldcard needed. Requires bitcoind/bitcoin-cli and embit (`pip install embit`).
- **`tests/_test_coldcard_regtest.py`** — 28 tests with a real Coldcard MK4 via `ckcc sign` CLI. Tests the full mixed WIF+Coldcard signing flow end-to-end: builds PSBT with bip32Derivation, pre-signs WIF input, sends to Coldcard for signing (user approves on device), verifies no P2PKH bug, finalizes, broadcasts on regtest, and confirms the tx is mined with correct recipient balance. Requires Coldcard MK4 + ckcc-protocol + bitcoind + embit. Device info auto-detected via `ckcc xfp/pubkey` (address derived locally from pubkey to avoid `ckcc addr` which blocks the device).
- **`tests/_test_coldcard_testnet4.py`** — 25 tests with a real Coldcard MK4 on testnet4. Builds a 2-input PSBT (CC + WIF), pre-signs WIF via embit, sends to Coldcard via `ckcc sign`, finalizes, broadcasts to testnet4 mempool.space, verifies tx in mempool and funds returned. Requires Coldcard MK4 + ckcc-protocol + embit + TESTNET4_WIF/TESTNET4_ADDRESS env vars.
- **`tests/_test_coldcard_website_e2e.py`** — 23 tests for the full browser + Coldcard E2E flow on testnet4. Playwright drives the actual sweeper website: fetches WIF + CC UTXOs, enters HW wallet info (xfp/pubkey/path), creates & partially signs PSBT via the website, signs with Coldcard via `ckcc sign`, uploads CC-signed PSBT back, combines & finalizes in browser, broadcasts to testnet4. Requires Coldcard MK4 + ckcc-protocol + Playwright + TESTNET4_WIF/TESTNET4_ADDRESS env vars.
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

**Master fingerprint propagation**: Xpub source labels (`.utxo-source-label[data-xpub-source]`) include a `.xpub-xfp` input for the master fingerprint. On input, the value propagates to all `.hw-xfp` fields of UTXOs under that label via DOM sibling traversal. The master fingerprint cannot be derived from an xpub — it must be entered manually (e.g. Coldcard: Advanced > View Identity). At PSBT creation time, a `confirm()` warning appears if any input has pubkey + derivation path but no master fingerprint.

**Network validation**: Mainnet xpubs are rejected when testnet/regtest is selected and vice versa.

### WIF-Based UTXO Fetching & Inline Signing
The fetch input also accepts WIF private keys (Wallet Import Format). `isWif(input)` validates against the selected network only (mainnet: 5/K/L, testnet/regtest: c/9) using `ECPair.fromWIF(input, getSelectedNetwork())`.

**Fetch flow** (`fetchUtxosFromWif()`):
1. Parse WIF via `ECPair.fromWIF(wif, network)` — catch and show error in `#fetchStatus`
2. Derive P2WPKH address via `bitcoin.payments.p2wpkh({ pubkey, network })`
3. Derive P2TR address via `pubkeyToAddress(pubkey, 'p2tr', network)`
4. Fetch UTXOs for both addresses, call `addFetchedInput()` with `wif` parameter
5. Clear `#fetchAddress` immediately (WIF must not linger in DOM)

**Per-UTXO WIF storage**: `addFetchedInput()` accepts optional `wif` parameter. When provided, sets `data-wif` attribute on the `[data-utxo]` div, adds a collapsible readonly WIF field (same pattern as HW wallet toggle), and shows ✔️ prefix on toggle. Manual input rows (`addInput()`) also have an editable WIF field — entering a valid WIF sets `data-wif` and shows the checkmark.

**Inline signing**: When `allUtxosHaveWif()` returns true, the Create button becomes "Create, Sign & Finalize". Clicking it creates the PSBT, signs each input with its per-UTXO WIF via `ECPair.fromWIF()` + `psbt.signInput()` (try/catch per input), finalizes, extracts the raw transaction, and navigates directly to the Broadcast step.

**Deferred WIF signing (mixed mode)**: When `someUtxosHaveWif()` returns true (mixed mode — some UTXOs have WIFs, some don't), the Create button becomes "Create PSBT (sign WIF after HW)". WIF inputs are intentionally left unsigned at PSBT creation time. The unsigned PSBT is shown via QR/file for the HW wallet to sign its inputs. After the HW-signed PSBT is uploaded, the combine step signs WIF inputs in the browser via `ECPair.fromWIF()` + `psbt.signInput()`, then finalizes. This deferred approach avoids the Coldcard Q auto-finalize bug: if WIF inputs were pre-signed, the CC Q would see all inputs signed and auto-finalize via QR, incorrectly putting P2WPKH signatures in scriptSig (P2PKH-style) instead of witness.

**Dispatch order** in `fetchUtxos()`: `isExtendedKey()` → `isWif()` → single address.

### Step Indicator Wizard UI
The UI uses a step-indicator wizard layout with numbered circles connected by lines. Steps adapt dynamically based on whether WIF private keys are present for all UTXOs.

**Step indicator**: `#stepIndicator` div with `.step` circles (numbered, with labels) and `.step-line` connectors. States: active (orange border `#f7931a`), done (green `#4caf50`), inactive (gray `#ddd`).

**Cards**: Three card divs (`#cardCreate`, `#cardSign`, `#cardBroadcast`) — only one visible at a time via `showCard(id)`. Navigation buttons between cards: `#nextToSign`, `#backToCreate`, `#backToSign`, `#nextToBroadcast`.

**Dynamic layout** (`updateStepLayout()`):
- **All WIFs present** (2-step mode): Shows `[1: Create] → [2: Broadcast]`, hides Sign/Combine steps, button says "Create, Sign & Finalize"
- **Some WIFs** (4-step mode): Shows full 4-step layout, button says "Create & Partially Sign PSBT"
- **No WIFs** (4-step mode): Shows full 4-step layout, button says "Create PSBT"

Called on init and whenever UTXOs change (add/remove/fetch/WIF entry).

**`setStep(n)`**: Updates step indicator circles — marks steps below `n` as done, step `n` as active.

**Combine handler**: Auto-navigates to broadcast card after successful combine (`setStep(3)` → `showCard('cardBroadcast')`).

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
- **Available sats**: `getAvailableSats()` returns `totalInput - estimatedFee` using rough vsize heuristic `ceil(10.5 + 68*nIn + 31*nOut)`. The `nOut` count includes the tip output when tip sats > 0 (via `getTipOutputCount()`).
- **Fee rate required**: The fee rate field is collapsible with a chevron toggle and summary showing the current rate + estimated fee when collapsed.
- **Wipe calculation**: `recalcWipeOutput()` subtracts tip sats from available balance when calculating wipe output value.
- **Percentage warning**: If outputs don't sum to ~100% of available sats and no wipe output exists, a `confirm()` dialog warns the user. Leftover becomes extra miner fee.

### Tip Section
Collapsible section below Fee Rate with preset buttons and per-network donation addresses.

- **Toggle**: Chevron + summary (e.g., "100 sats | 0.1%") when collapsed. Defaults to collapsed with 0.99% preset active.
- **Presets**: `0.99%`, `0.5%`, `0.1%`, `No Tip` — clicking recalculates tip sats from total input. Active preset highlighted orange. Custom sats entry deselects all presets.
- **Per-network addresses** (`TIP_ADDRESSES` map): mainnet (`bc1q...`), testnet (`tb1q...`), regtest (`bcrt1q...`). Updated via `updateTipAddress()` on network change.
- **Integration**: `gatherOutputs()` appends tip as an additional output when sats > 0. `getTipOutputCount()` returns 0 or 1 for vsize estimation. `recalcTipIfPreset()` auto-recalculates tip when inputs change (called from `updateOutputPercentages()`).
- **Summary**: `updateTipSummary()` shows sats + percentage in the collapsed header.

### Default Rows on Page Load
One default empty output row is shown on page load. No default input rows — users click "+ Add Input (manual entry)" or use "Fetch & Add UTXOs".

### UTXO Container Selectors
`fetchUtxos()` adds `.utxo-source-label` divs to `#utxoContainer` alongside `[data-utxo]` rows. Always use `querySelectorAll('#utxoContainer [data-utxo]')` (not `.children`) to iterate inputs. Same for outputs: use `querySelectorAll('#outputContainer [data-output]')`.

`removeOrphanedSourceLabels()` cleans up source labels when UTXOs are removed. Called from both `addInput()` and `addFetchedInput()` remove handlers. When all UTXOs under a label are removed, the label is removed. When some remain, the label's total BTC and UTXO count are recalculated.

### Test Hook
When `window.__TEST_MODE__ = true` (set via `page.add_init_script`), internal functions are exposed on `window._fn`, the bitcoin library on `window._bitcoin`, and the ECC library on `window._ecc`. This also prevents regtest option removal when no server is detected. The test hook also exposes `window._ECPair` and `window._Buffer`.

### Testnet4 Browser Signing
The testnet4 E2E test signs PSBTs in the browser using ECPair (loaded from `esm.sh/ecpair@3.0.0`). `sign_psbt_in_browser()` uses try/catch per input to skip non-matching inputs, enabling multi-wallet PSBT signing with a single function. Serial signing passes the partially-signed PSBT from key C to key D.

### Custom QR Code Generator (qr_generator.js)
All pages use `qr_generator.js` — a custom pure-JS QR code generator shared with the bitcoin-gift-paper-wallet project. It replaces the `qrcode-generator@1.4.4` CDN dependency.

**API**: `QRGenerator.generateQR(text, ecLevel)` returns a 2D boolean array (true = dark module). `QRGenerator.qrToCanvas(matrix, ctx, x, y, moduleSize, border)` renders to a canvas context.

**Rendering in index.html**: `renderQrToCanvas(matrix, canvas, fixedPixels)` iterates the boolean matrix using `matrix[row][col]` with fixed 16px pixel margins for consistent sizing across animated BBQr frames.

**Modes**: Alphanumeric (uppercased text fitting `0-9A-Z $%*+-./:`) and byte (everything else, UTF-8 encoded). Mode is auto-detected.

**Version range**: 1–20. Versions 7+ include BCH(18,6)-encoded version information in two 6×3 blocks.

### PSBT QR Code Display (BBQr)
After creating a PSBT, results are shown in a collapsible area with PSBT hex, Download button, and a Show/Hide QR Code toggle. The QR is rendered using `qr_generator.js` on a 350×350 canvas with fixed 16px pixel margins.

For large PSBTs, `bbqr` (via esm.sh) splits the data into multiple QR parts using the BBQr protocol (Coinkite), natively supported by Coldcard Q. Key settings:
- `splitQRs(data, 'P', { encoding: 'Z', maxVersion: 20 })` — PSBT type, zlib+base32, max QR version 20
- `maxVersion: 20` optimized for 350px canvas: 97 modules → ~3.3px/cell → reliable camera scanning
- Multi-part animation cycles at 250ms per frame with consistent canvas sizing
- `renderQrToCanvas(matrix, canvas, fixedPixels)` uses fixed pixel margins (not cell-based) so the QR pattern boundary stays identical across frames with different module counts
- `lastPsbt` stores the created PSBT; `hidePsbtResult()` clears stale results when inputs change

### QR Code Scanning (Upload Signed PSBTs)
The "Upload Signed PSBTs" section supports both file upload and camera-based QR scanning. A PSBT accumulator array collects PSBTs from both sources into a unified visual list.

- **Camera**: `getUserMedia({ facingMode: 'environment' })` opens rear camera in a 350px video element with orange border
- **Scan loop**: `requestAnimationFrame` → draw video to hidden `#qrScanCanvas` → `jsQR(imageData)` → `handleScannedQR()` (wrapped in try-catch to prevent silent loop death)
- **Format detection**: BBQr (`B$` prefix) → `handleBBQrPart()` with progress bar; raw binary PSBT → check first 5 bytes for magic `70736274ff`; base64 PSBT → decode + check magic bytes; hex PSBT → regex + magic bytes; raw transaction hex → version bytes `01000000`/`02000000` → sets `finalTxHex` and navigates to broadcast (Coldcard Q outputs finalized tx, not PSBT, when all inputs are signed); non-PSBT → "QR detected — not a PSBT" feedback
- **BBQr multi-part**: Deduplicates by part number, shows progress bar (`scanned/total`), calls `joinQRs(parts)` when complete
- **PSBT list**: `.psbt-list-item` cards show source badge (File/QR), label, byte count, and remove button. Styled like `.utxo-fetched` cards.
- **Combine handler**: Reads from `psbtAccumulator[]` instead of file input. Clears accumulator after successful finalize.
- **File input change handler**: Eagerly reads files into accumulator on selection, clears input for re-selection. Existing E2E tests work unchanged since `page.set_input_files()` triggers the change event.

### Testnet4 Wallet Credentials
Loaded in order: CLI args (`--wif`, `--address`) > env vars (`TESTNET4_WIF`, `TESTNET4_ADDRESS`) > `settings.json`. For Claude Code, credentials are stored in `.claude/settings.local.json` under the `env` key. The `settings.json` file is in `.gitignore`.

## Running Tests

```bash
# Unit tests — index.html (no bitcoind needed, ~15s)
python3 tests/test_psbt_builder.py

# E2E regtest tests (needs bitcoind + bitcoin-cli, ~120s)
python3 tests/test_regtest_e2e.py

# Coldcard simulation tests (needs bitcoind + embit, ~120s)
python3 tests/test_coldcard_simulation.py

# Real Coldcard MK4 tests (needs Coldcard + ckcc + bitcoind + embit)
# User must approve transaction on device when prompted
python3 tests/_test_coldcard_regtest.py

# E2E testnet4 tests (needs funded wallet, ~30s)
python3 tests/test_testnet4_e2e.py

# E2E with visible browser
python3 tests/test_psbt_builder.py --headed
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

## Known Issues

### Coldcard Q auto-finalizes via QR when all inputs are signed
When the Coldcard Q receives a PSBT via QR code where all inputs have `partial_sigs` (i.e., all inputs are already signed or signable), it auto-finalizes and outputs a raw transaction hex instead of a PSBT. This differs from `ckcc sign` via USB, which returns a PSBT with `partial_sigs` (not finalized). The auto-finalization incorrectly puts P2WPKH witness signatures in `scriptSig` (P2PKH-style) instead of the witness field, causing "Witness requires empty scriptSig" on broadcast. **Workaround**: WIF inputs are left unsigned when creating the PSBT for QR display to the Coldcard. After the Coldcard signs its input and returns the PSBT, WIF inputs are signed in the browser during the combine step.

### `ckcc addr` blocks the Coldcard USB interface
`ckcc addr -s -q <path>` returns the address to the CLI immediately, but the Coldcard's `show_address` protocol command displays the address on the device screen and waits for the user to dismiss it (press OK/X). While the address is displayed, the device is "busy" — any subsequent `ckcc` command (including `ckcc sign`) fails with "Coldcard is handling another request right now." There is no `--no-display` or `--silent` flag. The Coldcard tests use `ckcc pubkey` + local address derivation via embit instead to avoid blocking the device.

## Dependencies

- Python 3 + Playwright (`pip install playwright && playwright install chromium`)
- [embit](https://github.com/nicolo-ribaudo/embit) (`pip install embit`) for `tools/sign-psbt.py` and Coldcard tests
- [ckcc-protocol](https://github.com/Coldcard/ckcc-protocol) (`pip install ckcc-protocol`) for real Coldcard MK4 tests
- Bitcoin Core v30+ (bitcoind + bitcoin-cli) for E2E tests
- No npm/node required — all JS dependencies loaded via CDN
