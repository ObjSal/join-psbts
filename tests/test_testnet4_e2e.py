#!/usr/bin/env python3
"""
End-to-end testnet4 test for Bitcoin Address Sweeper.

Tests the full multi-wallet PSBT workflow on Bitcoin Testnet4 via mempool.space:

  Part A — Parallel Signing:
  1. Generate 4 temporary wallets (A, B for parallel; C, D for serial)
  2. Fund all 4 from a pre-funded main wallet (single funding TX)
  3. Fetch A+B UTXOs, create multi-input return PSBT
  4. Sign independently with A and B (parallel), combine, broadcast

  Part B — Serial Signing:
  5. Fetch C+D UTXOs, create multi-input return PSBT
  6. Sign with C first, then D signs C's output (serial chain)
  7. Finalize single file, broadcast — all funds return to main wallet

No local bitcoind needed — uses mempool.space Testnet4 API.
No mining waits — completes as soon as mempool accepts transactions.

Requires a pre-funded Testnet4 wallet. Configure in settings.json:
    {"TESTNET4_WIF": "cXXX...", "TESTNET4_ADDRESS": "tb1q..."}

Or set TESTNET4_WIF / TESTNET4_ADDRESS environment variables, or pass as CLI args.

Usage:
    python3 tests/test_testnet4_e2e.py                # headless
    python3 tests/test_testnet4_e2e.py --headed        # visible browser
    python3 tests/test_testnet4_e2e.py --recover       # recover funds from failed run
"""

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.request import urlopen, Request
from urllib.error import URLError

from playwright.sync_api import sync_playwright

# ============================================================
# Configuration
# ============================================================

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TEST_DIR)
SETTINGS_FILE = os.path.join(_PROJECT_ROOT, "settings.json")
RECOVERY_FILE = os.path.join(_TEST_DIR, "testnet4_recovery.json")
SERVER_READY_TIMEOUT = 15
MEMPOOL_PROPAGATION_WAIT = 10  # seconds between transactions
MEMPOOL_API = "https://mempool.space/testnet4/api"

# Amounts (sats) — parallel (A+B) and serial (C+D)
FUND_A_SATS = 10_000
FUND_B_SATS = 5_000
FUND_C_SATS = 8_000
FUND_D_SATS = 6_000
RETURN_FEE_SATS = 500
PARALLEL_RETURN_SATS = FUND_A_SATS + FUND_B_SATS - RETURN_FEE_SATS  # 14,500
SERIAL_RETURN_SATS = FUND_C_SATS + FUND_D_SATS - RETURN_FEE_SATS    # 13,500
TOTAL_FUND_SATS = FUND_A_SATS + FUND_B_SATS + FUND_C_SATS + FUND_D_SATS
MIN_BALANCE_SATS = TOTAL_FUND_SATS + 2_000  # extra for funding tx fee

# ECPair CDN import (used in page.evaluate for signing)
ECPAIR_IMPORT = "const { ECPairFactory } = await import('https://esm.sh/ecpair@3.0.0');"


# ============================================================
# Test infrastructure
# ============================================================

_pass_count = 0
_fail_count = 0
_failures = []


