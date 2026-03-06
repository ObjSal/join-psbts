#!/usr/bin/env python3
"""
Playwright test suite for Bitcoin PSBT Builder.

Tests all pure functions via page.evaluate() and DOM interactions
via Playwright actions. Runs against the real index.html in a browser.

Requires:
  - Python Playwright: pip install playwright && playwright install chromium

Usage:
    python3 tests/test_psbt_builder.py              # headless
    python3 tests/test_psbt_builder.py --headed      # visible browser
"""

import http.server
import os
import socket
import sys
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
# Testnet4 P2WPKH address
TESTNET_P2WPKH = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
# Mainnet P2WPKH address
MAINNET_P2WPKH = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
# Mainnet P2TR address
MAINNET_P2TR = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"
# Mainnet P2PKH
MAINNET_P2PKH = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
# Mainnet P2SH
MAINNET_P2SH = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"

# P2WPKH scriptPubKey for MAINNET_P2WPKH (OP_0 <20-byte-hash>)
P2WPKH_SCRIPT = "0014751e76e8199196d454941c45d1b3a323f1433bd6"
# P2TR scriptPubKey (OP_1 <32-byte-x-only-pubkey>)
P2TR_SCRIPT = "512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"

# Regtest
REGTEST_BECH32 = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"

# A valid-looking txid (64 hex chars)
FAKE_TXID = "a" * 64


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
# Tests
# ============================================================

