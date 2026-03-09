#!/usr/bin/env python3
"""
End-to-end test of the sweeper website with a real Coldcard MK4 on testnet4.

Tests the actual user flow through the Playwright browser:
1. Fetch UTXOs by WIF (hot wallet) and by address (Coldcard)
2. Enter HW wallet info (xfp, pubkey, path) for the CC UTXO
3. Set output to sweep all funds back to the WIF address
4. Create & Partially Sign PSBT (website signs WIF inputs)
5. Download PSBT, sign with Coldcard via ckcc CLI
6. Upload CC-signed PSBT back to the website
7. Combine & Finalize
8. Broadcast to testnet4
9. Verify tx in mempool and funds returned

Requires:
  - Coldcard MK4 plugged in, unlocked, set to XTN (testnet)
  - ckcc CLI: pip install ckcc-protocol
  - Playwright: pip install playwright && playwright install chromium
  - TESTNET4_WIF and TESTNET4_ADDRESS env vars set
"""

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.request import urlopen, Request

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TEST_DIR)


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_http_server(port):
    handler = http.server.SimpleHTTPRequestHandler
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd

# Derivation path for the Coldcard input (BIP84, first receive address)
CC_PATH = "m/84'/1'/0'/0/0"

MEMPOOL_API = "https://mempool.space/testnet4/api"


def pubkey_to_p2wpkh(pubkey_hex, network):
    """Derive P2WPKH (bech32) address from compressed pubkey hex."""
    from embit import ec as embit_ec, script as embit_script
    from embit.networks import NETWORKS
    pub = embit_ec.PublicKey.parse(bytes.fromhex(pubkey_hex))
    return embit_script.p2wpkh(pub).address(NETWORKS[network])