def test(name, condition, detail=""):
    global _pass_count, _fail_count
    if condition:
        _pass_count += 1
        print(f"  \u2713 {name}")
    else:
        _fail_count += 1
        msg = f"  \u2717 {name}"
        if detail:
            msg += f"  \u2014 {detail}"
        print(msg)
        _failures.append(name)


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ============================================================
# Server lifecycle (static file server for the UI)
# ============================================================

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port):
    """Start a static file server for index.html."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port)],
        cwd=_PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(SERVER_READY_TIMEOUT):
        try:
            urlopen(f"http://127.0.0.1:{port}/index.html", timeout=2)
            return proc
        except (URLError, ConnectionRefusedError, OSError):
            time.sleep(1)
        if proc.poll() is not None:
            raise RuntimeError(f"Server exited (rc={proc.returncode})")
    proc.kill()
    raise RuntimeError(f"Server not ready within {SERVER_READY_TIMEOUT}s")


def stop_server(proc):
    if not proc or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ============================================================
# Pre-flight balance check via mempool.space
# ============================================================

def preflight_balance_check(address):
    """Verify the address has enough funds before running the test."""
    url = f"{MEMPOOL_API}/address/{address}/utxo"
    print(f"\n--- Pre-flight: checking {address[:20]}...{address[-8:]} ---")

    try:
        req = Request(url, headers={"User-Agent": "PSBTBuilder/1.0"})
        resp = urlopen(req, timeout=15)
        utxos = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  ERROR: mempool.space unreachable: {e}")
        sys.exit(1)

    if not utxos:
        print(f"  \u2717 No UTXOs found.")
        print(f"  Fund at: https://mempool.space/testnet4/faucet")
        print(f"  Address: {address}")
        sys.exit(1)

    total_sats = sum(u.get("value", 0) for u in utxos)
    confirmed = [u for u in utxos if u.get("status", {}).get("confirmed")]
    unconfirmed = [u for u in utxos if not u.get("status", {}).get("confirmed")]

    if total_sats < MIN_BALANCE_SATS:
        print(f"  \u2717 Balance too low: {total_sats:,} sats (need {MIN_BALANCE_SATS:,})")
        print(f"  Fund at: https://mempool.space/testnet4/faucet")
        print(f"  Address: {address}")
        sys.exit(1)

    parts = []
    if confirmed:
        parts.append(f"{len(confirmed)} confirmed")
    if unconfirmed:
        parts.append(f"{len(unconfirmed)} unconfirmed")
    print(f"  \u2713 Balance: {total_sats:,} sats ({' + '.join(parts)} UTXOs)")
    return total_sats


# ============================================================
# Browser helpers — keypair generation and PSBT signing
# ============================================================

def preload_ecpair(page):
    """Preload ECPair from CDN; fail fast if unavailable."""
    ok = page.evaluate(f"""async () => {{
        try {{
            {ECPAIR_IMPORT}
            return true;
        }} catch (e) {{
            return false;
        }}
    }}""")
    if not ok:
        print("ERROR: Could not load ECPair library from CDN (esm.sh)")
        sys.exit(1)


def generate_keypair(page):
    """Generate a random P2WPKH keypair on testnet via the browser."""
    return page.evaluate(f"""async () => {{
        {ECPAIR_IMPORT}
        const ECPair = ECPairFactory(window._ecc);
        const network = window._fn.getSelectedNetwork();
        const bitcoin = window._bitcoin;

        const keyPair = ECPair.makeRandom({{ network, compressed: true }});
        const {{ address }} = bitcoin.payments.p2wpkh({{
            pubkey: keyPair.publicKey,
            network
        }});
        return {{ wif: keyPair.toWIF(), address }};
    }}""")


def verify_wif_matches_address(page, wif, expected_address):
    """Verify a WIF decodes to the expected P2WPKH address."""
    return page.evaluate(f"""async (args) => {{
        const {{ wif, expectedAddr }} = args;
        {ECPAIR_IMPORT}
        const ECPair = ECPairFactory(window._ecc);
        const network = window._fn.getSelectedNetwork();
        const bitcoin = window._bitcoin;

        const keyPair = ECPair.fromWIF(wif, network);
        const {{ address }} = bitcoin.payments.p2wpkh({{
            pubkey: keyPair.publicKey,
            network
        }});
        return {{ address, matches: address === expectedAddr }};
    }}""", {"wif": wif, "expectedAddr": expected_address})


def sign_psbt_in_browser(page, psbt_base64, wif):
    """Sign all matching PSBT inputs with a WIF key via ECPair in the browser.

    Uses try/catch per input to skip non-matching inputs (safe for multi-wallet PSBTs).
    Returns dict with {psbt: base64, signed: int, error: str|null}.
    """
    result = page.evaluate(f"""async (args) => {{
        const {{ psbtB64, wif }} = args;
        try {{
            {ECPAIR_IMPORT}
            const ECPair = ECPairFactory(window._ecc);
            const network = window._fn.getSelectedNetwork();
            const bitcoin = window._bitcoin;

            const keyPair = ECPair.fromWIF(wif, network);
            const psbt = bitcoin.Psbt.fromBase64(psbtB64, {{ network }});

            let signed = 0;
            for (let i = 0; i < psbt.data.inputs.length; i++) {{
                try {{
                    psbt.signInput(i, keyPair);
                    signed++;
                }} catch (e) {{
                    // skip — this input doesn't belong to this key
                }}
            }}

            return {{ psbt: psbt.toBase64(), signed, error: null }};
        }} catch (e) {{
            return {{ psbt: null, signed: 0, error: e.message }};
        }}
    }}""", {"psbtB64": psbt_base64, "wif": wif})

    if result["error"]:
        raise RuntimeError(f"PSBT signing failed: {result['error']}")
    return result


# ============================================================
# UI interaction helpers
# ============================================================

def setup_page(page, base_url):
    """Navigate to the Address Sweeper, wait for test hooks, set network to testnet."""
    page.goto(base_url)
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)
    page.select_option("#network", "testnet")
    # Clear default empty rows
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")


def fetch_utxos_for_address(page, address, min_utxo_count=1, prev_utxo_count=0):
    """Fetch UTXOs for an address via the UI and wait for completion."""
    page.fill("#fetchAddress", address)
    page.click("#fetchUtxosBtn")

    target_count = prev_utxo_count + min_utxo_count
    page.wait_for_function(
        f"() => document.querySelectorAll('[data-utxo]').length >= {target_count}"
        f" && document.getElementById('fetchStatus').textContent.includes('full transaction')",
        timeout=60000)

    return len(page.query_selector_all("[data-utxo]"))


def create_and_download_psbt(page, all_dialogs):
    """Click Create PSBT, then Download, return the PSBT binary bytes."""
    all_dialogs.clear()
    page.click("#createPsbt")
    page.wait_for_selector("#psbtResult", state="visible", timeout=30000)
    with page.expect_download(timeout=30000) as download_info:
        page.click("#downloadPsbt")
    dl = download_info.value

    # Only error alerts are fatal
    errors = [d for d in all_dialogs if "error" in d.lower()]
    if errors:
        raise RuntimeError(f"Error dialog(s) on create: {errors}")

    with open(dl.path(), "rb") as f:
        return f.read()


def upload_combine_finalize(page, signed_paths, all_dialogs):
    """Upload signed PSBT file(s), combine & finalize. Returns hex string."""
    all_dialogs.clear()
    page.set_input_files("#psbtFiles", signed_paths)
    page.click("#combinePsbt")
    page.wait_for_timeout(3000)

    if all_dialogs:
        raise RuntimeError(f"Dialog(s) on combine: {all_dialogs}")

    hex_str = (page.text_content("#combinedResult") or "").strip()
    return hex_str


def broadcast_via_ui(page, all_dialogs):
    """Click broadcast, return the TXID (or empty string on failure)."""
    all_dialogs.clear()
    page.click("#broadcastTx")
    page.wait_for_timeout(8000)  # testnet4 broadcast can be slow

    if all_dialogs:
        raise RuntimeError(f"Dialog(s) on broadcast: {all_dialogs}")

    bcast_text = page.text_content("#broadcastResult") or ""
    txid_match = re.search(r'[a-f0-9]{64}', bcast_text)
    return txid_match.group(0) if txid_match else ""


# ============================================================
# Recovery — sweep funds back from a failed test run
# ============================================================

def recover_funds():
    """Recover funds from wallet_a/wallet_b back to the main wallet."""
    if not os.path.exists(RECOVERY_FILE):
        print("ERROR: No recovery file found.")
        print(f"  Expected: {RECOVERY_FILE}")
        sys.exit(1)

    with open(RECOVERY_FILE) as f:
        data = json.load(f)

    main_address = data["main_address"]
    wallets = data["wallets"]

    print("=" * 60)
    print("Address Sweeper — Testnet4 RECOVERY MODE")
    print(f"  Returning funds to: {main_address[:20]}...{main_address[-8:]}")
    print("=" * 60)

    port = find_free_port()
    server_proc = start_server(port)
    base_url = f"http://127.0.0.1:{port}/index.html"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.add_init_script("window.__TEST_MODE__ = true;")

            for w in wallets:
                addr = w["address"]
                wif = w["wif"]
                print(f"\n  Checking {addr[:20]}...{addr[-8:]}")

                # Check balance via API
                try:
                    url = f"{MEMPOOL_API}/address/{addr}/utxo"
                    req = Request(url, headers={"User-Agent": "PSBTBuilder/1.0"})
                    resp = urlopen(req, timeout=15)
                    utxos = json.loads(resp.read().decode())
                except Exception as e:
                    print(f"    Error checking balance: {e}")
                    continue

                if not utxos:
                    print(f"    No UTXOs — already recovered or never funded")
                    continue

                total = sum(u["value"] for u in utxos)
                sweep_amount = total - RETURN_FEE_SATS
                if sweep_amount <= 0:
                    print(f"    Balance too low to sweep: {total} sats")
                    continue

                print(f"    Found {total:,} sats — sweeping {sweep_amount:,} back")

                # Load page, fetch UTXOs, create PSBT, sign, finalize, broadcast
                setup_page(page, base_url)
                preload_ecpair(page)

                fetch_utxos_for_address(page, addr)
                page.fill("#feeRate", "2")
                page.evaluate(f"""() => {{
                    window._fn.addOutput(null, "{main_address}", {sweep_amount});
                }}""")

                all_dialogs = []
                page.on("dialog", lambda d: (all_dialogs.append(d.message), d.accept()))

                psbt_bin = create_and_download_psbt(page, all_dialogs)
                psbt_b64 = base64.b64encode(psbt_bin).decode("ascii")
                sign_result = sign_psbt_in_browser(page, psbt_b64, wif)

                tmp = tempfile.mktemp(suffix=".psbt")
                with open(tmp, "wb") as f:
                    f.write(base64.b64decode(sign_result["psbt"]))

                upload_combine_finalize(page, [tmp], all_dialogs)
                txid = broadcast_via_ui(page, all_dialogs)
                os.unlink(tmp)

                if txid:
                    print(f"    Recovered! https://mempool.space/testnet4/tx/{txid}")
                else:
                    bcast = page.text_content("#broadcastResult") or ""
                    print(f"    Broadcast result: {bcast}")

            browser.close()

    except Exception as e:
        print(f"\nRECOVERY FAILED: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        stop_server(server_proc)

    os.remove(RECOVERY_FILE)
    print(f"\n{'='*60}")
    print("Recovery complete. File cleaned up.")
    print(f"{'='*60}")
    sys.exit(0)


# ============================================================
# Main test
# ============================================================

def run_tests(page, base_url, main_wif, main_address):

    # ========================================================
    section("1. Setup & Verify Main Wallet")
    # ========================================================

    setup_page(page, base_url)

    # Preload ECPair from CDN
    preload_ecpair(page)
    test("ECPair loaded from CDN", True)

    # Verify WIF matches the provided address
    verify = verify_wif_matches_address(page, main_wif, main_address)
    test("WIF matches main address", verify["matches"],
         f"WIF derives {verify['address']}, expected {main_address}")

    # Generate temp wallet keypairs (A+B for parallel, C+D for serial)
    wallet_a = generate_keypair(page)
    wallet_b = generate_keypair(page)
    wallet_c = generate_keypair(page)
    wallet_d = generate_keypair(page)

    test("wallet_a generated (tb1q)",
         wallet_a["address"].startswith("tb1q"),
         f"got {wallet_a['address']}")
    test("wallet_b generated (tb1q)",
         wallet_b["address"].startswith("tb1q"),
         f"got {wallet_b['address']}")
    test("wallet_c generated (tb1q)",
         wallet_c["address"].startswith("tb1q"),
         f"got {wallet_c['address']}")
    test("wallet_d generated (tb1q)",
         wallet_d["address"].startswith("tb1q"),
         f"got {wallet_d['address']}")

    print(f"\n  Main wallet: {main_address}")
    print(f"  Wallet A:    {wallet_a['address']}  ({FUND_A_SATS:,} sats) [parallel]")
    print(f"  Wallet B:    {wallet_b['address']}  ({FUND_B_SATS:,} sats) [parallel]")
    print(f"  Wallet C:    {wallet_c['address']}  ({FUND_C_SATS:,} sats) [serial]")
    print(f"  Wallet D:    {wallet_d['address']}  ({FUND_D_SATS:,} sats) [serial]")

    # Save recovery file immediately (in case test crashes after funding)
    recovery_data = {
        "main_address": main_address,
        "wallets": [
            {"wif": wallet_a["wif"], "address": wallet_a["address"]},
            {"wif": wallet_b["wif"], "address": wallet_b["address"]},
            {"wif": wallet_c["wif"], "address": wallet_c["address"]},
            {"wif": wallet_d["wif"], "address": wallet_d["address"]},
        ],
        "note": "Run with --recover to sweep funds back to main wallet",
    }
    with open(RECOVERY_FILE, "w") as f:
        json.dump(recovery_data, f, indent=2)
    print(f"  Recovery file saved: {RECOVERY_FILE}")

    # ========================================================
    section("2. Fund Temp Wallets (main -> A + B + C + D)")
    # ========================================================

    # Fetch main wallet UTXOs
    fetch_utxos_for_address(page, main_address)
    utxo_count = len(page.query_selector_all("[data-utxo]"))
    test("main wallet UTXOs fetched", utxo_count >= 1,
         f"got {utxo_count} UTXOs")

    # Fee rate 2 sat/vB, add outputs to all 4 wallets + wipe back to main
    page.fill("#feeRate", "2")

    page.evaluate(f"""() => {{
        window._fn.addOutput(null, "{wallet_a['address']}", {FUND_A_SATS});
        window._fn.addOutput(null, "{wallet_b['address']}", {FUND_B_SATS});
        window._fn.addOutput(null, "{wallet_c['address']}", {FUND_C_SATS});
        window._fn.addOutput(null, "{wallet_d['address']}", {FUND_D_SATS});
        window._fn.addOutput(null, "{main_address}", 0);
    }}""")
    # Enable wipe on the last output (change back to main)
    page.locator("[data-output]:last-child .output-wipe").check()

    page.wait_for_timeout(500)  # let fee calc update

    # Dialog handler
    all_dialogs = []
    page.on("dialog", lambda d: (all_dialogs.append(d.message), d.accept()))

    # Create & download PSBT
    fund_psbt_bin = create_and_download_psbt(page, all_dialogs)
    test("funding: PSBT created", fund_psbt_bin[:5] == b"psbt\xff")

    # Sign all inputs with main wallet WIF
    fund_psbt_b64 = base64.b64encode(fund_psbt_bin).decode("ascii")
    sign_result = sign_psbt_in_browser(page, fund_psbt_b64, main_wif)
    test("funding: main wallet signed",
         sign_result["signed"] > 0,
         f"signed {sign_result['signed']} inputs")

    # Write signed PSBT, upload, finalize, broadcast
    tmp_dir = tempfile.mkdtemp(prefix="psbt_testnet4_")
    fund_signed_path = os.path.join(tmp_dir, "fund_signed.psbt")
    with open(fund_signed_path, "wb") as f_out:
        f_out.write(base64.b64decode(sign_result["psbt"]))

    fund_hex = upload_combine_finalize(page, [fund_signed_path], all_dialogs)
    test("funding: finalized hex present",
         len(fund_hex) > 0 and re.match(r'^[0-9a-fA-F]+$', fund_hex) is not None,
         f"length={len(fund_hex)}")

    fund_txid = broadcast_via_ui(page, all_dialogs)
    test("funding: broadcast TXID valid",
         len(fund_txid) == 64,
         f"got: {fund_txid or '(empty)'}")

    if fund_txid:
        print(f"\n  Funding TX: https://mempool.space/testnet4/tx/{fund_txid}")

    if not fund_txid:
        print("\n  FATAL: Funding transaction failed. Cannot continue.")
        print("  Run with --recover to return any funds that were sent.")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    # Wait for mempool propagation
    print(f"\n  Waiting {MEMPOOL_PROPAGATION_WAIT}s for mempool propagation...")
    time.sleep(MEMPOOL_PROPAGATION_WAIT)

    # ================================================================
    #  PART A — Parallel Signing (A + B)
    # ================================================================

    # ========================================================
    section("3. Fetch Temp Wallet UTXOs via UI")
    # ========================================================

    # Reload for a fresh state
    setup_page(page, base_url)

    # Fetch wallet_a UTXOs
    count_a = fetch_utxos_for_address(page, wallet_a["address"])
    test("wallet_a UTXOs fetched", count_a >= 1,
         f"got {count_a} UTXO rows")

    # Fetch wallet_b UTXOs (additive)
    count_ab = fetch_utxos_for_address(
        page, wallet_b["address"], min_utxo_count=1, prev_utxo_count=count_a)
    test("wallet_b UTXOs fetched", count_ab >= 2,
         f"got {count_ab} UTXO rows total")

    # Verify values
    values = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('[data-utxo]')).map(r => {
            const inputs = r.querySelectorAll('input');
            return parseInt(inputs[2].value);
        });
    }""")
    test("UTXO values match funded amounts",
         sorted(values) == sorted([FUND_A_SATS, FUND_B_SATS]),
         f"got {sorted(values)}, expected {sorted([FUND_A_SATS, FUND_B_SATS])}")

    # ========================================================
    section("4. Create Return PSBT via UI")
    # ========================================================

    page.fill("#feeRate", "2")

    # Output: return all funds to main wallet (minus fee)
    page.evaluate(f"""() => {{
        window._fn.addOutput(null, "{main_address}", {PARALLEL_RETURN_SATS});
    }}""")

    # Re-register dialog handler (page was reloaded)
    all_dialogs = []
    page.on("dialog", lambda d: (all_dialogs.append(d.message), d.accept()))

    return_psbt_bin = create_and_download_psbt(page, all_dialogs)
    test("return: PSBT created", return_psbt_bin[:5] == b"psbt\xff")

    # ========================================================
    section("5. Sign Return PSBT (Parallel — A and B)")
    # ========================================================

    return_psbt_b64 = base64.b64encode(return_psbt_bin).decode("ascii")

    # Sign with wallet_a (signs only its input, skips wallet_b's)
    result_a = sign_psbt_in_browser(page, return_psbt_b64, wallet_a["wif"])
    test("return: wallet_a signed",
         result_a["signed"] >= 1,
         f"signed {result_a['signed']} inputs")

    # Sign with wallet_b (signs only its input, skips wallet_a's)
    result_b = sign_psbt_in_browser(page, return_psbt_b64, wallet_b["wif"])
    test("return: wallet_b signed",
         result_b["signed"] >= 1,
         f"signed {result_b['signed']} inputs")

    # Write both signed PSBTs to temp files
    signed_a_path = os.path.join(tmp_dir, "return_signed_a.psbt")
    signed_b_path = os.path.join(tmp_dir, "return_signed_b.psbt")
    with open(signed_a_path, "wb") as f_out:
        f_out.write(base64.b64decode(result_a["psbt"]))
    with open(signed_b_path, "wb") as f_out:
        f_out.write(base64.b64decode(result_b["psbt"]))

    # ========================================================
    section("6. Combine, Finalize & Broadcast Return TX")
    # ========================================================

    return_hex = upload_combine_finalize(
        page, [signed_a_path, signed_b_path], all_dialogs)
    test("return: finalized hex present",
         len(return_hex) > 0 and re.match(r'^[0-9a-fA-F]+$', return_hex) is not None,
         f"length={len(return_hex)}")

    return_txid = broadcast_via_ui(page, all_dialogs)
    test("return: broadcast TXID valid",
         len(return_txid) == 64,
         f"got: {return_txid or '(empty)'}")

    # ================================================================
    #  PART B — Serial Signing (C → D)
    # ================================================================

    # ========================================================
    section("7. Fetch Serial Wallet UTXOs via UI")
    # ========================================================

    # Reload for a fresh state
    setup_page(page, base_url)

    # Fetch wallet_c UTXOs
    count_c = fetch_utxos_for_address(page, wallet_c["address"])
    test("wallet_c UTXOs fetched", count_c >= 1,
         f"got {count_c} UTXO rows")

    # Fetch wallet_d UTXOs (additive)
    count_cd = fetch_utxos_for_address(
        page, wallet_d["address"], min_utxo_count=1, prev_utxo_count=count_c)
    test("wallet_d UTXOs fetched", count_cd >= 2,
         f"got {count_cd} UTXO rows total")

    # Verify values
    serial_values = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('[data-utxo]')).map(r => {
            const inputs = r.querySelectorAll('input');
            return parseInt(inputs[2].value);
        });
    }""")
    test("serial: UTXO values match funded amounts",
         sorted(serial_values) == sorted([FUND_C_SATS, FUND_D_SATS]),
         f"got {sorted(serial_values)}, expected {sorted([FUND_C_SATS, FUND_D_SATS])}")

    # ========================================================
    section("8. Create Serial Return PSBT via UI")
    # ========================================================

    page.fill("#feeRate", "2")

    # Output: return all funds to main wallet (minus fee)
    page.evaluate(f"""() => {{
        window._fn.addOutput(null, "{main_address}", {SERIAL_RETURN_SATS});
    }}""")

    # Re-register dialog handler (page was reloaded)
    all_dialogs = []
    page.on("dialog", lambda d: (all_dialogs.append(d.message), d.accept()))

    serial_psbt_bin = create_and_download_psbt(page, all_dialogs)
    test("serial: PSBT created", serial_psbt_bin[:5] == b"psbt\xff")

    # ========================================================
    section("9. Sign Serially (C -> D)")
    # ========================================================

    serial_psbt_b64 = base64.b64encode(serial_psbt_bin).decode("ascii")

    # Step 1: wallet_c signs first (only its input, skips wallet_d's)
    result_c = sign_psbt_in_browser(page, serial_psbt_b64, wallet_c["wif"])
    test("serial: wallet_c signed",
         result_c["signed"] >= 1,
         f"signed {result_c['signed']} inputs")

    # Step 2: wallet_d signs wallet_c's partially-signed PSBT
    # (adds its signature — now both inputs are signed)
    result_d = sign_psbt_in_browser(page, result_c["psbt"], wallet_d["wif"])
    test("serial: wallet_d signed",
         result_d["signed"] >= 1,
         f"signed {result_d['signed']} inputs")

    # Write single fully-signed (but not finalized) PSBT
    serial_signed_path = os.path.join(tmp_dir, "serial_signed.psbt")
    with open(serial_signed_path, "wb") as f_out:
        f_out.write(base64.b64decode(result_d["psbt"]))

    # ========================================================
    section("10. Finalize & Broadcast Serial Return TX")
    # ========================================================

    serial_hex = upload_combine_finalize(page, [serial_signed_path], all_dialogs)
    test("serial: finalized hex present",
         len(serial_hex) > 0 and re.match(r'^[0-9a-fA-F]+$', serial_hex) is not None,
         f"length={len(serial_hex)}")

    serial_txid = broadcast_via_ui(page, all_dialogs)
    test("serial: broadcast TXID valid",
         len(serial_txid) == 64,
         f"got: {serial_txid or '(empty)'}")

    if serial_txid:
        print(f"\n  Serial Return TX: https://mempool.space/testnet4/tx/{serial_txid}")

    # ========================================================
    section("11. Summary")
    # ========================================================

    if fund_txid:
        print(f"  Fund TX:            https://mempool.space/testnet4/tx/{fund_txid}")
    if return_txid:
        print(f"  Parallel Return TX: https://mempool.space/testnet4/tx/{return_txid}")
    if serial_txid:
        print(f"  Serial Return TX:   https://mempool.space/testnet4/tx/{serial_txid}")

    print(f"\n  --- Parallel (A + B) ---")
    print(f"  Wallet A funded:    {FUND_A_SATS:>10,} sats")
    print(f"  Wallet B funded:    {FUND_B_SATS:>10,} sats")
    print(f"  Returned to main:   {PARALLEL_RETURN_SATS:>10,} sats")
    print(f"  Fee:                {RETURN_FEE_SATS:>10,} sats")

    print(f"\n  --- Serial (C -> D) ---")
    print(f"  Wallet C funded:    {FUND_C_SATS:>10,} sats")
    print(f"  Wallet D funded:    {FUND_D_SATS:>10,} sats")
    print(f"  Returned to main:   {SERIAL_RETURN_SATS:>10,} sats")
    print(f"  Fee:                {RETURN_FEE_SATS:>10,} sats")

    # Clean up
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # Remove recovery file — funds are safely back
    if return_txid and serial_txid and os.path.exists(RECOVERY_FILE):
        os.remove(RECOVERY_FILE)
        print("\n  Recovery file cleaned up (all funds returned)")
    elif os.path.exists(RECOVERY_FILE):
        print("\n  WARNING: Not all funds returned. Recovery file kept.")
        print(f"  Run: python3 tests/test_testnet4_e2e.py --recover")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Testnet4 E2E test for Bitcoin Address Sweeper")
    parser.add_argument("--wif",
                        default=os.environ.get("TESTNET4_WIF"),
                        help="WIF for the pre-funded testnet4 address")
    parser.add_argument("--address",
                        default=os.environ.get("TESTNET4_ADDRESS"),
                        help="Pre-funded testnet4 address")
    parser.add_argument("--headed", action="store_true",
                        help="Run browser in visible mode")
    parser.add_argument("--recover", action="store_true",
                        help="Recover funds from a failed test run")
    args = parser.parse_args()

    # Recovery mode
    if args.recover:
        recover_funds()
        return

    # Load credentials: CLI args > env vars > settings.json
    main_wif = args.wif
    main_address = args.address

    if not main_wif or not main_address:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE) as f:
                settings = json.load(f)
            main_wif = main_wif or settings.get("TESTNET4_WIF")
            main_address = main_address or settings.get("TESTNET4_ADDRESS")

    if not main_wif or not main_address:
        print("ERROR: Testnet4 wallet credentials required.")
        print("  Option 1: settings.json with TESTNET4_WIF and TESTNET4_ADDRESS")
        print("  Option 2: --wif and --address CLI arguments")
        print("  Option 3: TESTNET4_WIF and TESTNET4_ADDRESS env vars")
        sys.exit(1)

    if not main_address.startswith("tb1q"):
        print(f"ERROR: Expected tb1q... SegWit address, got: {main_address}")
        sys.exit(1)

    # Check for leftover recovery file
    if os.path.exists(RECOVERY_FILE):
        print("WARNING: Recovery file exists from a previous failed run.")
        print(f"  File: {RECOVERY_FILE}")
        print("  Run with --recover to return those funds first,")
        print("  or delete the file if already handled.")
        sys.exit(1)

    # Pre-flight balance check
    preflight_balance_check(main_address)

    print("=" * 60)
    print("Bitcoin Address Sweeper — Testnet4 E2E Test")
    print(f"  Main wallet: {main_address[:20]}...{main_address[-8:]}")
    print(f"  Mode: {'headed' if args.headed else 'headless'}")
    print("=" * 60)

    port = find_free_port()
    server_proc = None

    try:
        print(f"\n--- Starting static server on port {port} ---")
        server_proc = start_server(port)
        base_url = f"http://127.0.0.1:{port}/index.html"
        print(f"  Server ready at {base_url}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.add_init_script("window.__TEST_MODE__ = true;")

            run_tests(page, base_url, main_wif, main_address)

            browser.close()

    except Exception:
        traceback.print_exc()
        if os.path.exists(RECOVERY_FILE):
            print(f"\n  Funds may be at temp wallets.")
            print(f"  Run: python3 tests/test_testnet4_e2e.py --recover")
    finally:
        if server_proc:
            stop_server(server_proc)

    # Summary
    print(f"\n{'='*60}")
    print(f"  RESULTS: {_pass_count} passed, {_fail_count} failed")
    print(f"{'='*60}")
    if _failures:
        print("\n  Failed tests:")
        for f in _failures:
            print(f"    \u2717 {f}")
    print()

    sys.exit(1 if _fail_count > 0 else 0)


if __name__ == "__main__":
    main()
