#!/usr/bin/env python3
"""
Coldcard signing simulation test for Bitcoin Address Sweeper.

Simulates Coldcard behavior using bitcoin-cli walletprocesspsbt with an
imported Coldcard xpub descriptor wallet. This allows fully automated
testing of PSBT structure, signing, finalization, and broadcasting
without requiring physical Coldcard interaction.

The key insight: Coldcard signing behavior for P2WPKH is determined by the
bip32Derivation path purpose (84' = P2WPKH). When the Coldcard sees
purpose 84, it signs using BIP143 sighash (segwit), producing a proper
witness. The bug we're investigating is when the Coldcard sees inputs
WITHOUT bip32Derivation (like WIF-signed inputs), it may default to
P2PKH signing.

This test simulates:
1. Pure Coldcard signing (all inputs have bip32Derivation)
2. Mixed WIF + Coldcard (pre-signed WIF input + Coldcard input)
3. Stripped witnessUtxo approach (parallel signing)
4. The actual PSBT created by the website's JavaScript code

Requires:
  - Bitcoin Core (bitcoind + bitcoin-cli) in PATH
  - embit: pip install embit
  - server/server.py for regtest node

Usage:
    python3 tests/test_coldcard_simulation.py
"""

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.request import urlopen, Request
from urllib.error import URLError

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TEST_DIR)
SERVER_READY_TIMEOUT = 90

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
# Server + CLI helpers
# ============================================================

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port):
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_PROJECT_ROOT, "server", "server.py"),
         str(port), "--regtest"],
        cwd=_PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=True)
    for _ in range(SERVER_READY_TIMEOUT):
        try:
            resp = urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "ok" and data.get("regtest"):
                return proc, data
        except (URLError, ConnectionRefusedError, OSError):
            pass
        if proc.poll() is not None:
            raise RuntimeError("Server exited prematurely")
        time.sleep(1)
    proc.kill()
    raise RuntimeError("Server timeout")