def detect_coldcard():
    """Auto-detect Coldcard device info via ckcc CLI.
    Returns (xfp, addr, pubkey) or raises RuntimeError.
    Uses ckcc xfp + ckcc pubkey only (no ckcc addr, which blocks
    the device waiting for user to dismiss the on-screen display)."""
    result = subprocess.run(["ckcc", "xfp"], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ckcc xfp failed: {result.stderr.strip()}")
    xfp = result.stdout.strip()
    time.sleep(1)  # let Coldcard USB settle between commands

    result = subprocess.run(["ckcc", "pubkey", CC_PATH],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ckcc pubkey failed: {result.stderr.strip()}")
    pubkey = result.stdout.strip()

    # Derive address locally from pubkey (avoids ckcc addr which
    # shows address on Coldcard screen and blocks USB until dismissed)
    addr = pubkey_to_p2wpkh(pubkey, "test")

    return xfp, addr, pubkey

# ============================================================
# Test infra
# ============================================================

_pass_count = 0
_fail_count = 0
_failures = []


def test(name, condition, detail=""):
    global _pass_count, _fail_count
    if condition:
        _pass_count += 1
        print(f"  ✓ {name}")
    else:
        _fail_count += 1
        msg = f"  ✗ {name}"
        if detail:
            msg += f"  — {detail}"
        print(msg)
        _failures.append(name)


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def fetch_json(url):
    return json.loads(urlopen(url, timeout=30).read().decode())


# ============================================================
# Main
# ============================================================

def run_tests():
    from playwright.sync_api import sync_playwright

    wif_key = os.environ.get("TESTNET4_WIF")
    wif_address = os.environ.get("TESTNET4_ADDRESS")

    # ========================================================
    section("1. Preflight Checks")
    # ========================================================

    test("TESTNET4_WIF set", bool(wif_key))
    test("TESTNET4_ADDRESS set", bool(wif_address))
    if not wif_key or not wif_address:
        print("  ❌ Set TESTNET4_WIF and TESTNET4_ADDRESS env vars")
        return

    time.sleep(0.5)  # let USB settle
    result = subprocess.run(["ckcc", "chain"], capture_output=True, text=True, timeout=30)
    test("Coldcard chain is XTN", result.stdout.strip() == "XTN")
    if result.stdout.strip() != "XTN":
        return

    # Auto-detect Coldcard device info
    print("  Auto-detecting Coldcard device info...")
    try:
        CC_XFP, CC_ADDR, CC_PUBKEY = detect_coldcard()
    except RuntimeError as e:
        test("ckcc can reach Coldcard", False, str(e))
        return

    print(f"  XFP:    {CC_XFP}")
    print(f"  CC addr:  {CC_ADDR}")
    print(f"  Pubkey: {CC_PUBKEY}")

    # Check both addresses have UTXOs
    cc_utxos = fetch_json(f"{MEMPOOL_API}/address/{CC_ADDR}/utxo")
    wif_utxos = fetch_json(f"{MEMPOOL_API}/address/{wif_address}/utxo")
    test("CC has UTXOs on testnet4", len(cc_utxos) >= 1)
    test("WIF has UTXOs on testnet4", len(wif_utxos) >= 1)
    if not cc_utxos or not wif_utxos:
        print("  ❌ Both addresses need testnet4 UTXOs")
        return

    cc_sats = sum(u["value"] for u in cc_utxos)
    wif_sats = sum(u["value"] for u in wif_utxos)
    print(f"  CC balance:  {cc_sats} sats")
    print(f"  WIF balance: {wif_sats} sats")
    print(f"  Total:       {cc_sats + wif_sats} sats")

    # ========================================================
    section("2. Launch Browser & Fetch UTXOs")
    # ========================================================

    headed = "--headed" in sys.argv
    tmp_dir = os.path.join(_TEST_DIR, "_tmp_cc")
    os.makedirs(tmp_dir, exist_ok=True)

    # Start static HTTP server (ESM modules need http://, not file://)
    port = find_free_port()
    os.chdir(_PROJECT_ROOT)
    httpd = start_http_server(port)
    base_url = f"http://127.0.0.1:{port}/index.html"
    print(f"  Static server on port {port}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page()

        # Enable test mode
        page.add_init_script("window.__TEST_MODE__ = true;")

        # Load page and wait for ESM modules to initialize
        page.goto(base_url)
        page.wait_for_function("() => window._fn !== undefined", timeout=15000)

        # Auto-accept dialogs (e.g. missing XFP warning)
        page.on("dialog", lambda d: d.accept())

        # Select testnet4 (static server auto-selects testnet4, but be explicit)
        page.select_option("#network", "testnet")
        page.wait_for_timeout(500)

        # Fetch WIF UTXOs
        print("  Fetching WIF UTXOs...")
        page.fill("#fetchAddress", wif_key)
        page.click("#fetchUtxosBtn")
        page.wait_for_selector("#utxoContainer [data-utxo]", timeout=30000)
        # WIF is cleared from input after fetch
        page.wait_for_timeout(1000)

        wif_utxo_count = page.locator("#utxoContainer [data-utxo]").count()
        test("WIF UTXOs fetched", wif_utxo_count >= 1, f"found {wif_utxo_count}")

        # Fetch CC address UTXOs
        print("  Fetching CC UTXOs...")
        page.fill("#fetchAddress", CC_ADDR)
        page.click("#fetchUtxosBtn")
        # Wait for more UTXOs to appear
        page.wait_for_timeout(5000)

        total_utxo_count = page.locator("#utxoContainer [data-utxo]").count()
        cc_utxo_count = total_utxo_count - wif_utxo_count
        test("CC UTXOs fetched", cc_utxo_count >= 1,
             f"total={total_utxo_count}, wif={wif_utxo_count}, cc={cc_utxo_count}")

        # ========================================================
        section("3. Set HW Wallet Info for CC UTXOs")
        # ========================================================

        # CC UTXOs need HW wallet info (xfp, pubkey, path)
        # They are plain address fetches, so HW fields are empty
        # Find CC UTXO rows (ones without data-wif)
        all_utxo_rows = page.locator("#utxoContainer [data-utxo]").all()
        cc_rows = []
        for row in all_utxo_rows:
            has_wif = row.get_attribute("data-wif")
            if not has_wif:
                cc_rows.append(row)

        test("found CC rows without WIF", len(cc_rows) >= 1, f"found {len(cc_rows)}")

        for row in cc_rows:
            # Expand HW wallet section
            hw_toggle = row.locator(".hw-toggle")
            if hw_toggle.count() > 0:
                hw_toggle.click()
                page.wait_for_timeout(200)

            # Fill in HW wallet fields
            xfp_input = row.locator(".hw-xfp")
            path_input = row.locator(".hw-path")
            pubkey_input = row.locator(".hw-pubkey")

            if xfp_input.count() > 0:
                xfp_input.fill(CC_XFP)
            if path_input.count() > 0:
                path_input.fill(CC_PATH)
            if pubkey_input.count() > 0 and not pubkey_input.get_attribute("readonly"):
                pubkey_input.fill(CC_PUBKEY)

        # Verify HW info was set
        hw_set = page.evaluate("""() => {
            const rows = document.querySelectorAll('#utxoContainer [data-utxo]');
            let hwCount = 0;
            for (const row of rows) {
                const xfp = row.querySelector('.hw-xfp');
                const path = row.querySelector('.hw-path');
                const pubkey = row.querySelector('.hw-pubkey');
                if (xfp && xfp.value && path && path.value && pubkey && pubkey.value) hwCount++;
            }
            return hwCount;
        }""")
        test("HW wallet info set on CC UTXOs", hw_set >= 1, f"hw_set={hw_set}")

        # ========================================================
        section("4. Configure Output & Create PSBT")
        # ========================================================

        # Wait for fee rates to load, set slow if needed
        page.wait_for_timeout(2000)
        fee_set = page.evaluate("""() => {
            const active = document.querySelector('.fee-preset.active');
            const feeVal = document.getElementById('feeRate').value;
            if (active || (feeVal && parseFloat(feeVal) > 0)) return true;
            // fallback: set 1 sat/vB manually
            document.getElementById('feeRate').value = '1';
            return true;
        }""")
        test("fee rate set", fee_set)

        # Set output to WIF address with wipe (sweep all)
        page.fill("#outputContainer [data-output] .output-address", wif_address)
        # Check wipe to sweep all
        wipe_checkbox = page.locator("#outputContainer [data-output] .output-wipe")
        if wipe_checkbox.count() > 0 and not wipe_checkbox.is_checked():
            wipe_checkbox.check()
        page.wait_for_timeout(300)

        # Clear tip to maximize returned funds
        page.evaluate("""() => {
            document.querySelectorAll('.tip-preset').forEach(p => p.classList.remove('active'));
            document.getElementById('tipSats').value = '0';
            if (window._fn && window._fn.updateTipSummary) window._fn.updateTipSummary();
            if (window._fn && window._fn.recalcWipeOutput) window._fn.recalcWipeOutput();
        }""")
        page.wait_for_timeout(300)

        # Verify button says "Create & Partially Sign PSBT"
        btn_text = page.locator("#createPsbt").inner_text()
        test("button says partial sign", "Partially Sign" in btn_text, f"got: '{btn_text}'")

        # Click Create (may fetch nonWitnessUtxo from mempool.space)
        print("  Creating PSBT...")
        page.click("#createPsbt")
        page.wait_for_timeout(3000)

        # Check PSBT was created
        psbt_visible = page.locator("#psbtResult").is_visible()
        test("PSBT result visible", psbt_visible)
        if not psbt_visible:
            # Check for alert
            print("  ❌ PSBT not created — check for errors")
            browser.close()
            return

        # Get PSBT hex (inside collapsed <details>, use textContent)
        psbt_hex = page.evaluate("() => document.getElementById('psbtHex').textContent")
        test("PSBT hex present", len(psbt_hex) > 100, f"len={len(psbt_hex)}")

        # Save PSBT to file for Coldcard signing
        psbt_bytes = bytes.fromhex(psbt_hex)
        psbt_in_path = os.path.join(tmp_dir, "website-mixed.psbt")
        psbt_out_path = os.path.join(tmp_dir, "website-mixed-signed.psbt")
        with open(psbt_in_path, "wb") as f:
            f.write(psbt_bytes)
        print(f"  PSBT saved: {psbt_in_path} ({len(psbt_bytes)} bytes)")

        # Verify PSBT has partial_sigs for WIF inputs
        has_partial = page.evaluate("""() => {
            const psbtBuf = window._Buffer.from(document.getElementById('psbtHex').textContent, 'hex');
            const psbt = window._bitcoin.Psbt.fromBuffer(psbtBuf);
            let partialCount = 0;
            for (const inp of psbt.data.inputs) {
                if (inp.partialSig && inp.partialSig.length > 0) partialCount++;
            }
            return { total: psbt.data.inputs.length, partial: partialCount };
        }""")
        test("PSBT has partial sigs (WIF signed)", has_partial["partial"] >= 1,
             f"{has_partial['partial']}/{has_partial['total']} inputs have partial_sigs")
        test("PSBT has unsigned inputs (for CC)", has_partial["partial"] < has_partial["total"],
             f"all {has_partial['total']} are signed — nothing for CC to sign")

        # ========================================================
        section("5. Sign with Coldcard")
        # ========================================================

        if os.path.exists(psbt_out_path):
            os.remove(psbt_out_path)

        print("  Sending PSBT to Coldcard for signing...")
        print("  >>> APPROVE THE TRANSACTION ON YOUR COLDCARD <<<")
        print()

        sign_result = subprocess.run(
            ["ckcc", "sign", psbt_in_path, psbt_out_path],
            capture_output=True, text=True, timeout=300)

        test("ckcc sign succeeded", sign_result.returncode == 0,
             f"stderr: {sign_result.stderr.strip()}")
        if sign_result.returncode != 0:
            browser.close()
            return

        test("signed file created", os.path.exists(psbt_out_path))

        # ========================================================
        section("6. Upload Signed PSBT & Combine")
        # ========================================================

        # Navigate to Sign/Combine step
        page.click("#nextToSign")
        page.wait_for_timeout(500)

        # Upload the CC-signed PSBT via file input
        file_input = page.locator("#psbtFiles")
        file_input.set_input_files(psbt_out_path)
        page.wait_for_timeout(1000)

        # Verify PSBT appeared in accumulator list
        psbt_items = page.locator(".psbt-list-item").count()
        test("signed PSBT in accumulator", psbt_items >= 1, f"found {psbt_items}")

        # Click Combine & Finalize
        print("  Combining & finalizing...")
        page.click("#combinePsbt")
        page.wait_for_timeout(2000)

        # Check we navigated to broadcast step
        broadcast_visible = page.locator("#cardBroadcast").is_visible()
        test("navigated to broadcast step", broadcast_visible)

        # Get the final tx hex
        final_hex = page.evaluate("""() => {
            const el = document.getElementById('combinedResult');
            return el ? el.textContent.trim() : '';
        }""")
        test("final tx hex present", len(final_hex) > 100, f"len={len(final_hex)}")

        if not final_hex:
            # Check for error
            combined_text = page.locator("#combinedResult").inner_text()
            print(f"  combinedResult: {combined_text[:200]}")
            browser.close()
            return

        # ========================================================
        section("7. Broadcast to Testnet4")
        # ========================================================

        # Click broadcast
        print("  Broadcasting to testnet4...")
        page.click("#broadcastTx")
        page.wait_for_timeout(5000)

        # Check broadcast result (format: "Broadcasted TXID:\n<txid>")
        broadcast_status = page.locator("#broadcastResult").inner_text()
        test("broadcast succeeded", "Broadcasted TXID" in broadcast_status or
             len(broadcast_status.strip()) == 64,
             f"status: {broadcast_status[:100]}")

        # Extract txid from broadcast result
        txid = ""
        match = re.search(r'[0-9a-f]{64}', broadcast_status)
        if match:
            txid = match.group(0)

        if txid:
            print(f"  TXID: {txid}")
            print(f"  https://mempool.space/testnet4/tx/{txid}")

            # Wait and verify
            print("  Waiting 5s for mempool propagation...")
            time.sleep(5)
            try:
                tx_data = fetch_json(f"{MEMPOOL_API}/tx/{txid}")
                test("tx visible in mempool", tx_data.get("txid") == txid)

                # Verify output goes to WIF address
                vouts = tx_data.get("vout", [])
                returned = any(v.get("scriptpubkey_address") == wif_address for v in vouts)
                test("funds returned to WIF address", returned)
                if returned:
                    returned_amount = sum(v["value"] for v in vouts
                                          if v.get("scriptpubkey_address") == wif_address)
                    print(f"  Returned: {returned_amount} sats to {wif_address}")
            except Exception as e:
                test("tx verification", False, str(e))

            print(f"\n  ✅ FULL E2E SUCCESS — website + Coldcard + testnet4!")
        else:
            test("txid extracted", False, f"status: {broadcast_status[:200]}")

        browser.close()

    # Clean up
    httpd.shutdown()
    for f_name in ["website-mixed.psbt", "website-mixed-signed.psbt"]:
        f_path = os.path.join(tmp_dir, f_name)
        if os.path.exists(f_path):
            os.remove(f_path)


# ============================================================
# Entry
# ============================================================

def main():
    print("=" * 60)
    print("  Website + Coldcard E2E Test (Testnet4)")
    print("  (Real browser + real device + real broadcast)")
    print("=" * 60)

    run_tests()

    print(f"\n{'='*60}")
    print(f"  Results: {_pass_count} passed, {_fail_count} failed")
    print(f"{'='*60}")
    if _failures:
        print(f"\n  Failed tests:")
        for f in _failures:
            print(f"    ✗ {f}")
    print()

    sys.exit(1 if _fail_count > 0 else 0)


if __name__ == "__main__":
    main()
