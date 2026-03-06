#!/usr/bin/env python3
"""
End-to-end regtest test for Bitcoin PSBT Builder.

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

    # No-change mode
    page.uncheck("#includeChange")

    # Add output to recipient
    page.evaluate(f"""() => {{
        window._fn.addOutput(null, "{addr_recipient}", {SEND_SATS});
    }}""")

    # Verify fee display
    page.wait_for_timeout(500)
    fee_text = page.text_content("#feeCalc")
    test("fee calc shows fee", "100000" in (fee_text or ""), f"got: {fee_text}")

    # Set up a single dialog handler for the entire test
    all_dialogs = []
    page.on("dialog", lambda d: (all_dialogs.append(d.message), d.accept()))

    # Click Create & Download and expect a file download
    all_dialogs.clear()
    with page.expect_download(timeout=30000) as download_info:
        page.click("#createPsbt")
    download = download_info.value

    test("no error dialog on create", len(all_dialogs) == 0,
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
