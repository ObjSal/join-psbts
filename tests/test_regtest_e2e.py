#!/usr/bin/env python3
"""
End-to-end regtest test for Bitcoin Address Sweeper.

Creates an isolated bitcoind regtest instance via server/server.py,
funds two wallets, builds a multi-input PSBT through the web UI,
signs with bitcoin-cli, combines/finalizes via UI, broadcasts via UI,
and verifies the transaction is confirmed on-chain.

Requires:
  - Bitcoin Core (bitcoind + bitcoin-cli) in PATH
  - Python Playwright: pip install playwright && playwright install chromium

Usage:
    python3 tests/test_regtest_e2e.py              # headless
    python3 tests/test_regtest_e2e.py --headed      # visible browser
"""

import base64
import json
import os
import re
import shutil
import signal
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
HEADED = "--headed" in sys.argv
SERVER_READY_TIMEOUT = 90  # seconds (bitcoind mines 101 blocks at startup)

# Test amounts
FUND_AMOUNT_A = "1.0"         # BTC
FUND_AMOUNT_B = "0.5"         # BTC
FUND_SATS_A = 100_000_000     # sats
FUND_SATS_B = 50_000_000      # sats
SEND_SATS = 149_900_000       # sats to recipient
FEE_SATS = 100_000            # implied fee (inputs - output)


# ============================================================
# Test infrastructure (same pattern as test_psbt_builder.py)
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
# Server lifecycle
# ============================================================

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port):
    """Start server.py --regtest as a subprocess, wait for /api/health."""
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_PROJECT_ROOT, "server", "server.py"),
         str(port), "--regtest"],
        cwd=_PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    for i in range(SERVER_READY_TIMEOUT):
        try:
            resp = urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "ok" and data.get("regtest"):
                return proc, data
        except (URLError, ConnectionRefusedError, OSError):
            pass
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise RuntimeError(
                f"Server exited prematurely (rc={proc.returncode})\n{stderr}")
        time.sleep(1)
    proc.kill()
    raise RuntimeError(
        f"Server failed to become ready within {SERVER_READY_TIMEOUT}s")


def stop_server(proc):
    """Gracefully stop server and its child bitcoind process."""
    if not proc or proc.poll() is not None:
        return

    pgid = os.getpgid(proc.pid)

    # 1. SIGINT -> triggers KeyboardInterrupt -> clean shutdown
    try:
        os.kill(proc.pid, signal.SIGINT)
        proc.wait(timeout=20)
        print("  Server stopped gracefully.")
        return
    except (subprocess.TimeoutExpired, OSError):
        pass

    # 2. SIGTERM process group
    try:
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=10)
        print("  Server process group terminated.")
        return
    except (subprocess.TimeoutExpired, OSError):
        pass

    # 3. Force kill
    try:
        os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=5)
        print("  Server process group force-killed.")
    except (OSError, subprocess.TimeoutExpired):
        pass


# ============================================================
# bitcoin-cli helper
# ============================================================

class BitcoinCLI:
    """Wrapper for bitcoin-cli commands against the server's regtest node."""

    def __init__(self, datadir, rpc_port):
        self.base_cmd = [
            "bitcoin-cli",
            f"-datadir={datadir}",
            "-regtest",
            f"-rpcport={rpc_port}",
            "-rpcuser=test",
            "-rpcpassword=test",
        ]

    def run(self, *args, wallet=None):
        cmd = list(self.base_cmd)
        if wallet:
            cmd.append(f"-rpcwallet={wallet}")
        cmd.extend(args)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"bitcoin-cli {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def run_json(self, *args, wallet=None):
        return json.loads(self.run(*args, wallet=wallet))


# ============================================================
# HTTP helper for server API calls
# ============================================================

def api_post(base_url, path, data):
    """POST JSON to the server API."""
    body = json.dumps(data).encode("utf-8")
    req = Request(f"{base_url}{path}", data=body,
                  headers={"Content-Type": "application/json"}, method="POST")
    resp = urlopen(req, timeout=30)
    return json.loads(resp.read().decode("utf-8"))


# ============================================================
# Tests
# ============================================================

def clear_tip(page):
    """Deselect tip presets and clear tip sats to avoid outputs > inputs."""
    page.evaluate("""() => {
        document.querySelectorAll('.tip-preset').forEach(p => p.classList.remove('active'));
        document.getElementById('tipSats').value = '0';
        if (window._fn && window._fn.updateTipSummary) window._fn.updateTipSummary();
    }""")


