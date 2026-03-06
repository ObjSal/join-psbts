# Bitcoin PSBT Builder

Build, sign, and broadcast multi-wallet Bitcoin transactions using PSBTs ([BIP 174](https://github.com/bitcoin/bips/blob/master/bip-0174.mediawiki)).

**[Live Demo](https://objsal.github.io/join-psbts/)**

## What It Does

This tool lets multiple wallet holders collaborate on a single Bitcoin transaction. Each person contributes UTXOs as inputs, signs their portion independently, and the results are combined into a finalized transaction ready for broadcast.

### Workflow

1. **Create** -- Add inputs (UTXOs) from multiple wallets, set outputs and fee, download the unsigned PSBT
2. **Sign** -- Each wallet holder signs the PSBT with their own wallet (hardware wallet, Bitcoin Core, etc.)
3. **Combine & Finalize** -- Upload all signed PSBTs, the tool merges signatures and produces the raw transaction
4. **Broadcast** -- Send the finalized transaction to the Bitcoin network via mempool.space

### Signing Approaches

| Approach | How it works | Upload |
|----------|-------------|--------|
| **Parallel** | Each party independently signs a copy of the unsigned PSBT | Upload all signed copies |
| **Serial** | Party A signs, passes to B, B signs, passes to C... | Upload the single final file |

Both approaches work through the same Combine & Finalize step.

## Features

- **Fetch UTXOs** by address from mempool.space (or local regtest server), displayed as compact read-only cards
- **Fee rate presets** pulled live from the network (fast/medium/slow), defaults to slow
- **Hardware wallet support** with BIP32 derivation paths, master fingerprint, and xpub auto-derivation of compressed public keys (supports xpub/ypub/zpub/vpub/tpub/upub formats via SLIP-132 normalization)
- **Drag-and-drop reordering** of inputs and outputs
- **Network support** for Mainnet, Testnet4, and Regtest
- **Guided workflow** with brief instructions under each step
- **No server required** -- runs entirely in the browser on GitHub Pages
- **Regtest mode** with a local Python server for development and testing

## Usage

### GitHub Pages (Mainnet / Testnet)

Visit the [live demo](https://objsal.github.io/join-psbts/) -- no installation needed.

### Local Development (Regtest)

Requires [Bitcoin Core](https://bitcoincore.org/en/download/) (bitcoind + bitcoin-cli).

```bash
# Start the regtest server (launches bitcoind, mines initial blocks)
python3 server/server.py 8000 --regtest

# Open in browser
open http://localhost:8000/index.html
```

The server provides a faucet and auto-mining, and exposes mempool.space-compatible API endpoints so the frontend works identically across all networks.

## Testing

```bash
# Unit tests -- 101 tests, no bitcoind needed (~15s)
python3 tests/test_psbt_builder.py

# E2E regtest tests -- 99 tests, requires bitcoind + bitcoin-cli (~90s)
# Covers P2WPKH + P2TR (Taproot), parallel + serial signing
python3 tests/test_regtest_e2e.py

# E2E testnet4 tests -- 27 tests, requires funded testnet4 wallet (~30s)
# Parallel + serial signing with real testnet4 transactions
python3 tests/test_testnet4_e2e.py

# E2E with visible browser
python3 tests/test_regtest_e2e.py --headed
python3 tests/test_testnet4_e2e.py --headed

# Recover funds from a failed testnet4 test run
python3 tests/test_testnet4_e2e.py --recover
```

### Testnet4 Wallet Setup

The testnet4 E2E test needs a pre-funded wallet. Provide credentials via:

1. **Environment variables**: `TESTNET4_WIF` and `TESTNET4_ADDRESS`
2. **CLI arguments**: `--wif` and `--address`
3. **settings.json** in project root: `{"TESTNET4_WIF": "c...", "TESTNET4_ADDRESS": "tb1q..."}`

Fund the wallet at the [testnet4 faucet](https://mempool.space/testnet4/faucet).

### Prerequisites

- Python 3
- [Playwright](https://playwright.dev/python/): `pip install playwright && playwright install chromium`
- Bitcoin Core v30+ (for regtest E2E tests only)

## Tech Stack

- **Frontend**: Single `index.html` file, no build step
- **JS Libraries** (loaded via CDN): [bitcoinjs-lib](https://github.com/nicolo-ribaudo/bitcoinjs-lib) v7.0.0-rc.0, [bip32](https://github.com/nicolo-ribaudo/bip32) v4.0.0, [bs58check](https://github.com/nicolo-ribaudo/bs58check) v3.0.1, [PaperCSS](https://www.getpapercss.com/), [SortableJS](https://sortablejs.github.io/Sortable/)
- **Dev Server**: Python stdlib (`http.server`) + Bitcoin Core RPC
- **Tests**: [Playwright](https://playwright.dev/python/) (Python sync API)

## Support This Project

Building and maintaining open-source Bitcoin tools takes time, caffeine, and compute. If you find this project useful, consider buying me a coffee — with Bitcoin!

<div align="center">

**`bc1qrfagrsfrm8erdsmrku3fgq5yc573zyp2q3uje8`**

*This address was generated using [₿itcoin Gift Paper Wallet](https://objsal.github.io/bitcoin-gift-paper-wallet/)*

</div>

Your donation helps cover the cost of Claude (the AI that helped build this), keeps the coffee flowing, and fuels development of more open-source Bitcoin tools. No VC funding, no ads, no tracking — just open-source code and generous supporters like you.

## License

This project is provided as-is, without warranty of any kind. The author is not responsible for any loss of funds from transactions created with this tool. Always verify addresses, amounts, and fees before signing and broadcasting.