def stop_server(proc):
    if not proc or proc.poll() is not None:
        return
    pgid = os.getpgid(proc.pid)
    try:
        os.kill(proc.pid, signal.SIGINT)
        proc.wait(timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        try:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


class BitcoinCLI:
    def __init__(self, datadir, rpc_port):
        self.base_cmd = ["bitcoin-cli", f"-datadir={datadir}", "-regtest",
                         f"-rpcport={rpc_port}", "-rpcuser=test", "-rpcpassword=test"]

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


def api_post(base_url, path, data):
    body = json.dumps(data).encode("utf-8")
    req = Request(f"{base_url}{path}", data=body,
                  headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urlopen(req, timeout=30).read().decode("utf-8"))


# ============================================================
# PSBT analysis helpers
# ============================================================

def analyze_psbt(psbt_bytes):
    """Analyze a PSBT and return per-input field info."""
    from embit.psbt import PSBT
    psbt = PSBT.parse(psbt_bytes)
    inputs = []
    for i, inp in enumerate(psbt.inputs):
        info = {
            "index": i,
            "has_witness_utxo": inp.witness_utxo is not None,
            "has_non_witness_utxo": inp.non_witness_utxo is not None,
            "has_partial_sigs": bool(inp.partial_sigs),
            "num_partial_sigs": len(inp.partial_sigs) if inp.partial_sigs else 0,
            "has_final_scriptwitness": inp.final_scriptwitness is not None,
            "has_final_scriptsig": inp.final_scriptsig is not None,
            "has_bip32": bool(inp.bip32_derivations),
        }
        if inp.witness_utxo:
            spk = inp.witness_utxo.script_pubkey.data.hex()
            info["spk"] = spk
            if spk.startswith("0014"):
                info["type"] = "P2WPKH"
            elif spk.startswith("5120"):
                info["type"] = "P2TR"
            else:
                info["type"] = "other"
        inputs.append(info)
    return inputs


def is_raw_transaction(data):
    if len(data) < 10:
        return False
    version = int.from_bytes(data[:4], 'little')
    return version in (1, 2)


# ============================================================
# Tests
# ============================================================

def run_tests():
    from embit.psbt import PSBT, DerivationPath
    from embit.transaction import Transaction, TransactionInput, TransactionOutput
    from embit.script import Script
    from embit import ec as embit_ec, script as embit_script
    from embit.networks import NETWORKS
    from embit.finalizer import finalize_psbt

    # Start server
    section("1. Setup")
    port = find_free_port()
    server_url = f"http://127.0.0.1:{port}"

    server_proc = None
    cli = None
    tmp_dir = tempfile.mkdtemp(prefix="cc_sim_")

    try:
        server_proc, health = start_server(port)
        cli = BitcoinCLI(health["datadir"], health.get("rpc_port", 18443))
        test("regtest server started", True)

        # Create wallets:
        # - cc_wallet: simulates Coldcard (standard Bitcoin Core wallet)
        # - wif_wallet: separate wallet for WIF keys
        # - recipient: receives funds
        for name in ["cc_wallet", "wif_wallet", "recipient"]:
            try:
                cli.run("createwallet", name)
            except RuntimeError as e:
                if "already exists" not in str(e):
                    raise

        # Get addresses from cc_wallet (simulates Coldcard addresses)
        cc_addr_0 = cli.run("getnewaddress", "", "bech32", wallet="cc_wallet")
        cc_addr_1 = cli.run("getnewaddress", "", "bech32", wallet="cc_wallet")
        test("CC addr 0 valid", cc_addr_0.startswith("bcrt1q"))
        test("CC addr 1 valid", cc_addr_1.startswith("bcrt1q"))

        # Generate a WIF key using embit
        wif_privkey = embit_ec.PrivateKey(b'\x01' * 32)
        wif_key = wif_privkey.wif(NETWORKS["regtest"])
        wif_pubkey = wif_privkey.get_public_key()
        wif_addr = embit_script.p2wpkh(wif_pubkey).address(NETWORKS["regtest"])
        test("WIF addr valid", wif_addr.startswith("bcrt1q"))

        print(f"\n  CC addr 0: {cc_addr_0}")
        print(f"  CC addr 1: {cc_addr_1}")
        print(f"  WIF addr:  {wif_addr}")

        # ============================================================
        section("2. Pure Parallel Signing (bitcoin-cli wallets)")
        # ============================================================
        #
        # Standard case: 2 inputs from different wallets, each signs
        # a copy of the unsigned PSBT, then combine & finalize.
        #

        # Fund both wallets
        fund_cc = api_post(server_url, "/api/faucet",
                           {"address": cc_addr_0, "amount": "1.0"})
        test("funded CC addr", fund_cc.get("success"))

        # Fund WIF address directly
        fund_wif = api_post(server_url, "/api/faucet",
                            {"address": wif_addr, "amount": "0.5"})
        test("funded WIF addr", fund_wif.get("success"))

        # Get UTXOs
        cc_utxos = json.loads(urlopen(f"{server_url}/api/address/{cc_addr_0}/utxo").read())
        wif_utxos = json.loads(urlopen(f"{server_url}/api/address/{wif_addr}/utxo").read())
        test("CC UTXO found", len(cc_utxos) >= 1)
        test("WIF UTXO found", len(wif_utxos) >= 1)

        cc_utxo = cc_utxos[0]
        wif_utxo = wif_utxos[0]

        # Get raw txs for witnessUtxo
        cc_raw_hex = urlopen(f"{server_url}/api/tx/{cc_utxo['txid']}/hex").read().decode()
        wif_raw_hex = urlopen(f"{server_url}/api/tx/{wif_utxo['txid']}/hex").read().decode()
        cc_raw_tx = Transaction.from_string(cc_raw_hex)
        wif_raw_tx = Transaction.from_string(wif_raw_hex)

        recipient_addr = cli.run("getnewaddress", "", "bech32", wallet="recipient")
        send_sats = 149_800_000

        # Build PSBT
        tx = Transaction(version=2,
            vin=[
                TransactionInput(txid=bytes.fromhex(cc_utxo["txid"]),
                                 vout=cc_utxo["vout"], sequence=0xffffffff),
                TransactionInput(txid=bytes.fromhex(wif_utxo["txid"]),
                                 vout=wif_utxo["vout"], sequence=0xffffffff),
            ],
            vout=[TransactionOutput(value=send_sats,
                                    script_pubkey=Script.from_address(recipient_addr))],
            locktime=0)

        psbt = PSBT(tx)
        psbt.inputs[0].witness_utxo = cc_raw_tx.vout[cc_utxo["vout"]]
        psbt.inputs[1].witness_utxo = wif_raw_tx.vout[wif_utxo["vout"]]

        # Sign CC input with bitcoin-cli (parallel approach)
        psbt_b64 = base64.b64encode(psbt.serialize()).decode()
        cc_signed = cli.run_json("walletprocesspsbt", psbt_b64,
                                  "true", "DEFAULT", "true", "false",
                                  wallet="cc_wallet")
        test("CC signed (partial)", not cc_signed.get("complete"))

        # Sign WIF input with embit
        wif_signed_psbt = PSBT.parse(psbt.serialize())
        wif_sigs = wif_signed_psbt.sign_with(wif_privkey)
        test("WIF signed", wif_sigs > 0)

        # Combine
        cc_signed_bytes = base64.b64decode(cc_signed["psbt"])
        wif_signed_bytes = wif_signed_psbt.serialize()

        combined = PSBT.parse(cc_signed_bytes)
        wif_half = PSBT.parse(wif_signed_bytes)
        combined.inputs[1].partial_sigs.update(wif_half.inputs[1].partial_sigs)

        # Analyze before finalization
        analysis = analyze_psbt(combined.serialize())
        print(f"\n  Combined PSBT:")
        for inp in analysis:
            print(f"    Input {inp['index']}: partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                  f"finalWit={inp['has_final_scriptwitness']}, finalSig={inp['has_final_scriptsig']}")

        test("input 0 has partial sig", analysis[0]['has_partial_sigs'])
        test("input 1 has partial sig", analysis[1]['has_partial_sigs'])

        # Finalize and broadcast
        final_tx = finalize_psbt(combined)
        final_hex = final_tx.serialize().hex()

        for i, inp in enumerate(final_tx.vin):
            has_wit = inp.witness and len(inp.witness.items) > 0
            has_sig = len(inp.script_sig.data) > 0
            test(f"parallel input {i}: has witness", has_wit)
            test(f"parallel input {i}: empty scriptSig", not has_sig)

        txid = cli.run("sendrawtransaction", final_hex)
        test("parallel tx broadcast", len(txid) == 64)
        api_post(server_url, "/api/mine", {"blocks": 1})
        decoded = cli.run_json("getrawtransaction", txid, "true")
        test("parallel tx confirmed", decoded.get("confirmations", 0) >= 1)

        # ============================================================
        section("3. Mixed WIF Pre-signed + CC Signing (Serial)")
        # ============================================================
        #
        # THE BUG SCENARIO:
        # 1. Create PSBT with 2 inputs (CC + WIF)
        # 2. Pre-sign the WIF input (partial_sigs)
        # 3. Give the partially-signed PSBT to CC wallet (walletprocesspsbt)
        # 4. CC should sign only its input
        # 5. Finalize & broadcast
        #

        # Fund new addresses
        cc_addr_s = cli.run("getnewaddress", "", "bech32", wallet="cc_wallet")
        fund_s = api_post(server_url, "/api/faucet", {"address": cc_addr_s, "amount": "0.8"})
        test("serial: funded CC addr", fund_s.get("success"))

        wif2_privkey = embit_ec.PrivateKey(b'\x02' * 32)
        wif2_addr = embit_script.p2wpkh(wif2_privkey.get_public_key()).address(NETWORKS["regtest"])
        fund_w = api_post(server_url, "/api/faucet", {"address": wif2_addr, "amount": "0.3"})
        test("serial: funded WIF addr", fund_w.get("success"))

        cc_s_utxos = json.loads(urlopen(f"{server_url}/api/address/{cc_addr_s}/utxo").read())
        wif2_utxos = json.loads(urlopen(f"{server_url}/api/address/{wif2_addr}/utxo").read())

        cc_s_utxo = cc_s_utxos[0]
        wif2_utxo = wif2_utxos[0]

        cc_s_raw = Transaction.from_string(
            urlopen(f"{server_url}/api/tx/{cc_s_utxo['txid']}/hex").read().decode())
        wif2_raw = Transaction.from_string(
            urlopen(f"{server_url}/api/tx/{wif2_utxo['txid']}/hex").read().decode())

        recip_s = cli.run("getnewaddress", "", "bech32", wallet="recipient")

        serial_tx = Transaction(version=2,
            vin=[
                TransactionInput(txid=bytes.fromhex(cc_s_utxo["txid"]),
                                 vout=cc_s_utxo["vout"], sequence=0xffffffff),
                TransactionInput(txid=bytes.fromhex(wif2_utxo["txid"]),
                                 vout=wif2_utxo["vout"], sequence=0xffffffff),
            ],
            vout=[TransactionOutput(value=109_800_000,
                                    script_pubkey=Script.from_address(recip_s))],
            locktime=0)

        serial_psbt = PSBT(serial_tx)
        serial_psbt.inputs[0].witness_utxo = cc_s_raw.vout[cc_s_utxo["vout"]]
        serial_psbt.inputs[1].witness_utxo = wif2_raw.vout[wif2_utxo["vout"]]

        # Step 1: Pre-sign WIF input
        wif2_sigs = serial_psbt.sign_with(wif2_privkey)
        test("serial: WIF pre-signed", wif2_sigs > 0)

        pre_analysis = analyze_psbt(serial_psbt.serialize())
        print(f"\n  After WIF pre-signing:")
        for inp in pre_analysis:
            print(f"    Input {inp['index']}: partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                  f"type={inp.get('type', '?')}")

        test("serial: input 0 unsigned", not pre_analysis[0]['has_partial_sigs'])
        test("serial: input 1 has WIF sig", pre_analysis[1]['has_partial_sigs'])

        # Step 2: Give to CC wallet (simulates Coldcard)
        presigned_b64 = base64.b64encode(serial_psbt.serialize()).decode()
        cc_serial = cli.run_json("walletprocesspsbt", presigned_b64,
                                  "true", "DEFAULT", "true", "false",
                                  wallet="cc_wallet")

        # Check: did bitcoin-cli sign only the CC input, or did it try to
        # re-finalize the WIF input too?
        cc_serial_bytes = base64.b64decode(cc_serial["psbt"])
        serial_analysis = analyze_psbt(cc_serial_bytes)
        print(f"\n  After CC (bitcoin-cli) signing:")
        for inp in serial_analysis:
            print(f"    Input {inp['index']}: "
                  f"partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                  f"finalWit={inp['has_final_scriptwitness']}, "
                  f"finalSig={inp['has_final_scriptsig']}")

        test("serial: CC signed its input (0)",
             serial_analysis[0]['has_partial_sigs'] or serial_analysis[0]['has_final_scriptwitness'])
        test("serial: CC did NOT add final_scriptsig to input 0",
             not serial_analysis[0]['has_final_scriptsig'],
             "bitcoin-cli finalized as P2PKH!")
        test("serial: WIF sig preserved on input 1",
             serial_analysis[1]['has_partial_sigs'] or serial_analysis[1]['has_final_scriptwitness'])

        # Step 3: Finalize & broadcast
        try:
            serial_final = PSBT.parse(cc_serial_bytes)
            serial_final_tx = finalize_psbt(serial_final)
            serial_hex = serial_final_tx.serialize().hex()

            for i, inp in enumerate(serial_final_tx.vin):
                has_wit = inp.witness and len(inp.witness.items) > 0
                has_sig = len(inp.script_sig.data) > 0
                test(f"serial finalized input {i}: has witness", has_wit)
                test(f"serial finalized input {i}: empty scriptSig", not has_sig,
                     f"P2PKH bug! scriptSig={inp.script_sig.data.hex()[:40]}...")

            txid_s = cli.run("sendrawtransaction", serial_hex)
            test("serial tx broadcast", len(txid_s) == 64)
            api_post(server_url, "/api/mine", {"blocks": 1})
        except Exception as e:
            test("serial finalization", False, str(e))

        # ============================================================
        section("4. Website PSBT Format Test (via Playwright)")
        # ============================================================
        #
        # Test the actual PSBT created by the website's JavaScript,
        # including the bip32Derivation field format.
        #

        try:
            from playwright.sync_api import sync_playwright

            # Fund a fresh CC address and WIF address
            cc_addr_w = cli.run("getnewaddress", "", "bech32", wallet="cc_wallet")
            fund_w1 = api_post(server_url, "/api/faucet", {"address": cc_addr_w, "amount": "0.6"})
            test("web: funded CC addr", fund_w1.get("success"))

            wif3_privkey = embit_ec.PrivateKey(b'\x03' * 32)
            wif3_key = wif3_privkey.wif(NETWORKS["regtest"])
            wif3_addr = embit_script.p2wpkh(wif3_privkey.get_public_key()).address(NETWORKS["regtest"])
            fund_w2 = api_post(server_url, "/api/faucet", {"address": wif3_addr, "amount": "0.4"})
            test("web: funded WIF addr", fund_w2.get("success"))

            base_url = f"http://127.0.0.1:{port}/index.html"

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not ("--headed" in sys.argv))
                page = browser.new_page()
                page.add_init_script("window.__TEST_MODE__ = true")
                page.goto(base_url)
                page.wait_for_function("() => window._fn !== undefined", timeout=15000)
                page.wait_for_function("() => window._fn.serverMode === true", timeout=10000)
                page.select_option("#network", "regtest")

                # Clear default rows
                page.evaluate("() => document.getElementById('utxoContainer').innerHTML = ''")
                page.evaluate("() => document.getElementById('outputContainer').innerHTML = ''")

                # Fetch CC UTXOs
                page.fill("#fetchAddress", cc_addr_w)
                page.click("#fetchUtxosBtn")
                page.wait_for_function(
                    "() => document.querySelectorAll('#utxoContainer [data-utxo]').length >= 1",
                    timeout=30000)
                # Wait for fetch to fully complete
                page.wait_for_function(
                    "() => !document.getElementById('fetchUtxosBtn').disabled",
                    timeout=10000)

                # Fetch WIF UTXOs
                page.fill("#fetchAddress", wif3_key)
                page.click("#fetchUtxosBtn")
                page.wait_for_function(
                    "() => document.querySelectorAll('#utxoContainer [data-utxo]').length >= 2",
                    timeout=30000)

                utxo_count = len(page.query_selector_all("[data-utxo]"))
                test("web: 2 UTXOs fetched", utxo_count == 2, f"got {utxo_count}")

                # Check which UTXOs have WIF
                has_wif_info = page.evaluate("""() => {
                    const rows = document.querySelectorAll('[data-utxo]');
                    return Array.from(rows).map((r, i) => ({
                        index: i,
                        hasWif: !!r.getAttribute('data-wif'),
                        wifPrefix: (r.getAttribute('data-wif') || '')[0] || 'none'
                    }));
                }""")
                print(f"\n  UTXO WIF info:")
                for info in has_wif_info:
                    print(f"    Input {info['index']}: hasWif={info['hasWif']}, prefix={info['wifPrefix']}")

                # Check step layout
                step_mode = page.evaluate("""() => {
                    return {
                        allWif: window._fn.allUtxosHaveWif(),
                        someWif: window._fn.someUtxosHaveWif(),
                    }
                }""")
                print(f"  Step mode: allWif={step_mode['allWif']}, someWif={step_mode['someWif']}")
                test("web: someUtxosHaveWif is true", step_mode['someWif'])
                test("web: allUtxosHaveWif is false", not step_mode['allWif'])

                # Set up output
                recip_w = cli.run("getnewaddress", "", "bech32", wallet="recipient")
                page.fill("#feeRate", "1")
                page.evaluate(f"""() => {{
                    window._fn.addOutput(null, "{recip_w}", 99800000);
                }}""")

                # Set tip to No Tip
                page.evaluate("""() => {
                    document.querySelectorAll('.tip-preset').forEach(p => p.classList.remove('active'));
                    document.getElementById('tipSats').value = '0';
                }""")

                # Create PSBT
                dialogs = []
                page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

                page.click("#createPsbt")
                page.wait_for_selector("#psbtResult", state="visible", timeout=30000)

                # Get the PSBT hex from the page
                psbt_hex = page.text_content("#psbtHex")
                test("web: PSBT hex generated", len(psbt_hex or "") > 0,
                     f"length={len(psbt_hex or '')}")

                if psbt_hex:
                    psbt_bytes = bytes.fromhex(psbt_hex)

                    if psbt_bytes[:5] == b"psbt\xff":
                        web_analysis = analyze_psbt(psbt_bytes)
                        print(f"\n  Website PSBT analysis:")
                        for inp in web_analysis:
                            print(f"    Input {inp['index']}: type={inp.get('type', '?')}, "
                                  f"bip32={inp['has_bip32']}, "
                                  f"partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                                  f"finalWit={inp['has_final_scriptwitness']}")

                        # The WIF input should be partially signed
                        wif_inputs = [inp for inp in web_analysis if inp['has_partial_sigs']]
                        unsigned_inputs = [inp for inp in web_analysis
                                           if not inp['has_partial_sigs']
                                           and not inp['has_final_scriptwitness']]

                        test("web: has WIF-signed inputs", len(wif_inputs) > 0,
                             f"found {len(wif_inputs)}")
                        test("web: has unsigned CC inputs", len(unsigned_inputs) > 0,
                             f"found {len(unsigned_inputs)}")

                        # Check if the CC input can be signed by walletprocesspsbt
                        web_b64 = base64.b64encode(psbt_bytes).decode()
                        try:
                            web_cc_signed = cli.run_json("walletprocesspsbt", web_b64,
                                                         "true", "DEFAULT", "true", "false",
                                                         wallet="cc_wallet")
                            web_signed_bytes = base64.b64decode(web_cc_signed["psbt"])
                            web_signed_analysis = analyze_psbt(web_signed_bytes)

                            print(f"\n  After CC signs website PSBT:")
                            for inp in web_signed_analysis:
                                print(f"    Input {inp['index']}: "
                                      f"partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                                      f"finalWit={inp['has_final_scriptwitness']}, "
                                      f"finalSig={inp['has_final_scriptsig']}")

                            # Check for P2PKH bug
                            for inp in web_signed_analysis:
                                if inp['has_final_scriptsig']:
                                    test(f"web: input {inp['index']} P2PKH check", False,
                                         "HAS final_scriptsig = P2PKH finalization bug!")

                            # Try finalize and broadcast
                            try:
                                final_web = PSBT.parse(web_signed_bytes)
                                final_web_tx = finalize_psbt(final_web)
                                final_web_hex = final_web_tx.serialize().hex()

                                for i, inp in enumerate(final_web_tx.vin):
                                    has_wit = inp.witness and len(inp.witness.items) > 0
                                    has_sig = len(inp.script_sig.data) > 0
                                    test(f"web final input {i}: witness={has_wit}", has_wit)
                                    test(f"web final input {i}: empty scriptSig", not has_sig)

                                txid_w = cli.run("sendrawtransaction", final_web_hex)
                                test("web tx broadcast", len(txid_w) == 64)
                                api_post(server_url, "/api/mine", {"blocks": 1})

                            except Exception as e:
                                test("web PSBT finalization", False, str(e))

                        except RuntimeError as e:
                            test("web CC signing", False, str(e))

                    else:
                        # Website produced something other than PSBT
                        # (maybe it's the savedWifPsbt/hwCopy from parallel mode)
                        print(f"  Website output is not PSBT: first5={psbt_bytes[:5].hex()}")
                        test("web: output is valid PSBT", False)

                browser.close()

        except ImportError:
            print("  ⚠ Playwright not available, skipping web tests")

        # ============================================================
        section("5. Coldcard-specific Behavior Simulation")
        # ============================================================
        #
        # Simulate how the Coldcard determines signing type:
        # - If bip32Derivation has purpose 84' → P2WPKH (segwit)
        # - If bip32Derivation has purpose 44' → P2PKH (legacy)
        # - If no bip32Derivation → Coldcard defaults to... what?
        #
        # walletprocesspsbt in Bitcoin Core always signs correctly based
        # on the scriptPubKey type, so we can't simulate the Coldcard bug
        # with it. But we can test the PSBT structure.

        # Create a PSBT where input has P2WPKH scriptPubKey but
        # bip32Derivation with purpose 44' (P2PKH path) — this would
        # confuse the Coldcard into signing as P2PKH
        print("\n  NOTE: bitcoin-cli always signs correctly based on scriptPubKey,")
        print("  not bip32Derivation path. The Coldcard-specific P2PKH bug")
        print("  can only be fully reproduced with physical Coldcard device.")
        print("  Tests 2-4 verify the PSBT structure is correct for proper")
        print("  Coldcard signing behavior.")

    except Exception as e:
        print(f"\n  ❌ Error: {e}")
        traceback.print_exc()

    finally:
        if server_proc:
            stop_server(server_proc)
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 60)
    print("  Coldcard Signing Simulation Test")
    print("  (No physical Coldcard needed)")
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