def run_tests(page, base_url, cli, server_url):
    """Run the full E2E regtest test flow."""

    # ========================================================
    section("1. Regtest Node Setup")
    # ========================================================

    health = json.loads(urlopen(f"{server_url}/api/health", timeout=5).read())
    test("health status ok", health.get("status") == "ok")
    test("regtest is active", health.get("regtest") is True)

    # Create test wallets
    for name in ["wallet_a", "wallet_b", "wallet_recipient"]:
        try:
            cli.run("createwallet", name)
        except RuntimeError as e:
            if "already exists" not in str(e):
                raise

    addr_a = cli.run("getnewaddress", "", "bech32", wallet="wallet_a")
    addr_b = cli.run("getnewaddress", "", "bech32", wallet="wallet_b")
    addr_recipient = cli.run("getnewaddress", "", "bech32", wallet="wallet_recipient")

    test("wallet_a address valid", addr_a.startswith("bcrt1q"), f"got {addr_a}")
    test("wallet_b address valid", addr_b.startswith("bcrt1q"), f"got {addr_b}")
    test("recipient address valid", addr_recipient.startswith("bcrt1q"), f"got {addr_recipient}")

    # ========================================================
    section("2. Fund Wallets")
    # ========================================================

    result_a = api_post(server_url, "/api/faucet",
                        {"address": addr_a, "amount": FUND_AMOUNT_A})
    test("faucet funded wallet_a", result_a.get("success") is True,
         f"got {result_a}")
    txid_a = result_a.get("txid", "")
    test("wallet_a txid valid", len(txid_a) == 64 and all(c in "0123456789abcdef" for c in txid_a),
         f"got {txid_a}")

    result_b = api_post(server_url, "/api/faucet",
                        {"address": addr_b, "amount": FUND_AMOUNT_B})
    test("faucet funded wallet_b", result_b.get("success") is True,
         f"got {result_b}")
    txid_b = result_b.get("txid", "")
    test("wallet_b txid valid", len(txid_b) == 64 and all(c in "0123456789abcdef" for c in txid_b),
         f"got {txid_b}")

    # ========================================================
    section("3. Fetch UTXOs via UI")
    # ========================================================

    # Navigate and wait for test hook
    page.goto(base_url)
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)

    # Wait for serverMode detection to complete (async init)
    page.wait_for_function("() => window._fn.serverMode === true", timeout=10000)
    test("serverMode is true", page.evaluate("() => window._fn.serverMode") is True)

    # Set network to regtest
    page.select_option("#network", "regtest")
    test("network set to regtest", page.evaluate(
        "() => document.getElementById('network').value") == "regtest")

    # Clear default rows
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

    # Fetch UTXOs for wallet_a
    page.fill("#fetchAddress", addr_a)
    page.click("#fetchUtxosBtn")
    page.wait_for_function(
        "() => document.getElementById('fetchStatus').textContent.includes('Added')",
        timeout=15000)
    status_text = page.text_content("#fetchStatus")
    test("wallet_a UTXOs fetched", "Added 1 UTXO" in status_text, f"got: {status_text}")

    # Check the UTXO value in the input row
    utxo_rows = page.query_selector_all("[data-utxo]")
    test("1 input row after wallet_a fetch", len(utxo_rows) == 1,
         f"got {len(utxo_rows)}")

    # Fetch UTXOs for wallet_b
    page.fill("#fetchAddress", addr_b)
    page.click("#fetchUtxosBtn")
    # Wait for second batch
    page.wait_for_function(
        f"() => document.querySelectorAll('[data-utxo]').length === 2",
        timeout=15000)

    utxo_rows = page.query_selector_all("[data-utxo]")
    test("2 input rows after wallet_b fetch", len(utxo_rows) == 2,
         f"got {len(utxo_rows)}")

    # Verify values in the input rows
    values = page.evaluate("""() => {
        const rows = document.querySelectorAll('[data-utxo]');
        return Array.from(rows).map(r => {
            const inputs = r.querySelectorAll('input');
            return parseInt(inputs[2].value);
        });
    }""")
    values_sorted = sorted(values)
    test("UTXO values correct", values_sorted == [FUND_SATS_B, FUND_SATS_A],
         f"got {values_sorted}")

    # ========================================================
    section("4. Create PSBT via UI")
    # ========================================================

    page.fill("#feeRate", "1")

    # Add output to recipient
    page.evaluate(f"""() => {{
        window._fn.addOutput(null, "{addr_recipient}", {SEND_SATS});
    }}""")

    # Verify fee display
    page.wait_for_timeout(500)
    fee_text = page.text_content("#feeCalc")
    test("fee calc shows fee", "Estimated fee" in (fee_text or ""), f"got: {fee_text}")

    # Set up a single dialog handler for the entire test
    all_dialogs = []
    page.on("dialog", lambda d: (all_dialogs.append(d.message), d.accept()))

    # Click Create & Download and expect a file download
    clear_tip(page)
    all_dialogs.clear()
    page.click("#createPsbt")
    page.wait_for_selector("#psbtResult", state="visible", timeout=30000)
    with page.expect_download(timeout=30000) as download_info:
        page.click("#downloadPsbt")
    download = download_info.value

    test("no unexpected error on create",
         all(("error" not in d.lower()) for d in all_dialogs),
         f"got: {all_dialogs}")
    test("PSBT download triggered", download is not None)
    test("PSBT filename is unsigned.psbt",
         download.suggested_filename == "unsigned.psbt")

    # Verify PSBT file
    psbt_path = download.path()
    with open(psbt_path, "rb") as f:
        psbt_binary = f.read()
    test("PSBT file non-empty", len(psbt_binary) > 0, f"size={len(psbt_binary)}")
    test("PSBT has magic header", psbt_binary[:5] == b"psbt\xff",
         f"got {psbt_binary[:5]}")

    # ========================================================
    section("5. Sign PSBT with bitcoin-cli")
    # ========================================================

    psbt_base64 = base64.b64encode(psbt_binary).decode("ascii")
    test("PSBT base64 non-empty", len(psbt_base64) > 0)

    # Sign with wallet_a (finalize=false to keep as partial signatures)
    result_a = cli.run_json("walletprocesspsbt", psbt_base64,
                            "true", "DEFAULT", "true", "false",
                            wallet="wallet_a")
    signed_a_base64 = result_a["psbt"]
    test("wallet_a signed PSBT", len(signed_a_base64) > 0)
    test("wallet_a signing partial (not complete)",
         result_a.get("complete") is False,
         f"complete={result_a.get('complete')}")

    # Sign with wallet_b (finalize=false to keep as partial signatures)
    result_b = cli.run_json("walletprocesspsbt", psbt_base64,
                            "true", "DEFAULT", "true", "false",
                            wallet="wallet_b")
    signed_b_base64 = result_b["psbt"]
    test("wallet_b signed PSBT", len(signed_b_base64) > 0)
    test("wallet_b signing partial (not complete)",
         result_b.get("complete") is False,
         f"complete={result_b.get('complete')}")

    # Write signed PSBTs to temp files for UI upload
    tmp_dir = tempfile.mkdtemp(prefix="psbt_test_")
    signed_a_path = os.path.join(tmp_dir, "signed_a.psbt")
    signed_b_path = os.path.join(tmp_dir, "signed_b.psbt")
    with open(signed_a_path, "wb") as f:
        f.write(base64.b64decode(signed_a_base64))
    with open(signed_b_path, "wb") as f:
        f.write(base64.b64decode(signed_b_base64))
    test("signed PSBT files written", os.path.exists(signed_a_path) and
         os.path.exists(signed_b_path))

    # ========================================================
    section("6. Combine & Finalize via UI")
    # ========================================================

    # Navigate to Sign card
    page.evaluate("() => window._fn.showCard('cardBroadcast')")
    # Upload signed PSBTs
    all_dialogs.clear()
    page.set_input_files("#psbtFiles", [signed_a_path, signed_b_path])

    # Click Combine & Finalize
    page.click("#combinePsbt")
    page.wait_for_timeout(3000)

    test("no error dialog on combine", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    # Verify combined result (check DOM content, not module-scoped variable)
    combined_hex_ui = (page.text_content("#combinedResult") or "").strip()
    test("combined result has hex",
         len(combined_hex_ui) > 0 and re.match(r'^[0-9a-fA-F]+$', combined_hex_ui) is not None,
         f"length={len(combined_hex_ui)}, preview={combined_hex_ui[:40]}...")

    # ========================================================
    section("7. Broadcast via UI")
    # ========================================================

    all_dialogs.clear()
    page.click("#broadcastTx")
    page.wait_for_timeout(3000)

    test("no error dialog on broadcast", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    broadcast_text = page.text_content("#broadcastResult")
    test("broadcast shows TXID", broadcast_text is not None and
         "Broadcasted TXID:" in broadcast_text,
         f"got: {broadcast_text}")

    txid_match = re.search(r'[a-f0-9]{64}', broadcast_text or "")
    test("broadcast TXID is valid hex", txid_match is not None)
    broadcast_txid = txid_match.group(0) if txid_match else ""

    # ========================================================
    section("8. On-chain Verification")
    # ========================================================

    if not broadcast_txid:
        test("SKIP: no broadcast txid for on-chain verification", False,
             "broadcast failed, cannot verify")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    # The broadcast handler auto-mines 1 block, but mine another to be safe
    try:
        api_post(server_url, "/api/mine", {"blocks": 1})
    except Exception:
        pass

    # Verify transaction is confirmed
    decoded = cli.run_json("getrawtransaction", broadcast_txid, "true")
    confirmations = decoded.get("confirmations", 0)
    test("transaction confirmed", confirmations >= 1,
         f"confirmations={confirmations}")

    # Verify structure
    test("transaction has 2 inputs", len(decoded.get("vin", [])) == 2,
         f"got {len(decoded.get('vin', []))}")
    test("transaction has 1 output", len(decoded.get("vout", [])) == 1,
         f"got {len(decoded.get('vout', []))}")

    # Verify output amount
    output_sats = round(decoded["vout"][0]["value"] * 1e8)
    test("output amount correct", output_sats == SEND_SATS,
         f"expected {SEND_SATS}, got {output_sats}")

    # Verify recipient wallet received funds
    # Import the address first so getbalance reflects it
    recipient_balance = float(cli.run("getbalance", wallet="wallet_recipient"))
    test("recipient balance > 0", recipient_balance > 0,
         f"balance={recipient_balance}")

    # Clean up temp files
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # ================================================================
    #  PART B — Serial Signing  (A signs → passes to B → single file)
    # ================================================================

    # ========================================================
    section("9. Fund Wallets (Serial Test)")
    # ========================================================

    SERIAL_FUND_A = "0.8"
    SERIAL_FUND_B = "0.3"
    SERIAL_SATS_A = 80_000_000
    SERIAL_SATS_B = 30_000_000
    SERIAL_SEND = 109_900_000       # 0.8 + 0.3 BTC minus 0.001 BTC fee

    addr_a2 = cli.run("getnewaddress", "", "bech32", wallet="wallet_a")
    addr_b2 = cli.run("getnewaddress", "", "bech32", wallet="wallet_b")
    addr_recip2 = cli.run("getnewaddress", "", "bech32", wallet="wallet_recipient")

    res_a2 = api_post(server_url, "/api/faucet",
                      {"address": addr_a2, "amount": SERIAL_FUND_A})
    test("serial: faucet funded wallet_a", res_a2.get("success") is True)

    res_b2 = api_post(server_url, "/api/faucet",
                      {"address": addr_b2, "amount": SERIAL_FUND_B})
    test("serial: faucet funded wallet_b", res_b2.get("success") is True)

    # ========================================================
    section("10. Fetch UTXOs & Create PSBT (Serial)")
    # ========================================================

    # Reload page for a fresh state
    page.goto(base_url)
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)
    page.wait_for_function("() => window._fn.serverMode === true", timeout=10000)
    page.select_option("#network", "regtest")

    # Clear default rows
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

    # Fetch UTXOs for both wallets
    page.fill("#fetchAddress", addr_a2)
    page.click("#fetchUtxosBtn")
    page.wait_for_function(
        "() => document.getElementById('fetchStatus').textContent.includes('Added')",
        timeout=15000)

    page.fill("#fetchAddress", addr_b2)
    page.click("#fetchUtxosBtn")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-utxo]').length === 2",
        timeout=15000)

    test("serial: 2 input rows fetched",
         len(page.query_selector_all("[data-utxo]")) == 2)

    # Add output and create PSBT
    page.fill("#feeRate", "1")
    page.evaluate(f"""() => {{
        window._fn.addOutput(null, "{addr_recip2}", {SERIAL_SEND});
    }}""")

    # Re-register dialog handler (page was reloaded)
    all_dialogs = []
    page.on("dialog", lambda d: (all_dialogs.append(d.message), d.accept()))

    clear_tip(page)
    all_dialogs.clear()
    page.click("#createPsbt")
    page.wait_for_selector("#psbtResult", state="visible", timeout=30000)
    with page.expect_download(timeout=30000) as download_info:
        page.click("#downloadPsbt")
    dl2 = download_info.value

    test("serial: no unexpected error on create",
         all(("error" not in d.lower()) for d in all_dialogs),
         f"got: {all_dialogs}")
    test("serial: PSBT downloaded", dl2 is not None)

    with open(dl2.path(), "rb") as f:
        psbt_bin2 = f.read()
    test("serial: PSBT has magic header", psbt_bin2[:5] == b"psbt\xff")

    # ========================================================
    section("11. Sign Serially (A → B)")
    # ========================================================

    psbt_b64_2 = base64.b64encode(psbt_bin2).decode("ascii")

    # Step 1: wallet_a signs → partial (only its input)
    after_a = cli.run_json("walletprocesspsbt", psbt_b64_2,
                           "true", "DEFAULT", "true", "false",
                           wallet="wallet_a")
    test("serial: wallet_a signed (partial)",
         after_a.get("complete") is False,
         f"complete={after_a.get('complete')}")

    # Step 2: wallet_b signs wallet_a's output → both sigs present
    # Note: complete=False is expected because finalize=false was requested;
    # the PSBT has all partial sigs but hasn't been finalized yet.
    after_b = cli.run_json("walletprocesspsbt", after_a["psbt"],
                           "true", "DEFAULT", "true", "false",
                           wallet="wallet_b")
    test("serial: wallet_b signed", len(after_b.get("psbt", "")) > 0)

    # Write the single fully-signed (but not finalized) file
    tmp_dir2 = tempfile.mkdtemp(prefix="psbt_serial_")
    serial_path = os.path.join(tmp_dir2, "serial_signed.psbt")
    with open(serial_path, "wb") as f:
        f.write(base64.b64decode(after_b["psbt"]))
    test("serial: signed file written", os.path.exists(serial_path))

    # ========================================================
    section("12. Finalize via UI (Single File)")
    # ========================================================

    page.evaluate("() => window._fn.showCard('cardBroadcast')")
    all_dialogs.clear()
    page.set_input_files("#psbtFiles", [serial_path])
    page.click("#combinePsbt")
    page.wait_for_timeout(3000)

    test("serial: no error on finalize", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    serial_hex = (page.text_content("#combinedResult") or "").strip()
    test("serial: finalized hex present",
         len(serial_hex) > 0 and
         re.match(r'^[0-9a-fA-F]+$', serial_hex) is not None,
         f"length={len(serial_hex)}")

    # ========================================================
    section("13. Broadcast & Verify (Serial)")
    # ========================================================

    all_dialogs.clear()
    page.click("#broadcastTx")
    page.wait_for_timeout(3000)

    test("serial: no error on broadcast", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    serial_bcast = page.text_content("#broadcastResult")
    test("serial: broadcast shows TXID",
         serial_bcast is not None and "Broadcasted TXID:" in serial_bcast,
         f"got: {serial_bcast}")

    serial_txid_match = re.search(r'[a-f0-9]{64}', serial_bcast or "")
    test("serial: broadcast TXID valid", serial_txid_match is not None)
    serial_txid = serial_txid_match.group(0) if serial_txid_match else ""

    if not serial_txid:
        test("SKIP: serial on-chain verification", False,
             "broadcast failed")
        shutil.rmtree(tmp_dir2, ignore_errors=True)
        return

    try:
        api_post(server_url, "/api/mine", {"blocks": 1})
    except Exception:
        pass

    decoded2 = cli.run_json("getrawtransaction", serial_txid, "true")
    test("serial: transaction confirmed",
         decoded2.get("confirmations", 0) >= 1,
         f"confirmations={decoded2.get('confirmations', 0)}")
    test("serial: 2 inputs", len(decoded2.get("vin", [])) == 2,
         f"got {len(decoded2.get('vin', []))}")
    test("serial: 1 output", len(decoded2.get("vout", [])) == 1,
         f"got {len(decoded2.get('vout', []))}")
    test("serial: output amount correct",
         round(decoded2["vout"][0]["value"] * 1e8) == SERIAL_SEND,
         f"expected {SERIAL_SEND}, got {round(decoded2['vout'][0]['value'] * 1e8)}")

    shutil.rmtree(tmp_dir2, ignore_errors=True)

    # ================================================================
    #  PART C — Taproot (P2TR) Parallel Signing
    # ================================================================

    # ========================================================
    section("14. Fund Taproot Wallets (Parallel)")
    # ========================================================

    TR_FUND_A = "0.6"
    TR_FUND_B = "0.4"
    TR_SATS_A = 60_000_000
    TR_SATS_B = 40_000_000
    TR_SEND = 99_900_000       # 0.6 + 0.4 BTC minus 0.001 BTC fee

    addr_tr_a = cli.run("getnewaddress", "", "bech32m", wallet="wallet_a")
    addr_tr_b = cli.run("getnewaddress", "", "bech32m", wallet="wallet_b")
    addr_tr_recip = cli.run("getnewaddress", "", "bech32m", wallet="wallet_recipient")

    test("taproot addr_a valid (bcrt1p)", addr_tr_a.startswith("bcrt1p"),
         f"got {addr_tr_a}")
    test("taproot addr_b valid (bcrt1p)", addr_tr_b.startswith("bcrt1p"),
         f"got {addr_tr_b}")
    test("taproot recipient valid (bcrt1p)", addr_tr_recip.startswith("bcrt1p"),
         f"got {addr_tr_recip}")

    res_tr_a = api_post(server_url, "/api/faucet",
                        {"address": addr_tr_a, "amount": TR_FUND_A})
    test("taproot: faucet funded wallet_a", res_tr_a.get("success") is True)

    res_tr_b = api_post(server_url, "/api/faucet",
                        {"address": addr_tr_b, "amount": TR_FUND_B})
    test("taproot: faucet funded wallet_b", res_tr_b.get("success") is True)

    # ========================================================
    section("15. Fetch UTXOs via UI (Taproot)")
    # ========================================================

    page.goto(base_url)
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)
    page.wait_for_function("() => window._fn.serverMode === true", timeout=10000)
    page.select_option("#network", "regtest")

    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

    page.fill("#fetchAddress", addr_tr_a)
    page.click("#fetchUtxosBtn")
    page.wait_for_function(
        "() => document.getElementById('fetchStatus').textContent.includes('Added')",
        timeout=15000)
    status_tr = page.text_content("#fetchStatus")
    test("taproot: wallet_a UTXOs fetched", "Added 1 UTXO" in status_tr,
         f"got: {status_tr}")

    page.fill("#fetchAddress", addr_tr_b)
    page.click("#fetchUtxosBtn")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-utxo]').length === 2",
        timeout=15000)
    test("taproot: 2 input rows fetched",
         len(page.query_selector_all("[data-utxo]")) == 2)

    # Verify Taproot scriptPubKey format (OP_1 <32-byte-key> = 5120...)
    tr_scripts = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('.script-input'))
            .map(el => el.value);
    }""")
    test("taproot: scriptPubKeys are P2TR (5120...)",
         all(s.startswith("5120") and len(s) == 68 for s in tr_scripts if s),
         f"got {tr_scripts}")

    # ========================================================
    section("16. Create PSBT via UI (Taproot)")
    # ========================================================

    page.fill("#feeRate", "1")
    page.evaluate(f"""() => {{
        window._fn.addOutput(null, "{addr_tr_recip}", {TR_SEND});
    }}""")

    # Re-register dialog handler (page was reloaded)
    all_dialogs = []
    page.on("dialog", lambda d: (all_dialogs.append(d.message), d.accept()))

    clear_tip(page)
    all_dialogs.clear()
    page.click("#createPsbt")
    page.wait_for_selector("#psbtResult", state="visible", timeout=30000)
    with page.expect_download(timeout=30000) as download_info:
        page.click("#downloadPsbt")
    dl_tr = download_info.value

    test("taproot: no unexpected error on create",
         all(("error" not in d.lower()) for d in all_dialogs),
         f"got: {all_dialogs}")
    test("taproot: PSBT downloaded", dl_tr is not None)

    with open(dl_tr.path(), "rb") as f:
        psbt_tr_bin = f.read()
    test("taproot: PSBT has magic header", psbt_tr_bin[:5] == b"psbt\xff")

    # ========================================================
    section("17. Sign PSBT with bitcoin-cli (Taproot)")
    # ========================================================

    psbt_tr_b64 = base64.b64encode(psbt_tr_bin).decode("ascii")

    result_tr_a = cli.run_json("walletprocesspsbt", psbt_tr_b64,
                                "true", "DEFAULT", "true", "false",
                                wallet="wallet_a")
    test("taproot: wallet_a signed", len(result_tr_a.get("psbt", "")) > 0)

    result_tr_b = cli.run_json("walletprocesspsbt", psbt_tr_b64,
                                "true", "DEFAULT", "true", "false",
                                wallet="wallet_b")
    test("taproot: wallet_b signed", len(result_tr_b.get("psbt", "")) > 0)

    # Write signed PSBTs to temp files
    tmp_tr = tempfile.mkdtemp(prefix="psbt_taproot_")
    tr_a_path = os.path.join(tmp_tr, "tr_signed_a.psbt")
    tr_b_path = os.path.join(tmp_tr, "tr_signed_b.psbt")
    with open(tr_a_path, "wb") as f:
        f.write(base64.b64decode(result_tr_a["psbt"]))
    with open(tr_b_path, "wb") as f:
        f.write(base64.b64decode(result_tr_b["psbt"]))
    test("taproot: signed files written",
         os.path.exists(tr_a_path) and os.path.exists(tr_b_path))

    # ========================================================
    section("18. Combine & Finalize via UI (Taproot)")
    # ========================================================

    page.evaluate("() => window._fn.showCard('cardBroadcast')")
    all_dialogs.clear()
    page.set_input_files("#psbtFiles", [tr_a_path, tr_b_path])
    page.click("#combinePsbt")
    page.wait_for_timeout(3000)

    test("taproot: no error on combine", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    tr_hex = (page.text_content("#combinedResult") or "").strip()
    test("taproot: finalized hex present",
         len(tr_hex) > 0 and re.match(r'^[0-9a-fA-F]+$', tr_hex) is not None,
         f"length={len(tr_hex)}, preview={tr_hex[:40]}...")

    # ========================================================
    section("19. Broadcast via UI (Taproot)")
    # ========================================================

    all_dialogs.clear()
    page.click("#broadcastTx")
    page.wait_for_timeout(3000)

    test("taproot: no error on broadcast", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    tr_bcast = page.text_content("#broadcastResult")
    test("taproot: broadcast shows TXID",
         tr_bcast is not None and "Broadcasted TXID:" in tr_bcast,
         f"got: {tr_bcast}")

    tr_txid_match = re.search(r'[a-f0-9]{64}', tr_bcast or "")
    test("taproot: broadcast TXID valid", tr_txid_match is not None)
    tr_txid = tr_txid_match.group(0) if tr_txid_match else ""

    # ========================================================
    section("20. On-chain Verification (Taproot)")
    # ========================================================

    if not tr_txid:
        test("SKIP: taproot on-chain verification", False,
             "broadcast failed, cannot verify")
        shutil.rmtree(tmp_tr, ignore_errors=True)
    else:
        try:
            api_post(server_url, "/api/mine", {"blocks": 1})
        except Exception:
            pass

        decoded_tr = cli.run_json("getrawtransaction", tr_txid, "true")
        test("taproot: transaction confirmed",
             decoded_tr.get("confirmations", 0) >= 1,
             f"confirmations={decoded_tr.get('confirmations', 0)}")
        test("taproot: 2 inputs", len(decoded_tr.get("vin", [])) == 2,
             f"got {len(decoded_tr.get('vin', []))}")
        test("taproot: 1 output", len(decoded_tr.get("vout", [])) == 1,
             f"got {len(decoded_tr.get('vout', []))}")
        test("taproot: output amount correct",
             round(decoded_tr["vout"][0]["value"] * 1e8) == TR_SEND,
             f"expected {TR_SEND}, got {round(decoded_tr['vout'][0]['value'] * 1e8)}")

        # Verify witness uses Schnorr signature (64 bytes = key-path spend)
        for i, vin in enumerate(decoded_tr.get("vin", [])):
            witness = vin.get("txinwitness", [])
            test(f"taproot: input {i} has 1 witness item (key-path)",
                 len(witness) == 1,
                 f"got {len(witness)} items")
            if witness:
                test(f"taproot: input {i} Schnorr sig (64 bytes)",
                     len(witness[0]) == 128,  # 64 bytes = 128 hex chars
                     f"got {len(witness[0])} hex chars")

        shutil.rmtree(tmp_tr, ignore_errors=True)

    # ================================================================
    #  PART D — Taproot (P2TR) Serial Signing
    # ================================================================

    # ========================================================
    section("21. Fund Wallets (Taproot Serial)")
    # ========================================================

    TRS_FUND_A = "0.7"
    TRS_FUND_B = "0.2"
    TRS_SATS_A = 70_000_000
    TRS_SATS_B = 20_000_000
    TRS_SEND = 89_900_000      # 0.7 + 0.2 BTC minus 0.001 BTC fee

    addr_trs_a = cli.run("getnewaddress", "", "bech32m", wallet="wallet_a")
    addr_trs_b = cli.run("getnewaddress", "", "bech32m", wallet="wallet_b")
    addr_trs_recip = cli.run("getnewaddress", "", "bech32m", wallet="wallet_recipient")

    res_trs_a = api_post(server_url, "/api/faucet",
                         {"address": addr_trs_a, "amount": TRS_FUND_A})
    test("taproot serial: faucet funded wallet_a",
         res_trs_a.get("success") is True)

    res_trs_b = api_post(server_url, "/api/faucet",
                         {"address": addr_trs_b, "amount": TRS_FUND_B})
    test("taproot serial: faucet funded wallet_b",
         res_trs_b.get("success") is True)

    # ========================================================
    section("22. Fetch UTXOs & Create PSBT (Taproot Serial)")
    # ========================================================

    page.goto(base_url)
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)
    page.wait_for_function("() => window._fn.serverMode === true", timeout=10000)
    page.select_option("#network", "regtest")

    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

    page.fill("#fetchAddress", addr_trs_a)
    page.click("#fetchUtxosBtn")
    page.wait_for_function(
        "() => document.getElementById('fetchStatus').textContent.includes('Added')",
        timeout=15000)

    page.fill("#fetchAddress", addr_trs_b)
    page.click("#fetchUtxosBtn")
    page.wait_for_function(
        "() => document.querySelectorAll('[data-utxo]').length === 2",
        timeout=15000)
    test("taproot serial: 2 input rows fetched",
         len(page.query_selector_all("[data-utxo]")) == 2)

    page.fill("#feeRate", "1")
    page.evaluate(f"""() => {{
        window._fn.addOutput(null, "{addr_trs_recip}", {TRS_SEND});
    }}""")

    # Re-register dialog handler (page was reloaded)
    all_dialogs = []
    page.on("dialog", lambda d: (all_dialogs.append(d.message), d.accept()))

    clear_tip(page)
    all_dialogs.clear()
    page.click("#createPsbt")
    page.wait_for_selector("#psbtResult", state="visible", timeout=30000)
    with page.expect_download(timeout=30000) as download_info:
        page.click("#downloadPsbt")
    dl_trs = download_info.value

    test("taproot serial: no unexpected error on create",
         all(("error" not in d.lower()) for d in all_dialogs),
         f"got: {all_dialogs}")

    with open(dl_trs.path(), "rb") as f:
        psbt_trs_bin = f.read()
    test("taproot serial: PSBT has magic header",
         psbt_trs_bin[:5] == b"psbt\xff")

    # ========================================================
    section("23. Sign Serially A -> B (Taproot)")
    # ========================================================

    psbt_trs_b64 = base64.b64encode(psbt_trs_bin).decode("ascii")

    # Step 1: wallet_a signs -> partial
    after_trs_a = cli.run_json("walletprocesspsbt", psbt_trs_b64,
                                "true", "DEFAULT", "true", "false",
                                wallet="wallet_a")
    test("taproot serial: wallet_a signed",
         len(after_trs_a.get("psbt", "")) > 0)

    # Step 2: wallet_b signs wallet_a's output -> both sigs present
    after_trs_b = cli.run_json("walletprocesspsbt", after_trs_a["psbt"],
                                "true", "DEFAULT", "true", "false",
                                wallet="wallet_b")
    test("taproot serial: wallet_b signed",
         len(after_trs_b.get("psbt", "")) > 0)

    # Write single file with both signatures
    tmp_trs = tempfile.mkdtemp(prefix="psbt_tr_serial_")
    trs_path = os.path.join(tmp_trs, "tr_serial_signed.psbt")
    with open(trs_path, "wb") as f:
        f.write(base64.b64decode(after_trs_b["psbt"]))
    test("taproot serial: signed file written", os.path.exists(trs_path))

    # ========================================================
    section("24. Finalize via UI (Taproot Serial)")
    # ========================================================

    page.evaluate("() => window._fn.showCard('cardBroadcast')")
    all_dialogs.clear()
    page.set_input_files("#psbtFiles", [trs_path])
    page.click("#combinePsbt")
    page.wait_for_timeout(3000)

    test("taproot serial: no error on finalize", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    trs_hex = (page.text_content("#combinedResult") or "").strip()
    test("taproot serial: finalized hex present",
         len(trs_hex) > 0 and re.match(r'^[0-9a-fA-F]+$', trs_hex) is not None,
         f"length={len(trs_hex)}")

    # ========================================================
    section("25. Broadcast & Verify (Taproot Serial)")
    # ========================================================

    all_dialogs.clear()
    page.click("#broadcastTx")
    page.wait_for_timeout(3000)

    test("taproot serial: no error on broadcast", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    trs_bcast = page.text_content("#broadcastResult")
    test("taproot serial: broadcast shows TXID",
         trs_bcast is not None and "Broadcasted TXID:" in trs_bcast,
         f"got: {trs_bcast}")

    trs_txid_match = re.search(r'[a-f0-9]{64}', trs_bcast or "")
    test("taproot serial: TXID valid", trs_txid_match is not None)
    trs_txid = trs_txid_match.group(0) if trs_txid_match else ""

    if not trs_txid:
        test("SKIP: taproot serial on-chain verification", False,
             "broadcast failed")
    else:
        try:
            api_post(server_url, "/api/mine", {"blocks": 1})
        except Exception:
            pass

        decoded_trs = cli.run_json("getrawtransaction", trs_txid, "true")
        test("taproot serial: transaction confirmed",
             decoded_trs.get("confirmations", 0) >= 1,
             f"confirmations={decoded_trs.get('confirmations', 0)}")
        test("taproot serial: 2 inputs",
             len(decoded_trs.get("vin", [])) == 2,
             f"got {len(decoded_trs.get('vin', []))}")
        test("taproot serial: 1 output",
             len(decoded_trs.get("vout", [])) == 1,
             f"got {len(decoded_trs.get('vout', []))}")
        test("taproot serial: output amount correct",
             round(decoded_trs["vout"][0]["value"] * 1e8) == TRS_SEND,
             f"expected {TRS_SEND}, got {round(decoded_trs['vout'][0]['value'] * 1e8)}")

    shutil.rmtree(tmp_trs, ignore_errors=True)

    # ================================================================
    # Part E: WIF Fetch & Inline Sign (index.html)
    # ================================================================

    # ========================================================
    section("26. Generate Keypair & Fund (WIF Fetch)")
    # ========================================================

    page.goto(base_url)
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)
    page.wait_for_function("() => window._fn.serverMode === true", timeout=10000)
    page.select_option("#network", "regtest")

    # Generate keypair in index.html (which now has ECPair)
    kp_wif = page.evaluate("""() => {
        const net = window._fn.getSelectedNetwork();
        const kp = window._ECPair.makeRandom({ network: net });
        const { address } = window._bitcoin.payments.p2wpkh({
            pubkey: kp.publicKey, network: net
        });
        return { wif: kp.toWIF(), address: address };
    }""")

    test("wif fetch: keypair generated",
         kp_wif.get("wif") is not None and kp_wif.get("address") is not None)
    test("wif fetch: address is bcrt1q",
         kp_wif["address"].startswith("bcrt1q"),
         f"got {kp_wif['address']}")

    # Fund the generated address
    F_FUND = "0.3"
    F_FUND_SATS = 30_000_000
    F_SEND = 29_900_000     # minus fee

    res_f = api_post(server_url, "/api/faucet",
                     {"address": kp_wif["address"], "amount": F_FUND})
    test("wif fetch: faucet funded", res_f.get("success") is True)

    # Create recipient address
    addr_f_recip = cli.run("getnewaddress", "", "bech32", wallet="wallet_recipient")
    test("wif fetch: recipient address valid",
         addr_f_recip.startswith("bcrt1q"), f"got {addr_f_recip}")

    # ========================================================
    section("27. Fetch UTXOs via WIF")
    # ========================================================

    # Clear existing UTXOs and outputs
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

    # Paste WIF into fetch input
    page.fill("#fetchAddress", kp_wif["wif"])
    page.click("#fetchUtxosBtn")
    page.wait_for_function(
        "() => document.getElementById('fetchStatus').textContent.includes('Added')",
        timeout=15000)

    status_f = page.text_content("#fetchStatus")
    test("wif fetch: UTXOs fetched", "Added" in status_f and "from WIF" in status_f,
         f"got: {status_f}")

    # Verify WIF was cleared from input
    fetch_val = page.evaluate("() => document.getElementById('fetchAddress').value")
    test("wif fetch: input cleared after fetch", fetch_val == "",
         f"got: '{fetch_val}'")

    # Check UTXO rows exist
    utxo_count = len(page.query_selector_all("[data-utxo]"))
    test("wif fetch: at least 1 UTXO row", utxo_count >= 1,
         f"got {utxo_count}")

    # Verify data-wif attribute is set
    data_wif_val = page.evaluate(
        "() => document.querySelector('[data-utxo]').getAttribute('data-wif')")
    test("wif fetch: data-wif attribute set", data_wif_val == kp_wif["wif"])

    # Verify WIF toggle shows checkmark
    wif_toggle = page.evaluate(
        "() => document.querySelector('.wif-toggle').textContent")
    test("wif fetch: WIF toggle shows checkmark", '\u2714' in wif_toggle,
         f"got '{wif_toggle}'")

    # Verify allUtxosHaveWif returns true
    all_wif = page.evaluate("() => window._fn.allUtxosHaveWif()")
    test("wif fetch: allUtxosHaveWif is true", all_wif is True)

    # ========================================================
    section("28. Dynamic Step Layout (WIF mode)")
    # ========================================================

    # Step layout should show 2 steps (WIF mode)
    visible_steps = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('.step-indicator .step'))
            .filter(s => s.style.display !== 'none').length;
    }""")
    test("wif mode: 2 steps visible", visible_steps == 2,
         f"got {visible_steps}")

    # Button should say "Create, Sign & Finalize"
    btn_text = page.evaluate("() => document.getElementById('createPsbt').textContent")
    test("wif mode: button says 'Create, Sign & Finalize'",
         'Sign' in btn_text, f"got '{btn_text}'")

    # ========================================================
    section("29. Create, Sign & Finalize via WIF (inline)")
    # ========================================================

    all_dialogs.clear()

    # Set fee rate
    page.fill("#feeRate", "1")
    time.sleep(0.5)

    # Add output
    page.evaluate("() => window._fn.addOutput()")
    page.wait_for_selector("[data-output]")
    output_row = page.query_selector("[data-output]")
    addr_input = output_row.query_selector(".output-address")
    addr_input.fill(addr_f_recip)

    # Set output value
    value_input = output_row.query_selector("input[placeholder='sats']")
    value_input.fill(str(F_SEND))

    # Click "Create, Sign & Finalize"
    clear_tip(page)
    page.click("#createPsbt")
    page.wait_for_timeout(2000)

    # Should have no alerts
    test("wif inline sign: no error alerts", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    # Broadcast card should be visible
    broadcast_visible = page.evaluate(
        "() => !document.getElementById('cardBroadcast').classList.contains('hidden')")
    test("wif inline sign: broadcast card visible", broadcast_visible is True)

    # Create card should be hidden
    create_hidden = page.evaluate(
        "() => document.getElementById('cardCreate').classList.contains('hidden')")
    test("wif inline sign: create card hidden", create_hidden is True)

    # Signed tx hex should be displayed
    tx_hex = page.evaluate(
        "() => document.getElementById('wifSignedTxHex').textContent")
    test("wif inline sign: signed tx hex shown",
         tx_hex is not None and len(tx_hex) > 100,
         f"hex length: {len(tx_hex) if tx_hex else 0}")

    # ========================================================
    section("30. Broadcast & Verify (WIF inline sign)")
    # ========================================================

    all_dialogs.clear()
    page.click("#broadcastTx")
    page.wait_for_timeout(3000)

    test("wif broadcast: no error on broadcast", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    f_bcast = page.text_content("#broadcastResult")
    test("wif broadcast: shows TXID",
         f_bcast is not None and "Broadcasted TXID:" in f_bcast,
         f"got: {f_bcast}")

    f_txid_match = re.search(r'[a-f0-9]{64}', f_bcast or "")
    test("wif broadcast: TXID valid", f_txid_match is not None)
    f_txid = f_txid_match.group(0) if f_txid_match else ""

    if not f_txid:
        test("SKIP: wif broadcast on-chain verification", False,
             "broadcast failed")
    else:
        try:
            api_post(server_url, "/api/mine", {"blocks": 1})
        except Exception:
            pass

        decoded_f = cli.run_json("getrawtransaction", f_txid, "true")
        test("wif broadcast: transaction confirmed",
             decoded_f.get("confirmations", 0) >= 1,
             f"confirmations={decoded_f.get('confirmations', 0)}")
        test("wif broadcast: 1 input",
             len(decoded_f.get("vin", [])) == 1,
             f"got {len(decoded_f.get('vin', []))}")
        test("wif broadcast: 1 output",
             len(decoded_f.get("vout", [])) == 1,
             f"got {len(decoded_f.get('vout', []))}")
        test("wif broadcast: output amount correct",
             round(decoded_f["vout"][0]["value"] * 1e8) == F_SEND,
             f"expected {F_SEND}, got {round(decoded_f['vout'][0]['value'] * 1e8)}")


    # ================================================================
    # Part F: Mixed WIF Partial Signing
    # ================================================================

    # ========================================================
    section("31. Generate Two Keypairs & Fund (Mixed WIF)")
    # ========================================================

    page.goto(base_url)
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)
    page.wait_for_function("() => window._fn.serverMode === true", timeout=10000)
    page.select_option("#network", "regtest")

    # Generate keypair A (will be fetched via WIF → has WIF)
    kp_a = page.evaluate("""() => {
        const net = window._fn.getSelectedNetwork();
        const kp = window._ECPair.makeRandom({ network: net });
        const { address } = window._bitcoin.payments.p2wpkh({
            pubkey: kp.publicKey, network: net
        });
        return { wif: kp.toWIF(), address: address };
    }""")
    test("mixed: keypair A generated", kp_a.get("wif") is not None)

    # Generate keypair B (will be fetched via address → no WIF)
    kp_b = page.evaluate("""() => {
        const net = window._fn.getSelectedNetwork();
        const kp = window._ECPair.makeRandom({ network: net });
        const { address } = window._bitcoin.payments.p2wpkh({
            pubkey: kp.publicKey, network: net
        });
        return { wif: kp.toWIF(), address: address };
    }""")
    test("mixed: keypair B generated", kp_b.get("wif") is not None)

    # Fund both addresses
    G_FUND = "0.2"
    G_SEND = 39_999_000  # 0.4 BTC total minus ~1000 sats fee

    res_a = api_post(server_url, "/api/faucet",
                     {"address": kp_a["address"], "amount": G_FUND})
    test("mixed: faucet funded A", res_a.get("success") is True)

    res_b = api_post(server_url, "/api/faucet",
                     {"address": kp_b["address"], "amount": G_FUND})
    test("mixed: faucet funded B", res_b.get("success") is True)

    # Create recipient address
    addr_g_recip = cli.run("getnewaddress", "", "bech32", wallet="wallet_recipient")
    test("mixed: recipient address valid",
         addr_g_recip.startswith("bcrt1q"), f"got {addr_g_recip}")

    # ========================================================
    section("32. Fetch Mixed UTXOs (WIF + Address)")
    # ========================================================

    # Clear existing UTXOs and outputs
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

    # Fetch via WIF A (will have WIF attached)
    page.fill("#fetchAddress", kp_a["wif"])
    page.click("#fetchUtxosBtn")
    page.wait_for_function(
        "() => document.getElementById('fetchStatus').textContent.includes('Added')",
        timeout=15000)
    test("mixed: WIF A fetched",
         "Added" in page.text_content("#fetchStatus"))

    # Fetch via address B (will NOT have WIF)
    page.fill("#fetchAddress", kp_b["address"])
    page.click("#fetchUtxosBtn")
    page.wait_for_function(
        f"() => document.querySelectorAll('[data-utxo]').length >= 2",
        timeout=15000)

    utxo_count = len(page.query_selector_all("[data-utxo]"))
    test("mixed: 2+ UTXO rows present", utxo_count >= 2, f"got {utxo_count}")

    # Verify mixed state
    all_wif = page.evaluate("() => window._fn.allUtxosHaveWif()")
    test("mixed: allUtxosHaveWif is false", all_wif is False)

    some_wif = page.evaluate("() => window._fn.someUtxosHaveWif()")
    test("mixed: someUtxosHaveWif is true", some_wif is True)

    # ========================================================
    section("33. Mixed Mode Layout & Create Partially Signed PSBT")
    # ========================================================

    # Verify button text
    btn_text = page.evaluate("() => document.getElementById('createPsbt').textContent")
    test("mixed: button says 'Create PSBT'",
         btn_text.strip() == 'Create PSBT', f"got '{btn_text}'")

    # Verify 3-step layout (Create → Combine → Broadcast)
    visible_steps = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('.step-indicator .step'))
            .filter(s => s.style.display !== 'none').length;
    }""")
    test("mixed: 2 steps visible", visible_steps == 2, f"got {visible_steps}")

    # Set output and fee
    page.evaluate("() => window._fn.addOutput()")
    page.wait_for_selector("[data-output]")
    output_row = page.query_selector("[data-output]")
    output_row.query_selector(".output-address").fill(addr_g_recip)
    output_row.query_selector(".output-value").fill(str(G_SEND))

    time.sleep(2)
    page.fill("#feeRate", "1")

    # Create partially signed PSBT
    clear_tip(page)
    all_dialogs.clear()
    page.click("#createPsbt")
    page.wait_for_selector("#psbtResult", state="visible", timeout=10000)

    test("mixed: no error alert on create", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    # Get the PSBT hex — it should be partially signed
    psbt_hex = page.text_content("#psbtHex")
    test("mixed: PSBT hex present", len(psbt_hex or "") > 0)

    # ========================================================
    section("34. Sign Remaining Inputs & Combine")
    # ========================================================

    # Parse the PSBT — all inputs should be unsigned (WIF signing deferred to combine step)
    partial_info = page.evaluate(f"""() => {{
        const bitcoin = window._bitcoin;
        const net = window._fn.getSelectedNetwork();
        const psbt = bitcoin.Psbt.fromHex(document.getElementById('psbtHex').textContent, {{ network: net }});
        let signed = 0;
        let unsigned = 0;
        for (let i = 0; i < psbt.data.inputs.length; i++) {{
            const inp = psbt.data.inputs[i];
            if (inp.partialSig && inp.partialSig.length > 0) signed++;
            else unsigned++;
        }}
        return {{ signed, unsigned, total: psbt.data.inputs.length }};
    }}""")
    test("mixed: all inputs unsigned (WIF deferred)",
         partial_info["signed"] == 0, f"got {partial_info}")
    test("mixed: total inputs correct",
         partial_info["total"] == partial_info["unsigned"], f"got {partial_info}")

    # Simulate HW wallet signing: sign only the non-WIF input (wallet B) externally
    # This mimics what a Coldcard would do — sign its input, leave others unsigned
    psbt_b64 = page.evaluate("""() => {
        const bitcoin = window._bitcoin;
        const net = window._fn.getSelectedNetwork();
        const psbt = bitcoin.Psbt.fromHex(
            document.getElementById('psbtHex').textContent, { network: net });
        return psbt.toBase64();
    }""")

    signed_b64 = page.evaluate(f"""(args) => {{
        const {{ psbtB64, wif }} = args;
        const bitcoin = window._bitcoin;
        const net = window._fn.getSelectedNetwork();
        const ECPair = window._ECPair;
        const keyPair = ECPair.fromWIF(wif, net);
        const psbt = bitcoin.Psbt.fromBase64(psbtB64, {{ network: net }});
        for (let i = 0; i < psbt.data.inputs.length; i++) {{
            try {{ psbt.signInput(i, keyPair); }} catch (e) {{}}
        }}
        return psbt.toBase64();
    }}""", {"psbtB64": psbt_b64, "wif": kp_b["wif"]})

    test("mixed: HW-signed PSBT obtained", signed_b64 is not None and len(signed_b64) > 0)

    # Write HW-signed PSBT to temp file for upload
    signed_bytes = base64.b64decode(signed_b64)
    with tempfile.NamedTemporaryFile(suffix=".psbt", delete=False) as f:
        f.write(signed_bytes)
        signed_path = f.name

    # Navigate to Sign card and upload — combine step will sign WIF inputs automatically
    page.evaluate("() => window._fn.showCard('cardBroadcast')")
    page.set_input_files("#psbtFiles", [signed_path])
    page.wait_for_function(
        "() => document.querySelectorAll('.psbt-list-item').length >= 1",
        timeout=5000)

    psbt_items = len(page.query_selector_all(".psbt-list-item"))
    test("mixed: HW-signed PSBT uploaded to accumulator", psbt_items >= 1,
         f"got {psbt_items}")

    # Combine & Finalize
    all_dialogs.clear()
    page.click("#combinePsbt")
    page.wait_for_timeout(2000)

    test("mixed: no error on combine", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    # Should have auto-navigated to broadcast card
    broadcast_visible = page.evaluate(
        "() => !document.getElementById('cardBroadcast').classList.contains('hidden')")
    test("mixed: broadcast card visible after combine", broadcast_visible)

    # ========================================================
    section("35. Broadcast & Verify (Mixed WIF)")
    # ========================================================

    all_dialogs.clear()
    page.click("#broadcastTx")
    page.wait_for_timeout(3000)

    test("mixed broadcast: no error", len(all_dialogs) == 0,
         f"got: {all_dialogs}")

    g_bcast = page.text_content("#broadcastResult")
    test("mixed broadcast: shows TXID",
         g_bcast is not None and "Broadcasted TXID:" in g_bcast,
         f"got: {g_bcast}")

    g_txid_match = re.search(r'[a-f0-9]{64}', g_bcast or "")
    test("mixed broadcast: TXID valid", g_txid_match is not None)
    g_txid = g_txid_match.group(0) if g_txid_match else ""

    if not g_txid:
        test("SKIP: mixed broadcast on-chain verification", False,
             "broadcast failed")
    else:
        try:
            api_post(server_url, "/api/mine", {"blocks": 1})
        except Exception:
            pass

        decoded_g = cli.run_json("getrawtransaction", g_txid, "true")
        test("mixed broadcast: transaction confirmed",
             decoded_g.get("confirmations", 0) >= 1,
             f"confirmations={decoded_g.get('confirmations', 0)}")
        test("mixed broadcast: 2 inputs",
             len(decoded_g.get("vin", [])) == 2,
             f"got {len(decoded_g.get('vin', []))}")
        test("mixed broadcast: 1 output",
             len(decoded_g.get("vout", [])) == 1,
             f"got {len(decoded_g.get('vout', []))}")
        test("mixed broadcast: output amount correct",
             round(decoded_g["vout"][0]["value"] * 1e8) == G_SEND,
             f"expected {G_SEND}, got {round(decoded_g['vout'][0]['value'] * 1e8)}")

    # Clean up temp file
    try:
        os.unlink(signed_path)
    except Exception:
        pass


# ============================================================
# Main
# ============================================================

def main():
    # Check prerequisites
    if not shutil.which("bitcoind") or not shutil.which("bitcoin-cli"):
        print("SKIP: bitcoind/bitcoin-cli not found in PATH.")
        print("Install Bitcoin Core to run regtest E2E tests.")
        sys.exit(0)

    port = find_free_port()
    server_url = f"http://127.0.0.1:{port}"
    base_url = f"http://127.0.0.1:{port}/index.html"
    server_proc = None

    print(f"Starting server with --regtest on port {port}...")
    print(f"Mode: {'headed' if HEADED else 'headless'}\n")

    try:
        server_proc, health = start_server(port)
        print(f"Server ready. Regtest node: {health.get('datadir')}")

        cli = BitcoinCLI(health["datadir"], health["rpc_port"])

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not HEADED)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.add_init_script("window.__TEST_MODE__ = true;")

            run_tests(page, base_url, cli, server_url)

            browser.close()
    except Exception:
        traceback.print_exc()
    finally:
        if server_proc:
            print("\nStopping server...")
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
