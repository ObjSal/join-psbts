#!/usr/bin/env python3
"""
Playwright test suite for the Bitcoin Transaction Signer (sign.html).

Tests PSBT loading, WIF validation, signing, download, QR display,
network selection, and all pure functions via page.evaluate().

Requires:
  - Python Playwright: pip install playwright && playwright install chromium

Usage:
    python3 tests/test_sign_html.py              # headless
    python3 tests/test_sign_html.py --headed      # visible browser
"""

import http.server
import os
import socket
import sys
import tempfile
import threading
import time
import traceback

from playwright.sync_api import sync_playwright

# ============================================================
# Configuration
# ============================================================

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TEST_DIR)
HEADED = "--headed" in sys.argv

# Known test vectors
FAKE_TXID = "a" * 64
P2WPKH_SCRIPT = "0014751e76e8199196d454941c45d1b3a323f1433bd6"


# ============================================================
# HTTP Server
# ============================================================

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_http_server(port):
    """Start a simple HTTP server in a background thread."""
    handler = http.server.SimpleHTTPRequestHandler
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


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


# ============================================================
# Helper: build a synthetic PSBT in-browser
# ============================================================

BUILD_TEST_PSBT_JS = """() => {
    const network = window._fn.getSelectedNetwork();
    const kp = window._ECPair.makeRandom({ network });
    const wif = kp.toWIF();
    const { address } = window._bitcoin.payments.p2wpkh({
        pubkey: kp.publicKey, network
    });
    const pubkeyHash = window._bitcoin.crypto.hash160(kp.publicKey);
    const scriptPubKey = window._Buffer.concat([
        window._Buffer.from([0x00, 0x14]), pubkeyHash
    ]);

    // Build PSBT with a fake UTXO
    const psbt = new window._bitcoin.Psbt({ network });
    psbt.addInput({
        hash: '""" + FAKE_TXID + """',
        index: 0,
        witnessUtxo: {
            script: scriptPubKey,
            value: 100000n,
        },
    });
    psbt.addOutput({
        address: address,
        value: 90000n,
    });
    const raw = psbt.toBuffer();
    return {
        wif,
        address,
        psbtHex: window._Buffer.from(raw).toString('hex'),
        psbtBase64: window._Buffer.from(raw).toString('base64'),
        psbtBytes: Array.from(raw),
    };
}"""


# ============================================================
# Tests
# ============================================================