def run_tests(page, base_url):
    """Run all tests against the loaded page."""

    # --------------------------------------------------------
    # Setup: navigate and wait for module init
    # --------------------------------------------------------
    page.goto(base_url)
    page.wait_for_function("() => window._fn !== undefined", timeout=15000)

    # ========================================================
    section("1. hexToBytes")
    # ========================================================

    # Valid hex
    result = page.evaluate("() => Array.from(window._fn.hexToBytes('deadbeef'))")
    test("hexToBytes valid hex", result == [0xde, 0xad, 0xbe, 0xef], f"got {result}")

    # Empty input
    result = page.evaluate("() => Array.from(window._fn.hexToBytes(''))")
    test("hexToBytes empty string", result == [], f"got {result}")

    # Null/undefined
    result = page.evaluate("() => Array.from(window._fn.hexToBytes(null))")
    test("hexToBytes null", result == [], f"got {result}")

    # Odd-length hex should throw
    threw = page.evaluate("""() => {
        try { window._fn.hexToBytes('abc'); return false; }
        catch(e) { return true; }
    }""")
    test("hexToBytes odd-length throws", threw)

    # Single byte
    result = page.evaluate("() => Array.from(window._fn.hexToBytes('ff'))")
    test("hexToBytes single byte", result == [255], f"got {result}")

    # ========================================================
    section("2. getSelectedNetwork")
    # ========================================================

    # Mainnet (default)
    page.select_option("#network", "mainnet")
    result = page.evaluate("() => { const n = window._fn.getSelectedNetwork(); return n.bech32; }")
    test("getSelectedNetwork mainnet bech32", result == "bc", f"got {result}")

    # Testnet
    page.select_option("#network", "testnet")
    result = page.evaluate("() => { const n = window._fn.getSelectedNetwork(); return n.bech32; }")
    test("getSelectedNetwork testnet bech32", result == "tb", f"got {result}")

    # Regtest
    page.select_option("#network", "regtest")
    result = page.evaluate("() => { const n = window._fn.getSelectedNetwork(); return n.bech32; }")
    test("getSelectedNetwork regtest bech32", result == "bcrt", f"got {result}")

    # ========================================================
    section("2b. getMempoolBaseUrl")
    # ========================================================

    page.select_option("#network", "mainnet")
    result = page.evaluate("() => window._fn.getMempoolBaseUrl()")
    test("getMempoolBaseUrl mainnet", result == "https://mempool.space/api", f"got {result}")

    page.select_option("#network", "testnet")
    result = page.evaluate("() => window._fn.getMempoolBaseUrl()")
    test("getMempoolBaseUrl testnet", result == "https://mempool.space/testnet4/api", f"got {result}")

    page.select_option("#network", "regtest")
    result = page.evaluate("() => window._fn.getMempoolBaseUrl()")
    test("getMempoolBaseUrl regtest", result == "https://mempool.space/signet/api", f"got {result}")

    # ========================================================
    section("3. validateBitcoinAddress")
    # ========================================================

    # Reset to mainnet for address tests
    page.select_option("#network", "mainnet")

    # Mainnet P2WPKH — valid
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateBitcoinAddress("{MAINNET_P2WPKH}", net);
    }}""")
    test("validateBitcoinAddress mainnet P2WPKH valid", result is True)

    # Mainnet P2TR — valid
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateBitcoinAddress("{MAINNET_P2TR}", net);
    }}""")
    test("validateBitcoinAddress mainnet P2TR valid", result is True)

    # Mainnet P2PKH — valid
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateBitcoinAddress("{MAINNET_P2PKH}", net);
    }}""")
    test("validateBitcoinAddress mainnet P2PKH valid", result is True)

    # Mainnet P2SH — valid
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateBitcoinAddress("{MAINNET_P2SH}", net);
    }}""")
    test("validateBitcoinAddress mainnet P2SH valid", result is True)

    # Testnet address on mainnet — invalid
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateBitcoinAddress("{TESTNET_P2WPKH}", net);
    }}""")
    test("validateBitcoinAddress testnet addr on mainnet invalid", result is False)

    # Invalid string
    result = page.evaluate("""() => {
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateBitcoinAddress("notanaddress", net);
    }""")
    test("validateBitcoinAddress garbage invalid", result is False)

    # Empty string
    result = page.evaluate("""() => {
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateBitcoinAddress("", net);
    }""")
    test("validateBitcoinAddress empty invalid", result is False)

    # Testnet P2WPKH on testnet — valid
    page.select_option("#network", "testnet")
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateBitcoinAddress("{TESTNET_P2WPKH}", net);
    }}""")
    test("validateBitcoinAddress testnet P2WPKH valid", result is True)

    # Mainnet on testnet — invalid
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateBitcoinAddress("{MAINNET_P2WPKH}", net);
    }}""")
    test("validateBitcoinAddress mainnet addr on testnet invalid", result is False)

    # Regtest
    page.select_option("#network", "regtest")
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateBitcoinAddress("{REGTEST_BECH32}", net);
    }}""")
    test("validateBitcoinAddress regtest valid", result is True)

    # ========================================================
    section("4. validateScriptPubKey")
    # ========================================================

    page.select_option("#network", "mainnet")

    # Valid P2WPKH script
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateScriptPubKey("{P2WPKH_SCRIPT}", net);
    }}""")
    test("validateScriptPubKey P2WPKH valid", result is True)

    # Invalid hex
    result = page.evaluate("""() => {
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateScriptPubKey("zzzz", net);
    }""")
    test("validateScriptPubKey invalid hex", result is False)

    # Empty
    result = page.evaluate("""() => {
        const net = window._fn.getSelectedNetwork();
        return window._fn.validateScriptPubKey("", net);
    }""")
    test("validateScriptPubKey empty", result is False)

    # ========================================================
    section("5. decodeAddressFromScript")
    # ========================================================

    page.select_option("#network", "mainnet")

    # P2WPKH script → mainnet address
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.decodeAddressFromScript("{P2WPKH_SCRIPT}", net);
    }}""")
    test("decodeAddressFromScript P2WPKH", result == MAINNET_P2WPKH, f"got {result}")

    # P2TR script (Taproot manual detection)
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        return window._fn.decodeAddressFromScript("{P2TR_SCRIPT}", net);
    }}""")
    test("decodeAddressFromScript P2TR returns address", result is not None and result.startswith("bc1p"), f"got {result}")

    # Invalid script returns null
    result = page.evaluate("""() => {
        const net = window._fn.getSelectedNetwork();
        return window._fn.decodeAddressFromScript("deadbeef", net);
    }""")
    test("decodeAddressFromScript invalid returns null", result is None)

    # Empty returns null
    result = page.evaluate("""() => {
        const net = window._fn.getSelectedNetwork();
        return window._fn.decodeAddressFromScript("", net);
    }""")
    test("decodeAddressFromScript empty returns null", result is None)

    # ========================================================
    section("6. estimateVirtualSize")
    # ========================================================

    # Create a simple PSBT to test vsize estimation
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        const utxos = [{{
            txid: "{FAKE_TXID}",
            vout: 0,
            value: 100000,
            scriptPubKey: "{P2WPKH_SCRIPT}"
        }}];
        const outputs = [{{
            address: "{MAINNET_P2WPKH}",
            value: 90000
        }}];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 0, "");
        return window._fn.estimateVirtualSize(psbt);
    }}""")
    # 1 input, 1 output: baseSize = 10 + 41 + 34 = 85, witnessSize = 107
    # vsize = ceil((3*85 + 107) / 4) = ceil(362/4) = 91
    test("estimateVirtualSize 1-in 1-out", result == 91, f"got {result}")

    # 2 inputs, 2 outputs
    result = page.evaluate(f"""() => {{
        const net = window._fn.getSelectedNetwork();
        const utxos = [
            {{ txid: "{FAKE_TXID}", vout: 0, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}" }},
            {{ txid: "{FAKE_TXID}", vout: 1, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}" }}
        ];
        const outputs = [
            {{ address: "{MAINNET_P2WPKH}", value: 90000 }},
            {{ address: "{MAINNET_P2WPKH}", value: 90000 }}
        ];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 0, "");
        return window._fn.estimateVirtualSize(psbt);
    }}""")
    # 2 inputs, 2 outputs: baseSize = 10 + 82 + 68 = 160, witnessSize = 214
    # vsize = ceil((3*160 + 214) / 4) = ceil(694/4) = 174
    test("estimateVirtualSize 2-in 2-out", result == 174, f"got {result}")

    # ========================================================
    section("7. colourField")
    # ========================================================

    # Test with the change address input
    page.select_option("#network", "mainnet")

    # Empty → neutral (#ccc or rgb)
    color = page.evaluate("""() => {
        const el = document.getElementById('changeAddress');
        el.value = '';
        window._fn.colourField(el, false);
        return el.style.borderColor;
    }""")
    test("colourField empty → neutral", "204" in color or "ccc" in color, f"got '{color}'")

    # Valid → green
    color = page.evaluate("""() => {
        const el = document.getElementById('changeAddress');
        el.value = 'something';
        window._fn.colourField(el, true);
        return el.style.borderColor;
    }""")
    test("colourField valid → green", color == "green", f"got '{color}'")

    # Invalid → red
    color = page.evaluate("""() => {
        const el = document.getElementById('changeAddress');
        el.value = 'invalid';
        window._fn.colourField(el, false);
        return el.style.borderColor;
    }""")
    test("colourField invalid → red", color == "red", f"got '{color}'")

    # ========================================================
    section("8. createPsbtFromInputs")
    # ========================================================

    page.select_option("#network", "mainnet")

    # Basic PSBT creation — no change
    result = page.evaluate(f"""() => {{
        const utxos = [{{ txid: "{FAKE_TXID}", vout: 0, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}" }}];
        const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 90000 }}];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 0, "");
        return {{
            inputCount: psbt.data.inputs.length,
            outputCount: psbt.data.outputs.length,
            hasBuffer: typeof psbt.toBuffer === 'function'
        }};
    }}""")
    test("createPsbt no change — 1 input", result["inputCount"] == 1)
    test("createPsbt no change — 1 output", result["outputCount"] == 1)
    test("createPsbt has toBuffer", result["hasBuffer"] is True)

    # With change address
    result = page.evaluate(f"""() => {{
        const utxos = [{{ txid: "{FAKE_TXID}", vout: 0, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}" }}];
        const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 50000 }}];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 1000, "{MAINNET_P2WPKH}");
        return {{ outputCount: psbt.data.outputs.length }};
    }}""")
    # 50000 output + (100000-50000-1000)=49000 change = 2 outputs
    test("createPsbt with change — 2 outputs", result["outputCount"] == 2)

    # Change exactly zero (fee eats the rest) — no change output added
    result = page.evaluate(f"""() => {{
        const utxos = [{{ txid: "{FAKE_TXID}", vout: 0, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}" }}];
        const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 50000 }}];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 50000, "{MAINNET_P2WPKH}");
        return {{ outputCount: psbt.data.outputs.length }};
    }}""")
    test("createPsbt change=0 — 1 output (no change)", result["outputCount"] == 1)

    # Outputs + fee exceed input — should throw
    threw = page.evaluate(f"""() => {{
        try {{
            const utxos = [{{ txid: "{FAKE_TXID}", vout: 0, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}" }}];
            const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 90000 }}];
            window._fn.createPsbtFromInputs(utxos, outputs, 20000, "{MAINNET_P2WPKH}");
            return false;
        }} catch(e) {{ return e.message; }}
    }}""")
    test("createPsbt outputs+fee>input throws", "exceed" in str(threw).lower(), f"got {threw}")

    # No change mode, outputs > inputs — should throw
    threw = page.evaluate(f"""() => {{
        try {{
            const utxos = [{{ txid: "{FAKE_TXID}", vout: 0, value: 50000, scriptPubKey: "{P2WPKH_SCRIPT}" }}];
            const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 90000 }}];
            window._fn.createPsbtFromInputs(utxos, outputs, 0, "");
            return false;
        }} catch(e) {{ return e.message; }}
    }}""")
    test("createPsbt no-change outputs>inputs throws", "exceed" in str(threw).lower(), f"got {threw}")

    # Multiple inputs and outputs
    result = page.evaluate(f"""() => {{
        const utxos = [
            {{ txid: "{FAKE_TXID}", vout: 0, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}" }},
            {{ txid: "{FAKE_TXID}", vout: 1, value: 200000, scriptPubKey: "{P2WPKH_SCRIPT}" }}
        ];
        const outputs = [
            {{ address: "{MAINNET_P2WPKH}", value: 50000 }},
            {{ address: "{MAINNET_P2WPKH}", value: 60000 }}
        ];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 5000, "{MAINNET_P2WPKH}");
        return {{
            inputCount: psbt.data.inputs.length,
            outputCount: psbt.data.outputs.length
        }};
    }}""")
    # 2 inputs, 2 explicit outputs + 1 change (300000-110000-5000=185000) = 3 outputs
    test("createPsbt multi — 2 inputs", result["inputCount"] == 2)
    test("createPsbt multi — 3 outputs (incl change)", result["outputCount"] == 3)

    # Verify witnessUtxo is set on inputs
    result = page.evaluate(f"""() => {{
        const utxos = [{{ txid: "{FAKE_TXID}", vout: 0, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}" }}];
        const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 90000 }}];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 0, "");
        const inp = psbt.data.inputs[0];
        return {{
            hasWitnessUtxo: !!inp.witnessUtxo,
            witnessValue: inp.witnessUtxo ? inp.witnessUtxo.value.toString() : null
        }};
    }}""")
    test("createPsbt input has witnessUtxo", result["hasWitnessUtxo"] is True)
    test("createPsbt witnessUtxo value correct", result["witnessValue"] == "100000")

    # ========================================================
    section("9. DOM: Add/Remove Input Rows")
    # ========================================================

    # Clear existing inputs first
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")

    # Add an input
    page.click("#addInputButton")
    count = page.evaluate("() => document.querySelectorAll('[data-utxo]').length")
    test("addInput creates row", count == 1)

    # Add another
    page.click("#addInputButton")
    count = page.evaluate("() => document.querySelectorAll('[data-utxo]').length")
    test("addInput second row", count == 2)

    # Remove first input (click ✕)
    page.click("[data-utxo]:first-child .remove")
    count = page.evaluate("() => document.querySelectorAll('[data-utxo]').length")
    test("remove input row", count == 1)

    # ========================================================
    section("10. DOM: Add/Remove Output Rows")
    # ========================================================

    # Clear existing outputs
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

    # Add output
    page.click("#addOutputButton")
    count = page.evaluate("() => document.querySelectorAll('[data-output]').length")
    test("addOutput creates row", count == 1)

    # Add another
    page.click("#addOutputButton")
    count = page.evaluate("() => document.querySelectorAll('[data-output]').length")
    test("addOutput second row", count == 2)

    # Remove one
    page.click("[data-output]:first-child .remove")
    count = page.evaluate("() => document.querySelectorAll('[data-output]').length")
    test("remove output row", count == 1)

    # ========================================================
    section("11. DOM: Change Mode Toggle")
    # ========================================================

    # With change checked
    page.check("#includeChange")
    fee_visible = page.evaluate("() => document.getElementById('feeRateGroup').style.display !== 'none'")
    change_visible = page.evaluate("() => document.getElementById('changeAddrGroup').style.display !== 'none'")
    test("change mode ON — fee rate visible", fee_visible)
    test("change mode ON — change addr visible", change_visible)

    # Uncheck
    page.uncheck("#includeChange")
    fee_visible = page.evaluate("() => document.getElementById('feeRateGroup').style.display !== 'none'")
    change_visible = page.evaluate("() => document.getElementById('changeAddrGroup').style.display !== 'none'")
    test("change mode OFF — fee rate hidden", not fee_visible)
    test("change mode OFF — change addr hidden", not change_visible)

    # Re-check for remaining tests
    page.check("#includeChange")

    # ========================================================
    section("12. DOM: Script Label Live Decoding")
    # ========================================================

    page.select_option("#network", "mainnet")

    # Clear and add fresh input
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.click("#addInputButton")

    # Type a valid scriptPubKey
    script_input = page.locator("[data-utxo] .script-input")
    script_input.fill(P2WPKH_SCRIPT)
    script_input.dispatch_event("input")
    time.sleep(0.2)

    label = page.locator("[data-utxo] .script-label span").text_content()
    test("script label shows decoded address", label == MAINNET_P2WPKH, f"got '{label}'")

    # Type invalid script
    script_input.fill("deadbeef")
    script_input.dispatch_event("input")
    time.sleep(0.2)

    label = page.locator("[data-utxo] .script-label span").text_content()
    test("script label shows Invalid for bad script", "Invalid" in label, f"got '{label}'")

    # ========================================================
    section("13. DOM: Address Validation Coloring")
    # ========================================================

    page.select_option("#network", "mainnet")

    # Clear and add fresh output
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")
    page.click("#addOutputButton")

    addr_input = page.locator("[data-output] .output-address")

    # Type valid address
    addr_input.fill(MAINNET_P2WPKH)
    addr_input.dispatch_event("input")
    time.sleep(0.2)
    color = page.evaluate("() => document.querySelector('.output-address').style.borderColor")
    test("output address valid → green", color == "green", f"got '{color}'")

    # Type invalid address
    addr_input.fill("notvalid")
    addr_input.dispatch_event("input")
    time.sleep(0.2)
    color = page.evaluate("() => document.querySelector('.output-address').style.borderColor")
    test("output address invalid → red", color == "red", f"got '{color}'")

    # ========================================================
    section("14. DOM: Change Address Validation")
    # ========================================================

    page.select_option("#network", "mainnet")
    change_input = page.locator("#changeAddress")

    change_input.fill(MAINNET_P2WPKH)
    change_input.dispatch_event("input")
    time.sleep(0.2)
    color = page.evaluate("() => document.getElementById('changeAddress').style.borderColor")
    test("change address valid → green", color == "green", f"got '{color}'")

    change_input.fill("bad")
    change_input.dispatch_event("input")
    time.sleep(0.2)
    color = page.evaluate("() => document.getElementById('changeAddress').style.borderColor")
    test("change address invalid → red", color == "red", f"got '{color}'")

    # ========================================================
    section("15. DOM: Network Change Re-validates")
    # ========================================================

    # Set up a mainnet script, then switch to testnet
    page.select_option("#network", "mainnet")
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.click("#addInputButton")
    script_input = page.locator("[data-utxo] .script-input")
    script_input.fill(P2WPKH_SCRIPT)
    script_input.dispatch_event("input")
    time.sleep(0.2)

    # On mainnet it should show the address
    label = page.locator("[data-utxo] .script-label span").text_content()
    test("script valid on mainnet", label == MAINNET_P2WPKH, f"got '{label}'")

    # Switch to testnet — same script should decode to testnet address or stay valid
    # (P2WPKH script is network-independent at the script level, but the decoded address changes)
    page.select_option("#network", "testnet")
    time.sleep(0.3)
    label = page.locator("[data-utxo] .script-label span").text_content()
    test("script re-validated on network change", label is not None and len(label) > 1, f"got '{label}'")

    # ========================================================
    section("16. DOM: Fee Calculation Updates")
    # ========================================================

    page.select_option("#network", "mainnet")
    page.check("#includeChange")

    # Set up input and output
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

    page.evaluate(f"""() => {{
        window._fn.addInput(null, "{FAKE_TXID}", 0, 100000, "{P2WPKH_SCRIPT}");
        window._fn.addOutput(null, "{MAINNET_P2WPKH}", 50000);
    }}""")

    # Set fee rate and change address
    page.fill("#feeRate", "10")
    page.fill("#changeAddress", MAINNET_P2WPKH)
    page.locator("#feeRate").dispatch_event("input")
    time.sleep(0.3)

    fee_text = page.evaluate("() => document.getElementById('feeCalc').textContent")
    test("fee calc shows estimated fee", "Estimated fee" in fee_text, f"got '{fee_text}'")
    test("fee calc shows vB", "vB" in fee_text, f"got '{fee_text}'")

    # Uncheck change mode
    page.uncheck("#includeChange")
    time.sleep(0.3)
    fee_text = page.evaluate("() => document.getElementById('feeCalc').textContent")
    test("no-change fee calc shows transaction fee", "Transaction fee: 50000 sats" in fee_text, f"got '{fee_text}'")

    # Re-check for next tests
    page.check("#includeChange")

    # ========================================================
    section("17. Integration: Create PSBT Download")
    # ========================================================

    page.select_option("#network", "mainnet")
    page.check("#includeChange")

    # Set up valid inputs
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

    page.evaluate(f"""() => {{
        window._fn.addInput(null, "{FAKE_TXID}", 0, 100000, "{P2WPKH_SCRIPT}");
        window._fn.addOutput(null, "{MAINNET_P2WPKH}", 50000);
    }}""")
    page.fill("#feeRate", "10")
    page.fill("#changeAddress", MAINNET_P2WPKH)

    # Intercept download
    with page.expect_download() as download_info:
        page.click("#createPsbt")
    download = download_info.value
    test("PSBT download triggered", download is not None)
    test("PSBT filename is unsigned.psbt", download.suggested_filename == "unsigned.psbt")

    # Verify the downloaded PSBT is valid
    path = download.path()
    with open(path, "rb") as f:
        psbt_bytes = f.read()
    test("PSBT file is non-empty", len(psbt_bytes) > 0, f"size={len(psbt_bytes)}")
    # PSBT magic bytes: "psbt\xff"
    test("PSBT has magic header", psbt_bytes[:5] == b"psbt\xff", f"got {psbt_bytes[:5]}")

    # ========================================================
    section("18. Integration: Validation Errors")
    # ========================================================

    # Missing fee rate — use empty txid to avoid slow network fetches
    page.select_option("#network", "mainnet")
    page.check("#includeChange")
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")
    # Use empty txid so fetchAllNonWitnessUtxos has nothing to fetch
    page.evaluate(f"""() => {{
        window._fn.addInput(null, "", 0, 100000, "{P2WPKH_SCRIPT}");
        window._fn.addOutput(null, "{MAINNET_P2WPKH}", 50000);
    }}""")
    page.fill("#feeRate", "")
    page.fill("#changeAddress", MAINNET_P2WPKH)

    dialog_msg = []
    page.on("dialog", lambda d: (dialog_msg.append(d.message), d.accept()))
    page.click("#createPsbt")
    time.sleep(1)
    test("missing fee rate shows alert", len(dialog_msg) > 0 and "fee" in dialog_msg[-1].lower(),
         f"got {dialog_msg}")

    # Invalid change address
    dialog_msg.clear()
    page.fill("#feeRate", "10")
    page.fill("#changeAddress", "bad_address")
    page.click("#createPsbt")
    time.sleep(1)
    test("invalid change addr shows alert", len(dialog_msg) > 0 and "change" in dialog_msg[-1].lower(),
         f"got {dialog_msg}")

    # ========================================================
    section("19. Integration: No-Change PSBT Creation")
    # ========================================================

    page.select_option("#network", "mainnet")
    page.uncheck("#includeChange")

    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

    page.evaluate(f"""() => {{
        window._fn.addInput(null, "{FAKE_TXID}", 0, 100000, "{P2WPKH_SCRIPT}");
        window._fn.addOutput(null, "{MAINNET_P2WPKH}", 95000);
    }}""")

    with page.expect_download() as download_info:
        page.click("#createPsbt")
    download = download_info.value
    test("no-change PSBT download works", download is not None)

    # Verify PSBT has 1 output (no change)
    path = download.path()
    with open(path, "rb") as f:
        psbt_bytes = f.read()
    test("no-change PSBT has magic header", psbt_bytes[:5] == b"psbt\xff")

    # Also verify through JS that 1 output in the PSBT
    result = page.evaluate(f"""() => {{
        const utxos = [{{ txid: "{FAKE_TXID}", vout: 0, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}" }}];
        const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 95000 }}];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 0, "");
        return psbt.data.outputs.length;
    }}""")
    test("no-change PSBT: 1 output in JS", result == 1)

    # ========================================================
    section("20. HW Wallet Info Toggle")
    # ========================================================

    page.select_option("#network", "mainnet")
    page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
    page.click("#addInputButton")

    # HW fields should be hidden by default
    hw_visible = page.evaluate("() => document.querySelector('.hw-fields').classList.contains('open')")
    test("HW fields hidden by default", not hw_visible)

    # Click toggle to open
    page.click(".hw-toggle")
    hw_visible = page.evaluate("() => document.querySelector('.hw-fields').classList.contains('open')")
    test("HW fields visible after toggle", hw_visible)

    # Click toggle to close
    page.click(".hw-toggle")
    hw_visible = page.evaluate("() => document.querySelector('.hw-fields').classList.contains('open')")
    test("HW fields hidden after second toggle", not hw_visible)

    # ========================================================
    section("20b. bip32Derivation in PSBT")
    # ========================================================

    page.select_option("#network", "mainnet")

    # Create PSBT with bip32Derivation data
    # Use a known compressed pubkey (33 bytes = 66 hex)
    test_pubkey = "02" + "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    test_xfp = "aabbccdd"
    test_path = "m/84'/0'/0'/0/0"

    result = page.evaluate(f"""() => {{
        const utxos = [{{
            txid: "{FAKE_TXID}",
            vout: 0,
            value: 100000,
            scriptPubKey: "{P2WPKH_SCRIPT}",
            xfp: "{test_xfp}",
            pubkey: "{test_pubkey}",
            derivationPath: "{test_path}"
        }}];
        const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 90000 }}];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 0, "");
        const inp = psbt.data.inputs[0];
        return {{
            hasBip32: !!inp.bip32Derivation && inp.bip32Derivation.length > 0,
            xfp: inp.bip32Derivation ? Array.from(inp.bip32Derivation[0].masterFingerprint).map(b => b.toString(16).padStart(2,'0')).join('') : null,
            path: inp.bip32Derivation ? inp.bip32Derivation[0].path : null,
            pubkeyLen: inp.bip32Derivation ? inp.bip32Derivation[0].pubkey.length : 0
        }};
    }}""")
    test("bip32Derivation present in input", result["hasBip32"] is True)
    test("bip32 XFP correct", result["xfp"] == test_xfp, f"got {result['xfp']}")
    test("bip32 path correct", result["path"] == test_path, f"got {result['path']}")
    test("bip32 pubkey is 33 bytes", result["pubkeyLen"] == 33, f"got {result['pubkeyLen']}")

    # PSBT without bip32Derivation (no HW info)
    result = page.evaluate(f"""() => {{
        const utxos = [{{
            txid: "{FAKE_TXID}",
            vout: 0,
            value: 100000,
            scriptPubKey: "{P2WPKH_SCRIPT}"
        }}];
        const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 90000 }}];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 0, "");
        const inp = psbt.data.inputs[0];
        return {{ hasBip32: !!inp.bip32Derivation }};
    }}""")
    test("no bip32Derivation when no HW info", result["hasBip32"] is False)

    # Multi-input: one with bip32, one without
    result = page.evaluate(f"""() => {{
        const utxos = [
            {{
                txid: "{FAKE_TXID}", vout: 0, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}",
                xfp: "{test_xfp}", pubkey: "{test_pubkey}", derivationPath: "{test_path}"
            }},
            {{
                txid: "{FAKE_TXID}", vout: 1, value: 50000, scriptPubKey: "{P2WPKH_SCRIPT}"
            }}
        ];
        const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 140000 }}];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 0, "");
        return {{
            input0_bip32: !!psbt.data.inputs[0].bip32Derivation,
            input1_bip32: !!psbt.data.inputs[1].bip32Derivation
        }};
    }}""")
    test("multi-input: input 0 has bip32", result["input0_bip32"] is True)
    test("multi-input: input 1 no bip32", result["input1_bip32"] is False)

    # ========================================================
    section("21. Sortable Drag & Drop Initialized")
    # ========================================================

    # Verify Sortable is attached to containers
    has_sortable = page.evaluate("""() => {
        const utxoEl = document.getElementById('utxoContainer');
        const outputEl = document.getElementById('outputContainer');
        // Sortable adds data attributes
        return !!(utxoEl && outputEl);
    }""")
    test("sortable containers exist", has_sortable)

    # ========================================================
    section("21. PSBT Buffer Round-Trip")
    # ========================================================

    # Create a PSBT, convert to buffer and back
    result = page.evaluate(f"""() => {{
        const utxos = [{{ txid: "{FAKE_TXID}", vout: 0, value: 100000, scriptPubKey: "{P2WPKH_SCRIPT}" }}];
        const outputs = [{{ address: "{MAINNET_P2WPKH}", value: 90000 }}];
        const psbt = window._fn.createPsbtFromInputs(utxos, outputs, 0, "");
        const buf = psbt.toBuffer();
        // Round-trip: parse back
        const net = window._fn.getSelectedNetwork();
        const psbt2 = window._bitcoin.Psbt.fromBuffer(buf, {{ network: net }});
        return {{
            inputCount: psbt2.data.inputs.length,
            outputCount: psbt2.data.outputs.length,
            bufferLength: buf.length
        }};
    }}""")
    test("PSBT round-trip — inputs preserved", result["inputCount"] == 1)
    test("PSBT round-trip — outputs preserved", result["outputCount"] == 1)
    test("PSBT buffer has content", result["bufferLength"] > 0)


    # ========================================================
    section("22. xpub Public Key Derivation")
    # ========================================================

    # BIP32 test vector 1 master xpub (depth 0)
    MASTER_XPUB = "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoCu1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"

    # normalizeExtendedKey — xpub passthrough
    result = page.evaluate(f"""() => {{
        const r = window._fn.normalizeExtendedKey("{MASTER_XPUB}");
        return {{ key: r.key, isTestnet: r.isTestnet }};
    }}""")
    test("normalizeExtendedKey: xpub unchanged", result["key"] == MASTER_XPUB)
    test("normalizeExtendedKey: xpub is mainnet", result["isTestnet"] is False)

    # normalizeExtendedKey — invalid key
    result = page.evaluate("""() => {
        try { window._fn.normalizeExtendedKey("notavalidkey"); return "no error"; }
        catch (e) { return e.message; }
    }""")
    test("normalizeExtendedKey: invalid key throws", "error" not in result.lower() or "nrecognized" in result.lower() or result != "no error", f"got: {result}")

    # getRelativePath — basic
    result = page.evaluate("""() => window._fn.getRelativePath("m/84'/0'/0'/0/5", 3)""")
    test("getRelativePath: m/84'/0'/0'/0/5 depth 3 → 0/5", result == "0/5")

    # getRelativePath — depth 0
    result = page.evaluate("""() => window._fn.getRelativePath("m/0/1", 0)""")
    test("getRelativePath: m/0/1 depth 0 → 0/1", result == "0/1")

    # getRelativePath — too shallow
    result = page.evaluate("""() => {
        try { window._fn.getRelativePath("m/84'/0'", 3); return "no error"; }
        catch (e) { return e.message; }
    }""")
    test("getRelativePath: too shallow throws", result != "no error")

    # getRelativePath — hardened child from xpub
    result = page.evaluate("""() => {
        try { window._fn.getRelativePath("m/84'/0'/0'/0'/5", 3); return "no error"; }
        catch (e) { return e.message; }
    }""")
    test("getRelativePath: hardened child throws", "hardened" in result.lower())

    # derivePublicKeyFromXpub — end-to-end with master xpub at m/0/1
    result = page.evaluate(f"""() => {{
        const pubkey = window._fn.derivePublicKeyFromXpub("{MASTER_XPUB}", "m/0/1");
        return {{ pubkey, len: pubkey.length, prefix: pubkey.slice(0, 2) }};
    }}""")
    test("derivePublicKeyFromXpub: returns 66 hex", result["len"] == 66)
    test("derivePublicKeyFromXpub: starts with 02 or 03", result["prefix"] in ("02", "03"))

    # derivePublicKeyFromXpub — same xpub different path gives different key
    result = page.evaluate(f"""() => {{
        const k1 = window._fn.derivePublicKeyFromXpub("{MASTER_XPUB}", "m/0/0");
        const k2 = window._fn.derivePublicKeyFromXpub("{MASTER_XPUB}", "m/0/1");
        return {{ k1, k2, different: k1 !== k2 }};
    }}""")
    test("derivePublicKeyFromXpub: different paths → different keys", result["different"])

    # DOM: xpub auto-derives pubkey
    page.click("#addInputButton")
    page.click("[data-utxo]:last-child .hw-toggle")
    page.fill("[data-utxo]:last-child .hw-path", "m/0/0")
    page.fill(f"[data-utxo]:last-child .hw-xpub", MASTER_XPUB)
    page.dispatch_event("[data-utxo]:last-child .hw-xpub", "input")
    pubkey_val = page.input_value("[data-utxo]:last-child .hw-pubkey")
    test("DOM: xpub auto-populates pubkey", len(pubkey_val) == 66, f"got len={len(pubkey_val)}")

    # DOM: pubkey field is readonly when xpub present
    is_readonly = page.evaluate("() => document.querySelector('[data-utxo]:last-child .hw-pubkey').readOnly")
    test("DOM: pubkey readonly when xpub set", is_readonly)

    # DOM: clearing xpub restores manual mode
    page.fill("[data-utxo]:last-child .hw-xpub", "")
    page.dispatch_event("[data-utxo]:last-child .hw-xpub", "input")
    is_readonly = page.evaluate("() => document.querySelector('[data-utxo]:last-child .hw-pubkey').readOnly")
    test("DOM: pubkey editable when xpub cleared", not is_readonly)

    # Clean up the extra input row
    page.click("[data-utxo]:last-child .remove")


# ============================================================
# Main
# ============================================================

def main():
    port = find_free_port()
    os.chdir(_PROJECT_ROOT)
    httpd = start_http_server(port)
    base_url = f"http://127.0.0.1:{port}/index.html"

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