def run_tests(page, base_url):
    """Run all tests against sign.html."""

    # --------------------------------------------------------
    # Setup: navigate and wait for module init
    # --------------------------------------------------------
    page.goto(base_url)
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)

    # Global dialog handler
    _all_dialogs = []
    page.on("dialog", lambda d: (_all_dialogs.append(d.message), d.accept()))

    # ========================================================
    section("1. getSelectedNetwork")
    # ========================================================

    page.select_option("#network", "mainnet")
    result = page.evaluate("() => { const n = window._fn.getSelectedNetwork(); return n.bech32; }")
    test("getSelectedNetwork mainnet", result == "bc", f"got {result}")

    page.select_option("#network", "testnet")
    result = page.evaluate("() => { const n = window._fn.getSelectedNetwork(); return n.bech32; }")
    test("getSelectedNetwork testnet", result == "tb", f"got {result}")

    page.select_option("#network", "regtest")
    result = page.evaluate("() => { const n = window._fn.getSelectedNetwork(); return n.bech32; }")
    test("getSelectedNetwork regtest", result == "bcrt", f"got {result}")

    # ========================================================
    section("2. Network URL Params")
    # ========================================================

    url_base = base_url.replace("sign.html", "")

    page.goto(f"{url_base}sign.html?network=regtest")
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)
    val = page.evaluate("() => document.getElementById('network').value")
    test("URL param network=regtest", val == "regtest", f"got {val}")

    page.goto(f"{url_base}sign.html?network=testnet")
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)
    val = page.evaluate("() => document.getElementById('network').value")
    test("URL param network=testnet", val == "testnet", f"got {val}")

    page.goto(f"{url_base}sign.html?network=mainnet&serverMode=true")
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)
    val = page.evaluate("() => document.getElementById('network').value")
    sm = page.evaluate("() => window._fn.serverMode")
    test("URL param network=mainnet&serverMode", val == "mainnet" and sm is True,
         f"got network={val}, serverMode={sm}")

    # Return to regtest for remaining tests
    page.goto(f"{url_base}sign.html?network=regtest")
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)
    _all_dialogs.clear()
    page.on("dialog", lambda d: (_all_dialogs.append(d.message), d.accept()))

    # ========================================================
    section("3. updateBackLink")
    # ========================================================

    page.select_option("#network", "regtest")
    page.evaluate("() => { window._fn.serverMode = true; window._fn.updateBackLink(); }")
    href = page.evaluate("() => document.getElementById('backToSweeper').href")
    test("back link regtest+serverMode", "network=regtest" in href and "serverMode=true" in href,
         f"got {href}")

    page.select_option("#network", "testnet")
    page.evaluate("() => { window._fn.serverMode = false; window._fn.updateBackLink(); }")
    href = page.evaluate("() => document.getElementById('backToSweeper').href")
    test("back link testnet", "network=testnet" in href and "serverMode" not in href,
         f"got {href}")

    page.select_option("#network", "mainnet")
    page.evaluate("() => window._fn.updateBackLink()")
    href = page.evaluate("() => document.getElementById('backToSweeper').href")
    test("back link mainnet", "network=mainnet" in href, f"got {href}")

    # Reset to regtest
    page.select_option("#network", "regtest")

    # ========================================================
    section("4. extractWifFromData")
    # ========================================================

    # URL with ?wif= param
    result = page.evaluate("""() => {
        return window._fn.extractWifFromData(
            'https://example.com/sweep.html?wif=cNYfRxoekiNGKWRwFnAmVT7Ea5q1WRqVbQdh1YMGiUZo8hjabiKv&network=testnet&type=taproot'
        );
    }""")
    test("extractWifFromData: URL with ?wif=",
         result is not None and result.get("wif", "").startswith("c"),
         f"got {result}")

    # URL with ?wif= and network
    test("extractWifFromData: URL extracts network",
         result.get("network") == "testnet", f"got {result}")

    # URL with ?wif= returns source
    test("extractWifFromData: URL source is paper wallet",
         result.get("source") == "paper wallet", f"got {result}")

    # Raw WIF starting with c (testnet)
    result = page.evaluate("""() => {
        return window._fn.extractWifFromData('cNYfRxoekiNGKWRwFnAmVT7Ea5q1WRqVbQdh1YMGiUZo8hjabiKv');
    }""")
    test("extractWifFromData: raw WIF (c prefix)",
         result is not None and result.get("wif", "").startswith("c"),
         f"got {result}")

    # Raw WIF starting with K (mainnet compressed)
    result = page.evaluate("""() => {
        return window._fn.extractWifFromData('KwDiBf89QgGbjEhKnhXJuH7LrciVrZi3qYjgd9M7rFU73sVHnoWn');
    }""")
    test("extractWifFromData: raw WIF (K prefix)",
         result is not None and result.get("wif", "").startswith("K"),
         f"got {result}")

    # Random garbage
    result = page.evaluate("""() => {
        return window._fn.extractWifFromData('this is not a wif');
    }""")
    test("extractWifFromData: garbage returns null",
         result is None, f"got {result}")

    # Empty string
    result = page.evaluate("""() => {
        return window._fn.extractWifFromData('');
    }""")
    test("extractWifFromData: empty returns null",
         result is None, f"got {result}")

    # ========================================================
    section("5. loadPsbtFromBytes")
    # ========================================================

    # Build a test PSBT in-browser
    test_data = page.evaluate(BUILD_TEST_PSBT_JS)
    psbt_bytes = test_data["psbtBytes"]

    # Load valid PSBT
    result = page.evaluate("""(bytes) => {
        return window._fn.loadPsbtFromBytes(new Uint8Array(bytes), 'test');
    }""", psbt_bytes)
    test("loadPsbtFromBytes: valid PSBT returns true", result is True)

    # Info displayed
    info_display = page.evaluate(
        "() => document.getElementById('psbtInfo').style.display")
    test("loadPsbtFromBytes: info visible", info_display != "none", f"got '{info_display}'")

    info_text = page.text_content("#psbtInfo")
    test("loadPsbtFromBytes: shows input/output count",
         "Inputs:" in info_text and "Outputs:" in info_text, f"got {info_text}")

    # Sign button enabled
    sign_disabled = page.evaluate("() => document.getElementById('signBtn').disabled")
    test("loadPsbtFromBytes: sign button enabled", not sign_disabled)

    # Invalid bytes
    _all_dialogs.clear()
    result = page.evaluate("""() => {
        return window._fn.loadPsbtFromBytes(new Uint8Array([1,2,3,4,5]), 'bad');
    }""")
    test("loadPsbtFromBytes: invalid bytes returns false", result is False)

    # ========================================================
    section("6. PSBT Loading via Paste")
    # ========================================================

    # Reset state
    page.evaluate("() => window._fn.clearState()")

    # Paste hex PSBT
    page.fill("#psbtPaste", test_data["psbtHex"])
    _all_dialogs.clear()
    page.click("#loadPasteBtn")
    time.sleep(0.5)
    info_vis = page.evaluate(
        "() => document.getElementById('psbtInfo').style.display")
    test("paste hex: PSBT loaded", info_vis != "none" and len(_all_dialogs) == 0,
         f"display={info_vis}, dialogs={_all_dialogs}")

    # Verify paste area cleared after success
    paste_val = page.evaluate("() => document.getElementById('psbtPaste').value")
    test("paste hex: textarea cleared", paste_val == "", f"got '{paste_val}'")

    # Reset and paste base64
    page.evaluate("() => window._fn.clearState()")
    page.fill("#psbtPaste", test_data["psbtBase64"])
    _all_dialogs.clear()
    page.click("#loadPasteBtn")
    time.sleep(0.5)
    info_vis = page.evaluate(
        "() => document.getElementById('psbtInfo').style.display")
    test("paste base64: PSBT loaded", info_vis != "none" and len(_all_dialogs) == 0,
         f"display={info_vis}, dialogs={_all_dialogs}")

    # Paste invalid data
    page.evaluate("() => window._fn.clearState()")
    page.fill("#psbtPaste", "not-valid-psbt-data")
    _all_dialogs.clear()
    page.click("#loadPasteBtn")
    time.sleep(0.5)
    test("paste invalid: shows alert", len(_all_dialogs) > 0,
         f"dialogs={_all_dialogs}")

    # ========================================================
    section("7. PSBT Loading via File Upload")
    # ========================================================

    page.evaluate("() => window._fn.clearState()")

    # Write PSBT to temp file
    tmp_dir = tempfile.mkdtemp(prefix="sign_test_")
    psbt_path = os.path.join(tmp_dir, "test.psbt")
    with open(psbt_path, "wb") as f:
        f.write(bytes(psbt_bytes))

    page.set_input_files("#psbtFileInput", [psbt_path])
    time.sleep(1)
    info_vis = page.evaluate(
        "() => document.getElementById('psbtInfo').style.display")
    test("file upload: PSBT loaded", info_vis != "none", f"display={info_vis}")

    info_text = page.text_content("#psbtInfo")
    test("file upload: shows correct counts",
         "Inputs: 1" in info_text and "Outputs: 1" in info_text,
         f"got {info_text}")

    # ========================================================
    section("8. WIF Validation")
    # ========================================================

    # Generate a regtest WIF
    wif_data = page.evaluate("""() => {
        const network = window._fn.getSelectedNetwork();
        const kp = window._ECPair.makeRandom({ network });
        const wif = kp.toWIF();
        const { address } = window._bitcoin.payments.p2wpkh({
            pubkey: kp.publicKey, network
        });
        return { wif, address };
    }""")

    # Valid regtest WIF
    page.fill("#wifInput", wif_data["wif"])
    page.dispatch_event("#wifInput", "input")
    time.sleep(0.5)
    derived = page.text_content("#derivedAddr")
    test("WIF valid: shows derived address",
         wif_data["address"] in derived, f"got {derived}")

    border_color = page.evaluate(
        "() => document.getElementById('wifInput').style.borderColor")
    test("WIF valid: green border", border_color in ("rgb(46, 204, 113)", "#2ecc71"),
         f"got {border_color}")

    # Invalid WIF
    page.fill("#wifInput", "notavalidwif")
    page.dispatch_event("#wifInput", "input")
    time.sleep(0.5)
    derived = page.text_content("#derivedAddr")
    test("WIF invalid: error message", "Invalid" in derived or "error" in derived.lower(),
         f"got {derived}")

    border_color = page.evaluate(
        "() => document.getElementById('wifInput').style.borderColor")
    test("WIF invalid: red border", border_color in ("rgb(231, 76, 60)", "#e74c3c"),
         f"got {border_color}")

    # Mainnet WIF on regtest → network mismatch
    page.fill("#wifInput", "KwDiBf89QgGbjEhKnhXJuH7LrciVrZi3qYjgd9M7rFU73sVHnoWn")
    page.dispatch_event("#wifInput", "input")
    time.sleep(0.5)
    derived = page.text_content("#derivedAddr")
    test("WIF mismatch: warning shown", "different network" in derived.lower(),
         f"got {derived}")

    # Empty WIF clears derived address
    page.fill("#wifInput", "")
    page.dispatch_event("#wifInput", "input")
    time.sleep(0.3)
    derived_vis = page.evaluate(
        "() => document.getElementById('derivedAddr').style.display")
    test("WIF empty: hides derived area",
         derived_vis == "none" or page.text_content("#derivedAddr").strip() == "",
         f"display={derived_vis}")

    # ========================================================
    section("9. Signing")
    # ========================================================

    # Build a fresh PSBT+WIF pair where the WIF matches the PSBT input
    page.evaluate("() => window._fn.clearState()")
    test_data2 = page.evaluate(BUILD_TEST_PSBT_JS)

    # Load PSBT
    page.evaluate("""(bytes) => {
        window._fn.loadPsbtFromBytes(new Uint8Array(bytes), 'test');
    }""", test_data2["psbtBytes"])

    # Sign button starts enabled
    sign_disabled = page.evaluate("() => document.getElementById('signBtn').disabled")
    test("sign: button enabled after load", not sign_disabled)

    # Enter matching WIF and sign
    page.fill("#wifInput", test_data2["wif"])
    page.dispatch_event("#wifInput", "input")
    time.sleep(0.5)
    page.click("#signBtn")
    time.sleep(1)

    result_vis = page.evaluate(
        "() => document.getElementById('signResult').style.display")
    test("sign: result visible", result_vis != "none", f"display={result_vis}")

    result_text = page.text_content("#signResult")
    test("sign: signed 1 of 1", "Signed 1 of 1" in result_text, f"got {result_text}")

    # Output card visible
    output_vis = page.evaluate(
        "() => document.getElementById('outputCard').style.display")
    test("sign: output card visible", output_vis != "none", f"display={output_vis}")

    # Signed hex present
    signed_hex = page.text_content("#signedPsbtHex")
    test("sign: hex output present", len(signed_hex.strip()) > 0 and "70736274ff" in signed_hex,
         f"length={len(signed_hex.strip())}")

    # Non-matching WIF → warning
    page.evaluate("() => window._fn.clearState()")
    page.evaluate("""(bytes) => {
        window._fn.loadPsbtFromBytes(new Uint8Array(bytes), 'test');
    }""", test_data2["psbtBytes"])

    # Generate a different keypair
    other_wif = page.evaluate("""() => {
        const kp = window._ECPair.makeRandom({ network: window._fn.getSelectedNetwork() });
        return kp.toWIF();
    }""")
    page.fill("#wifInput", other_wif)
    page.dispatch_event("#wifInput", "input")
    time.sleep(0.5)
    page.click("#signBtn")
    time.sleep(1)

    result_text = page.text_content("#signResult")
    test("sign non-matching: warning shown",
         "doesn't match" in result_text.lower() or "0" in result_text,
         f"got {result_text}")

    # ========================================================
    section("10. Download")
    # ========================================================

    # Re-sign with matching key for download test
    page.evaluate("() => window._fn.clearState()")
    page.evaluate("""(bytes) => {
        window._fn.loadPsbtFromBytes(new Uint8Array(bytes), 'test');
    }""", test_data2["psbtBytes"])
    page.fill("#wifInput", test_data2["wif"])
    page.dispatch_event("#wifInput", "input")
    time.sleep(0.5)
    page.click("#signBtn")
    time.sleep(1)

    with page.expect_download(timeout=10000) as dl_info:
        page.click("#downloadBtn")
    dl = dl_info.value
    test("download: file created", dl is not None)

    with open(dl.path(), "rb") as f:
        dl_bytes = f.read()
    test("download: PSBT magic header", dl_bytes[:5] == b"psbt\xff",
         f"got {dl_bytes[:5].hex()}")

    # ========================================================
    section("11. QR Code Display")
    # ========================================================

    # QR display should be hidden initially
    qr_vis = page.evaluate(
        "() => document.getElementById('qrDisplay').style.display")
    test("QR: hidden initially", qr_vis == "none", f"display={qr_vis}")

    # Show QR
    page.click("#showQrBtn")
    time.sleep(1)
    qr_vis = page.evaluate(
        "() => document.getElementById('qrDisplay').style.display")
    test("QR: visible after click", qr_vis != "none", f"display={qr_vis}")

    btn_text = page.text_content("#showQrBtn")
    test("QR: button text says Hide", "Hide" in btn_text, f"got {btn_text}")

    # Hide QR
    page.click("#showQrBtn")
    time.sleep(0.5)
    qr_vis = page.evaluate(
        "() => document.getElementById('qrDisplay').style.display")
    test("QR: hidden after toggle", qr_vis == "none", f"display={qr_vis}")

    # ========================================================
    section("12. clearState")
    # ========================================================

    # First load a PSBT so there's state to clear
    page.evaluate("""(bytes) => {
        window._fn.loadPsbtFromBytes(new Uint8Array(bytes), 'test');
    }""", test_data2["psbtBytes"])
    page.evaluate("() => window._fn.clearState()")

    # loadedPsbt should be null
    is_null = page.evaluate("() => window._fn.loadedPsbt === null")
    test("clearState: loadedPsbt null", is_null)

    # PSBT info hidden
    info_vis = page.evaluate(
        "() => document.getElementById('psbtInfo').style.display")
    test("clearState: info hidden", info_vis == "none", f"got {info_vis}")

    # Sign button disabled
    sign_disabled = page.evaluate("() => document.getElementById('signBtn').disabled")
    test("clearState: sign button disabled", sign_disabled)

    # Clean up temp files
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# Main
# ============================================================

def main():
    port = find_free_port()
    os.chdir(_PROJECT_ROOT)
    httpd = start_http_server(port)
    base_url = f"http://127.0.0.1:{port}/sign.html"

    print(f"Server started at http://127.0.0.1:{port}")
    print(f"Mode: {'headed' if HEADED else 'headless'}\n")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not HEADED)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            # Enable test mode
            page.add_init_script("window.__TEST_MODE__ = true;")

            run_tests(page, base_url)

            browser.close()
    except Exception:
        traceback.print_exc()
    finally:
        httpd.shutdown()

    # Summary
    print(f"\n{'='*60}")
    print(f"  RESULTS: {_pass_count} passed, {_fail_count} failed")
    print(f"{'='*60}")
    if _failures:
        print("\n  Failed tests:")
        for f in _failures:
            print(f"    ✗ {f}")
    print()

    sys.exit(1 if _fail_count > 0 else 0)


if __name__ == "__main__":
    main()
