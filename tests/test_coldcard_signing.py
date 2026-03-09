#!/usr/bin/env python3
"""
Coldcard MK4 signing tests for Bitcoin Address Sweeper.

Tests PSBT signing behavior with a physical Coldcard MK4 connected via USB,
using the ckcc-protocol CLI tool. Validates signing behavior for:
  - Pure Coldcard (HW-only) P2WPKH signing
  - Mixed WIF + Coldcard partial signing (the bug scenario)
  - Coldcard finalization behavior (P2PKH vs P2WPKH)
  - Virtual disk auto-sign workflow

Requires:
  - Coldcard MK4 connected via USB with auto-sign enabled
  - ckcc-protocol: pip install ckcc-protocol
  - embit: pip install embit
  - Bitcoin Core (bitcoind + bitcoin-cli) in PATH
  - server/server.py for regtest node

Usage:
    python3 tests/test_coldcard_signing.py              # headless
    python3 tests/test_coldcard_signing.py --headed      # visible browser
"""

import base64
import binascii
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

# ============================================================
# Configuration
# ============================================================

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TEST_DIR)
HEADED = "--headed" in sys.argv
SERVER_READY_TIMEOUT = 90

# Coldcard info — auto-detected at runtime
CC_XFP = None  # Set by detect_coldcard_info()
CC_XPUB_84 = None
CC_DERIVATION_BASE = "m/84'/1'/0'"

# Virtual disk path
COLDCARD_VOLUME = "/Volumes/COLDCARD"

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
# Prerequisites check
# ============================================================

def detect_coldcard_info():
    """Auto-detect Coldcard fingerprint and xpub from the connected device."""
    global CC_XFP, CC_XPUB_84

    try:
        result = subprocess.run(["ckcc", "xfp"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            CC_XFP = result.stdout.strip().lower()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    try:
        result = subprocess.run(["ckcc", "xpub", CC_DERIVATION_BASE],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            CC_XPUB_84 = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    return CC_XFP is not None and CC_XPUB_84 is not None


def check_prerequisites():
    """Verify all prerequisites are met before running tests."""
    section("0. Prerequisites")

    # Check ckcc
    try:
        result = subprocess.run(["ckcc", "list"], capture_output=True, text=True, timeout=10)
        has_ckcc = result.returncode == 0
        test("ckcc-protocol installed", has_ckcc)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        test("ckcc-protocol installed", False, "install with: pip install ckcc-protocol")
        return False

    # Check Coldcard connected and logged in
    print("  Checking Coldcard connection (waiting up to 60s for device login)...")
    if not wait_for_coldcard(timeout=60):
        test("Coldcard connected & logged in", False,
             "Device not found. Make sure it's connected via USB and logged in (PIN entered).")
        return False

    # Auto-detect Coldcard info
    detected = detect_coldcard_info()
    test("Coldcard info detected", detected,
         f"XFP={CC_XFP}, xpub={'found' if CC_XPUB_84 else 'missing'}")
    if not detected:
        return False

    print(f"    XFP:  {CC_XFP}")
    print(f"    xpub: {CC_XPUB_84[:20]}...{CC_XPUB_84[-8:]}")

    # Check chain is regtest
    try:
        result = subprocess.run(["ckcc", "chain"], capture_output=True, text=True, timeout=10)
        chain = result.stdout.strip()
        test("Coldcard on regtest (XRT)", chain == "XRT", f"got: {chain}")
        if chain != "XRT":
            print("  ⚠ Coldcard must be set to regtest chain. Change in: Settings > Blockchain > Regtest")
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        test("Coldcard chain check", False)
        return False

    # Check embit
    try:
        from embit import ec
        from embit.psbt import PSBT
        test("embit library available", True)
    except ImportError:
        test("embit library available", False, "install with: pip install embit")
        return False

    # Check bitcoind
    try:
        result = subprocess.run(["bitcoind", "--version"], capture_output=True, text=True, timeout=5)
        test("bitcoind available", result.returncode == 0)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        test("bitcoind available", False)
        return False

    # Check virtual disk (optional — not all tests need it)
    has_volume = os.path.isdir(COLDCARD_VOLUME)
    test("COLDCARD virtual disk mounted (optional)", has_volume,
         "some tests will be skipped" if not has_volume else "")

    return True


# ============================================================
# Server lifecycle (reuse from test_regtest_e2e.py)
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
    try:
        os.kill(proc.pid, signal.SIGINT)
        proc.wait(timeout=20)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=10)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


# ============================================================
# bitcoin-cli helper
# ============================================================

class BitcoinCLI:
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


def api_post(base_url, path, data):
    body = json.dumps(data).encode("utf-8")
    req = Request(f"{base_url}{path}", data=body,
                  headers={"Content-Type": "application/json"}, method="POST")
    resp = urlopen(req, timeout=30)
    return json.loads(resp.read().decode("utf-8"))


# ============================================================
# Coldcard helpers
# ============================================================

def wait_for_coldcard(timeout=60):
    """Wait for Coldcard to be available via USB."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = subprocess.run(["ckcc", "xfp"], capture_output=True,
                                    text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        time.sleep(2)
    return False


def ckcc_sign(psbt_path, output_path=None, finalize=False, retries=3, base_wait=5):
    """Sign a PSBT using the Coldcard via USB (ckcc sign).

    Returns the path to the signed PSBT file.
    NOTE: Requires user to physically approve on the Coldcard device!
    """
    if output_path is None:
        base, ext = os.path.splitext(psbt_path)
        output_path = f"{base}-signed{ext}"

    cmd = ["ckcc", "sign"]
    if finalize:
        cmd.append("--finalize")
    cmd.extend([psbt_path, output_path])

    print(f"    👆 Please APPROVE the transaction on the Coldcard...")

    for attempt in range(retries):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return output_path
        err = result.stderr.strip()
        if ("handling another request" in err or "Could not find" in err) and attempt < retries - 1:
            wait_time = base_wait * (attempt + 1)
            print(f"    ⏳ Coldcard busy/unavailable, retrying in {wait_time}s... (attempt {attempt + 1}/{retries})")
            time.sleep(wait_time)
            continue
        raise RuntimeError(f"ckcc sign failed: {err}")

    return output_path


def ckcc_sign_virtual_disk(psbt_path, timeout=30):
    """Sign a PSBT using the Coldcard virtual disk auto-sign.

    Copies the PSBT to /Volumes/COLDCARD/, waits for signed output,
    returns the path to the signed file.
    """
    filename = os.path.basename(psbt_path)
    dest = os.path.join(COLDCARD_VOLUME, filename)

    # Clean up any previous signed files
    for f in os.listdir(COLDCARD_VOLUME):
        if f.endswith("-signed.psbt") or f.endswith("-part.psbt") or f.endswith("-final.txn"):
            os.remove(os.path.join(COLDCARD_VOLUME, f))

    # Copy PSBT to volume
    shutil.copy2(psbt_path, dest)

    # Wait for signed output
    base, ext = os.path.splitext(filename)
    possible_outputs = [
        f"{base}-signed{ext}",      # Partially signed PSBT
        f"{base}-part{ext}",        # Partially signed (alternate naming)
        f"{base}-final.txn",        # Finalized raw transaction
    ]

    start = time.time()
    while time.time() - start < timeout:
        for out_name in possible_outputs:
            out_path = os.path.join(COLDCARD_VOLUME, out_name)
            if os.path.exists(out_path):
                # Wait a moment for file to be fully written
                time.sleep(0.5)
                return out_path
        time.sleep(0.5)

    raise TimeoutError(f"Coldcard did not produce signed output within {timeout}s")


def coldcard_get_address(derivation_path, addr_type="segwit"):
    """Get an address from the Coldcard for a given derivation path."""
    cmd = ["ckcc", "addr"]
    if addr_type == "segwit":
        cmd.append("-s")
    elif addr_type == "taproot":
        cmd.append("-t")
    elif addr_type == "wrapped":
        cmd.append("-w")
    cmd.extend(["-q", derivation_path])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(f"ckcc addr failed: {result.stderr.strip()}")
    return result.stdout.strip()


def coldcard_get_pubkey(derivation_path):
    """Get a compressed public key from the Coldcard."""
    result = subprocess.run(
        ["ckcc", "pubkey", derivation_path],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError(f"ckcc pubkey failed: {result.stderr.strip()}")
    return result.stdout.strip()


def analyze_psbt_fields(psbt_bytes):
    """Analyze a PSBT and return per-input field info using embit."""
    from embit.psbt import PSBT

    psbt = PSBT.parse(psbt_bytes)
    inputs = []
    for i, inp in enumerate(psbt.inputs):
        info = {
            "index": i,
            "has_witness_utxo": inp.witness_utxo is not None,
            "has_non_witness_utxo": inp.non_witness_utxo is not None,
            "has_partial_sigs": len(inp.partial_sigs) > 0 if hasattr(inp, 'partial_sigs') and inp.partial_sigs else False,
            "num_partial_sigs": len(inp.partial_sigs) if hasattr(inp, 'partial_sigs') and inp.partial_sigs else 0,
            "has_final_scriptwitness": inp.final_scriptwitness is not None,
            "has_final_scriptsig": inp.final_scriptsig is not None,
            "has_bip32_derivations": len(inp.bip32_derivations) > 0 if hasattr(inp, 'bip32_derivations') and inp.bip32_derivations else False,
        }

        # Get scriptPubKey from witness_utxo if available
        if inp.witness_utxo:
            spk = inp.witness_utxo.script_pubkey.data.hex()
            info["script_pubkey"] = spk
            if spk.startswith("0014"):
                info["script_type"] = "P2WPKH"
            elif spk.startswith("5120"):
                info["script_type"] = "P2TR"
            elif spk.startswith("0020"):
                info["script_type"] = "P2WSH"
            elif spk.startswith("76a914"):
                info["script_type"] = "P2PKH"
            else:
                info["script_type"] = "unknown"

        # Check bip32 derivation details
        if hasattr(inp, 'bip32_derivations') and inp.bip32_derivations:
            for pubkey, derivation in inp.bip32_derivations.items():
                info["bip32_fingerprint"] = derivation.fingerprint.hex()
                info["bip32_path"] = "/".join(str(x) for x in derivation.derivation)
                break  # Just get the first one

        inputs.append(info)

    return {
        "num_inputs": len(psbt.inputs),
        "num_outputs": len(psbt.outputs),
        "inputs": inputs,
    }


def is_raw_transaction(data):
    """Check if binary data is a raw Bitcoin transaction (not a PSBT)."""
    if len(data) < 10:
        return False
    # Check for version bytes (little-endian uint32)
    version = int.from_bytes(data[:4], 'little')
    return version in (1, 2)


# ============================================================
# Tests
# ============================================================

def run_tests():
    """Run all Coldcard signing tests."""

    # ========================================================
    section("1. Coldcard Address Derivation")
    # ========================================================

    # Verify we can derive addresses that match the xpub
    cc_addr_0 = coldcard_get_address(f"{CC_DERIVATION_BASE}/0/0")
    cc_addr_1 = coldcard_get_address(f"{CC_DERIVATION_BASE}/0/1")
    cc_addr_change = coldcard_get_address(f"{CC_DERIVATION_BASE}/1/0")

    test("CC addr 0/0 is regtest bech32", cc_addr_0.startswith("bcrt1q"),
         f"got: {cc_addr_0}")
    test("CC addr 0/1 is regtest bech32", cc_addr_1.startswith("bcrt1q"),
         f"got: {cc_addr_1}")
    test("CC change addr is regtest bech32", cc_addr_change.startswith("bcrt1q"),
         f"got: {cc_addr_change}")

    cc_pubkey_0 = coldcard_get_pubkey(f"{CC_DERIVATION_BASE}/0/0")
    test("CC pubkey 0/0 is 66-char hex", len(cc_pubkey_0) == 66 and
         cc_pubkey_0.startswith(("02", "03")),
         f"got: {cc_pubkey_0}")

    print(f"\n  Coldcard addresses:")
    print(f"    0/0: {cc_addr_0}")
    print(f"    0/1: {cc_addr_1}")
    print(f"    1/0 (change): {cc_addr_change}")

    # ========================================================
    # Start regtest server
    # ========================================================

    section("2. Start Regtest Server")

    port = find_free_port()
    server_url = f"http://127.0.0.1:{port}"
    base_url = f"http://127.0.0.1:{port}/index.html"

    server_proc = None
    cli = None
    tmp_dir = tempfile.mkdtemp(prefix="cc_test_")

    try:
        server_proc, health_data = start_server(port)
        datadir = health_data.get("datadir", "")
        rpc_port = health_data.get("rpc_port", 18443)
        cli = BitcoinCLI(datadir, rpc_port)

        test("regtest server started", True)
        test("server datadir found", len(datadir) > 0, f"datadir: {datadir}")

        # Create test wallets
        for name in ["cc_faucet", "wif_wallet", "recipient"]:
            try:
                cli.run("createwallet", name)
            except RuntimeError as e:
                if "already exists" not in str(e):
                    raise

        # ========================================================
        section("3. Pure Coldcard P2WPKH Signing (ckcc)")
        # ========================================================
        #
        # Scenario: Fund a Coldcard-derived address, create PSBT with
        # proper bip32Derivation, sign via ckcc, validate result.
        #

        # Fund Coldcard address
        fund_result = api_post(server_url, "/api/faucet",
                               {"address": cc_addr_0, "amount": "1.0"})
        test("funded CC addr 0/0", fund_result.get("success") is True,
             f"got: {fund_result}")
        cc_txid = fund_result.get("txid", "")

        # Get the UTXO details
        utxo_url = f"{server_url}/api/address/{cc_addr_0}/utxo"
        utxos_raw = urlopen(utxo_url, timeout=10).read()
        utxos = json.loads(utxos_raw)
        test("CC UTXO fetched", len(utxos) >= 1, f"got {len(utxos)} UTXOs")

        cc_utxo = utxos[0]
        cc_utxo_txid = cc_utxo["txid"]
        cc_utxo_vout = cc_utxo["vout"]
        cc_utxo_value = cc_utxo["value"]

        print(f"\n  Coldcard UTXO: {cc_utxo_txid}:{cc_utxo_vout} = {cc_utxo_value} sats")

        # Get recipient address
        recipient_addr = cli.run("getnewaddress", "", "bech32", wallet="recipient")

        # Create PSBT using embit with proper bip32Derivation
        from embit.psbt import PSBT, DerivationPath
        from embit.transaction import Transaction, TransactionInput, TransactionOutput
        from embit.script import Script
        from embit import bip32, ec as embit_ec

        # Get the raw transaction for witnessUtxo
        raw_tx_hex = urlopen(f"{server_url}/api/tx/{cc_utxo_txid}/hex", timeout=10).read().decode()

        # Parse the raw tx to get the output script
        raw_tx = Transaction.from_string(raw_tx_hex)
        witness_utxo = raw_tx.vout[cc_utxo_vout]

        # Build the unsigned transaction
        send_sats = 99_900_000  # 0.999 BTC (fee = 100k sats)
        tx = Transaction(
            version=2,
            vin=[TransactionInput(
                txid=bytes.fromhex(cc_utxo_txid),
                vout=cc_utxo_vout,
                sequence=0xffffffff,
            )],
            vout=[TransactionOutput(
                value=send_sats,
                script_pubkey=Script.from_address(recipient_addr),
            )],
            locktime=0,
        )

        # Create PSBT
        psbt = PSBT(tx)

        # Set witnessUtxo
        psbt.inputs[0].witness_utxo = witness_utxo

        # Set bip32Derivation for Coldcard to recognize the input
        pubkey_obj = embit_ec.PublicKey.parse(bytes.fromhex(cc_pubkey_0))
        xfp_bytes = bytes.fromhex(CC_XFP)
        derivation = DerivationPath(
            fingerprint=xfp_bytes,
            derivation=[84 | 0x80000000, 1 | 0x80000000, 0 | 0x80000000, 0, 0]
        )
        psbt.inputs[0].bip32_derivations[pubkey_obj] = derivation

        # Serialize and save
        psbt_bytes = psbt.serialize()
        psbt_path = os.path.join(tmp_dir, "cc_pure.psbt")
        with open(psbt_path, "wb") as f:
            f.write(psbt_bytes)

        test("PSBT created with bip32Derivation", len(psbt_bytes) > 0,
             f"size: {len(psbt_bytes)} bytes")

        # Analyze the unsigned PSBT
        analysis = analyze_psbt_fields(psbt_bytes)
        test("PSBT has 1 input", analysis["num_inputs"] == 1)
        test("input has witnessUtxo", analysis["inputs"][0]["has_witness_utxo"])
        test("input has bip32Derivation", analysis["inputs"][0]["has_bip32_derivations"])
        test("input script type is P2WPKH", analysis["inputs"][0].get("script_type") == "P2WPKH",
             f"got: {analysis['inputs'][0].get('script_type')}")
        test("bip32 fingerprint is CC XFP",
             analysis["inputs"][0].get("bip32_fingerprint") == CC_XFP,
             f"got: {analysis['inputs'][0].get('bip32_fingerprint')}")

        # Sign with Coldcard via ckcc
        signed_path = os.path.join(tmp_dir, "cc_pure-signed.psbt")
        try:
            ckcc_sign(psbt_path, signed_path)
            test("ckcc sign succeeded", True)
        except RuntimeError as e:
            test("ckcc sign succeeded", False, str(e))
            # Try to continue with remaining tests
            signed_path = None

        if signed_path and os.path.exists(signed_path):
            with open(signed_path, "rb") as f:
                signed_bytes = f.read()

            # Check if it's a PSBT or raw tx
            is_psbt = signed_bytes[:5] == b"psbt\xff"
            is_raw_tx = is_raw_transaction(signed_bytes)

            test("signed output is PSBT (not raw tx)", is_psbt,
                 f"PSBT={is_psbt}, rawTx={is_raw_tx}, first5={signed_bytes[:5].hex()}")

            if is_psbt:
                signed_analysis = analyze_psbt_fields(signed_bytes)
                inp0 = signed_analysis["inputs"][0]

                print(f"\n  Signed PSBT analysis:")
                print(f"    has_partial_sigs: {inp0['has_partial_sigs']}")
                print(f"    num_partial_sigs: {inp0['num_partial_sigs']}")
                print(f"    has_final_scriptwitness: {inp0['has_final_scriptwitness']}")
                print(f"    has_final_scriptsig: {inp0['has_final_scriptsig']}")

                # Coldcard should sign P2WPKH properly (witness, not scriptSig)
                test("signed: has partial_sigs OR final_scriptwitness",
                     inp0['has_partial_sigs'] or inp0['has_final_scriptwitness'],
                     f"partial_sigs={inp0['has_partial_sigs']}, finalWit={inp0['has_final_scriptwitness']}")
                test("signed: NO final_scriptsig (P2PKH would set this)",
                     not inp0['has_final_scriptsig'],
                     "Coldcard incorrectly finalized as P2PKH!")

            elif is_raw_tx:
                print(f"\n  ⚠ Coldcard produced raw transaction (finalized)")
                print(f"    Size: {len(signed_bytes)} bytes")
                # Parse and check the raw tx structure
                raw = Transaction.from_string(signed_bytes.hex())
                has_witness = any(len(inp.witness.items) > 0 for inp in raw.vin if inp.witness)
                test("raw tx uses witness (segwit)", has_witness,
                     "Coldcard finalized as P2PKH instead of P2WPKH!")

        # ========================================================
        section("4. Coldcard Finalize Mode (ckcc sign -f)")
        # ========================================================
        #
        # When Coldcard can sign ALL inputs, using -f flag should
        # produce a finalized raw transaction with proper witness data.
        #

        finalized_path = os.path.join(tmp_dir, "cc_finalized.txn")
        try:
            ckcc_sign(psbt_path, finalized_path, finalize=True)
            test("ckcc sign --finalize succeeded", True)
        except RuntimeError as e:
            test("ckcc sign --finalize succeeded", False, str(e))
            finalized_path = None

        if finalized_path and os.path.exists(finalized_path):
            with open(finalized_path, "rb") as f:
                finalized_bytes = f.read()

            is_raw = is_raw_transaction(finalized_bytes)
            test("finalized output is raw transaction", is_raw,
                 f"first4={finalized_bytes[:4].hex()}")

            if is_raw:
                raw = Transaction.from_string(finalized_bytes.hex())
                has_witness = any(
                    inp.witness and len(inp.witness.items) > 0
                    for inp in raw.vin
                )
                has_scriptsig = any(
                    len(inp.script_sig.data) > 0
                    for inp in raw.vin
                )
                test("finalized tx has witness data", has_witness)
                test("finalized tx has empty scriptSig", not has_scriptsig,
                     "P2WPKH should have empty scriptSig!")

                # Try to broadcast
                try:
                    raw_hex = finalized_bytes.hex()
                    txid = cli.run("sendrawtransaction", raw_hex)
                    test("finalized tx broadcast succeeded", len(txid) == 64,
                         f"txid: {txid}")

                    # Mine a block
                    api_post(server_url, "/api/mine", {"blocks": 1})
                    decoded = cli.run_json("getrawtransaction", txid, "true")
                    test("finalized tx confirmed", decoded.get("confirmations", 0) >= 1)
                except RuntimeError as e:
                    test("finalized tx broadcast succeeded", False, str(e))

        # ========================================================
        section("5. Mixed WIF + Coldcard — Partially Signed PSBT")
        # ========================================================
        #
        # THE BUG SCENARIO: Create a PSBT with 2 inputs:
        #   Input 0: Coldcard P2WPKH (with bip32Derivation)
        #   Input 1: WIF P2WPKH (signed by us before giving to Coldcard)
        #
        # When we pre-sign input 1 with the WIF key, then give the
        # PSBT to Coldcard, we need Coldcard to:
        #   1. Sign its own input (0) as P2WPKH
        #   2. Leave input 1 alone (already signed)
        #   3. NOT finalize as P2PKH
        #

        # Fund a new Coldcard address
        cc_addr_1_path = f"{CC_DERIVATION_BASE}/0/1"
        cc_addr_1_val = coldcard_get_address(cc_addr_1_path)
        cc_pubkey_1 = coldcard_get_pubkey(cc_addr_1_path)

        fund_cc2 = api_post(server_url, "/api/faucet",
                            {"address": cc_addr_1_val, "amount": "0.5"})
        test("funded CC addr 0/1", fund_cc2.get("success") is True)

        # Generate a WIF key and derive its P2WPKH address using embit
        from embit import script as embit_script
        from embit.networks import NETWORKS

        wif_privkey = embit_ec.PrivateKey.from_wif(
            # Generate a deterministic test key (not for real use!)
            embit_ec.PrivateKey(b'\x01' * 32).wif(NETWORKS["regtest"])
        )
        wif_key = wif_privkey.wif(NETWORKS["regtest"])
        wif_pubkey = wif_privkey.get_public_key()
        # Derive P2WPKH address
        wif_sc = embit_script.p2wpkh(wif_pubkey)
        wif_addr = wif_sc.address(NETWORKS["regtest"])
        test("WIF key generated", wif_key.startswith(("c", "9")),
             f"prefix: {wif_key[0]}, addr: {wif_addr}")

        fund_wif = api_post(server_url, "/api/faucet",
                            {"address": wif_addr, "amount": "0.3"})
        test("funded WIF address", fund_wif.get("success") is True)

        # Get UTXOs for both
        cc2_utxos = json.loads(
            urlopen(f"{server_url}/api/address/{cc_addr_1_val}/utxo", timeout=10).read()
        )
        wif_utxos = json.loads(
            urlopen(f"{server_url}/api/address/{wif_addr}/utxo", timeout=10).read()
        )
        test("CC2 has UTXOs", len(cc2_utxos) >= 1)
        test("WIF has UTXOs", len(wif_utxos) >= 1)

        cc2_utxo = cc2_utxos[0]
        wif_utxo = wif_utxos[0]

        print(f"\n  Mixed inputs:")
        print(f"    CC:  {cc2_utxo['txid']}:{cc2_utxo['vout']} = {cc2_utxo['value']} sats ({cc_addr_1_val})")
        print(f"    WIF: {wif_utxo['txid']}:{wif_utxo['vout']} = {wif_utxo['value']} sats ({wif_addr})")

        # Get raw txs for witness UTXOs
        cc2_raw_hex = urlopen(f"{server_url}/api/tx/{cc2_utxo['txid']}/hex", timeout=10).read().decode()
        wif_raw_hex = urlopen(f"{server_url}/api/tx/{wif_utxo['txid']}/hex", timeout=10).read().decode()

        cc2_raw_tx = Transaction.from_string(cc2_raw_hex)
        wif_raw_tx = Transaction.from_string(wif_raw_hex)

        cc2_witness_utxo = cc2_raw_tx.vout[cc2_utxo["vout"]]
        wif_witness_utxo = wif_raw_tx.vout[wif_utxo["vout"]]

        # Recipient for mixed tx
        mixed_recipient = cli.run("getnewaddress", "", "bech32", wallet="recipient")
        mixed_send_sats = 79_800_000  # 0.5 + 0.3 - fee

        # Build 2-input PSBT
        mixed_tx = Transaction(
            version=2,
            vin=[
                TransactionInput(
                    txid=bytes.fromhex(cc2_utxo["txid"]),
                    vout=cc2_utxo["vout"],
                    sequence=0xffffffff,
                ),
                TransactionInput(
                    txid=bytes.fromhex(wif_utxo["txid"]),
                    vout=wif_utxo["vout"],
                    sequence=0xffffffff,
                ),
            ],
            vout=[TransactionOutput(
                value=mixed_send_sats,
                script_pubkey=Script.from_address(mixed_recipient),
            )],
            locktime=0,
        )

        mixed_psbt = PSBT(mixed_tx)

        # Input 0: Coldcard with bip32Derivation
        mixed_psbt.inputs[0].witness_utxo = cc2_witness_utxo
        cc2_pubkey_obj = embit_ec.PublicKey.parse(bytes.fromhex(cc_pubkey_1))
        cc2_derivation = DerivationPath(
            fingerprint=xfp_bytes,
            derivation=[84 | 0x80000000, 1 | 0x80000000, 0 | 0x80000000, 0, 1]
        )
        mixed_psbt.inputs[0].bip32_derivations[cc2_pubkey_obj] = cc2_derivation

        # Input 1: WIF (witnessUtxo only, no bip32Derivation)
        mixed_psbt.inputs[1].witness_utxo = wif_witness_utxo

        # Save the UNSIGNED mixed PSBT
        unsigned_mixed_path = os.path.join(tmp_dir, "mixed_unsigned.psbt")
        with open(unsigned_mixed_path, "wb") as f:
            f.write(mixed_psbt.serialize())

        unsigned_analysis = analyze_psbt_fields(mixed_psbt.serialize())
        print(f"\n  Unsigned mixed PSBT analysis:")
        for inp in unsigned_analysis["inputs"]:
            print(f"    Input {inp['index']}: type={inp.get('script_type', '?')}, "
                  f"bip32={inp['has_bip32_derivations']}, "
                  f"witnessUtxo={inp['has_witness_utxo']}")

        # --------------------------------------------------------
        # Test 5a: Sign WIF input first, then give to Coldcard
        # --------------------------------------------------------
        section("5a. Pre-sign WIF input, then Coldcard signs")

        # Sign input 1 with WIF
        wif_privkey = embit_ec.PrivateKey.from_wif(wif_key)
        presigned_psbt = PSBT.parse(mixed_psbt.serialize())
        sigs = presigned_psbt.sign_with(wif_privkey)
        test("WIF pre-signed input 1", sigs > 0, f"signed {sigs} inputs")

        presigned_bytes = presigned_psbt.serialize()
        presigned_analysis = analyze_psbt_fields(presigned_bytes)
        print(f"\n  After WIF signing:")
        for inp in presigned_analysis["inputs"]:
            print(f"    Input {inp['index']}: partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                  f"finalWit={inp['has_final_scriptwitness']}, finalSig={inp['has_final_scriptsig']}")

        test("input 0 still unsigned (no partial sigs)",
             not presigned_analysis["inputs"][0]["has_partial_sigs"])
        test("input 1 has partial sig from WIF",
             presigned_analysis["inputs"][1]["has_partial_sigs"],
             f"num_partial_sigs={presigned_analysis['inputs'][1]['num_partial_sigs']}")

        # Save pre-signed PSBT
        presigned_path = os.path.join(tmp_dir, "mixed_wif_presigned.psbt")
        with open(presigned_path, "wb") as f:
            f.write(presigned_bytes)

        # Now sign with Coldcard
        cc_signed_path = os.path.join(tmp_dir, "mixed_cc_signed.psbt")
        try:
            ckcc_sign(presigned_path, cc_signed_path)
            test("ckcc sign of pre-signed PSBT succeeded", True)
        except RuntimeError as e:
            test("ckcc sign of pre-signed PSBT succeeded", False, str(e))
            cc_signed_path = None

        if cc_signed_path and os.path.exists(cc_signed_path):
            with open(cc_signed_path, "rb") as f:
                cc_signed_bytes = f.read()

            is_psbt_result = cc_signed_bytes[:5] == b"psbt\xff"
            is_raw_result = is_raw_transaction(cc_signed_bytes)

            print(f"\n  Coldcard output type: PSBT={is_psbt_result}, rawTx={is_raw_result}")
            print(f"    Size: {len(cc_signed_bytes)} bytes")
            print(f"    First 10 bytes: {cc_signed_bytes[:10].hex()}")

            if is_psbt_result:
                cc_signed_analysis = analyze_psbt_fields(cc_signed_bytes)
                print(f"\n  After Coldcard signing:")
                for inp in cc_signed_analysis["inputs"]:
                    print(f"    Input {inp['index']}: "
                          f"partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                          f"finalWit={inp['has_final_scriptwitness']}, "
                          f"finalSig={inp['has_final_scriptsig']}, "
                          f"type={inp.get('script_type', '?')}")

                # KEY TESTS for the bug
                inp0 = cc_signed_analysis["inputs"][0]
                inp1 = cc_signed_analysis["inputs"][1]

                test("input 0 (CC): has signature",
                     inp0['has_partial_sigs'] or inp0['has_final_scriptwitness'])
                test("input 0 (CC): NO final_scriptsig (not P2PKH!)",
                     not inp0['has_final_scriptsig'],
                     "BUG: Coldcard finalized as P2PKH!")
                test("input 1 (WIF): preserved partial sig",
                     inp1['has_partial_sigs'] or inp1['has_final_scriptwitness'])

                # Try to finalize and broadcast
                try:
                    from embit.finalizer import finalize_psbt
                    final_psbt = PSBT.parse(cc_signed_bytes)
                    finalize_psbt(final_psbt)
                    final_tx = final_psbt.final_tx()
                    final_hex = final_tx.serialize().hex()

                    test("mixed PSBT finalized successfully", len(final_hex) > 0)

                    # Check witness vs scriptsig
                    for i, inp in enumerate(final_tx.vin):
                        has_wit = inp.witness and len(inp.witness.items) > 0
                        has_sig = len(inp.script_sig.data) > 0
                        test(f"finalized input {i}: has witness",
                             has_wit, f"witness={has_wit}")
                        test(f"finalized input {i}: empty scriptSig",
                             not has_sig,
                             f"NON-EMPTY scriptSig = P2PKH finalization bug!")

                    # Broadcast
                    txid = cli.run("sendrawtransaction", final_hex)
                    test("mixed tx broadcast succeeded", len(txid) == 64,
                         f"txid: {txid}")

                    api_post(server_url, "/api/mine", {"blocks": 1})
                    decoded = cli.run_json("getrawtransaction", txid, "true")
                    test("mixed tx confirmed", decoded.get("confirmations", 0) >= 1)

                except Exception as e:
                    test("mixed PSBT finalization", False, str(e))
                    traceback.print_exc()

            elif is_raw_result:
                # Coldcard finalized everything — check for the bug
                print(f"\n  ⚠ Coldcard produced raw transaction (finalized all inputs)")
                raw = Transaction.from_string(cc_signed_bytes.hex())

                for i, inp in enumerate(raw.vin):
                    has_wit = inp.witness and len(inp.witness.items) > 0
                    has_sig = len(inp.script_sig.data) > 0
                    test(f"raw tx input {i}: has witness",
                         has_wit, f"witness={has_wit}")
                    test(f"raw tx input {i}: empty scriptSig",
                         not has_sig,
                         f"BUG: NON-EMPTY scriptSig = P2PKH finalization!")

                    if has_sig:
                        print(f"    ⚠ Input {i} scriptSig ({len(inp.script_sig.data)} bytes): "
                              f"{inp.script_sig.data.hex()[:60]}...")

                # Try broadcasting even if we suspect the bug
                try:
                    raw_hex = cc_signed_bytes.hex()
                    txid = cli.run("sendrawtransaction", raw_hex)
                    test("raw tx broadcast succeeded", len(txid) == 64,
                         f"txid: {txid}")
                except RuntimeError as e:
                    error_msg = str(e)
                    test("raw tx broadcast succeeded", False, error_msg)
                    if "scriptSig" in error_msg or "script-verify" in error_msg:
                        print(f"\n  ❌ CONFIRMED BUG: Coldcard finalized P2WPKH as P2PKH")
                        print(f"     Error: {error_msg}")

        # --------------------------------------------------------
        # Test 5b: Give UNSIGNED PSBT to Coldcard (without WIF input witness data)
        # --------------------------------------------------------
        section("5b. Unsigned PSBT to Coldcard (stripped WIF witnessUtxo)")

        # Create a copy where input 1 has NO witnessUtxo
        # (to prevent Coldcard from recognizing/signing it)
        stripped_psbt = PSBT.parse(mixed_psbt.serialize())
        stripped_psbt.inputs[1].witness_utxo = None

        stripped_path = os.path.join(tmp_dir, "mixed_stripped.psbt")
        with open(stripped_path, "wb") as f:
            f.write(stripped_psbt.serialize())

        stripped_analysis = analyze_psbt_fields(stripped_psbt.serialize())
        print(f"\n  Stripped PSBT (no witnessUtxo on input 1):")
        for inp in stripped_analysis["inputs"]:
            print(f"    Input {inp['index']}: witnessUtxo={inp['has_witness_utxo']}, "
                  f"bip32={inp['has_bip32_derivations']}")

        test("input 0 has witnessUtxo", stripped_analysis["inputs"][0]["has_witness_utxo"])
        test("input 1 has NO witnessUtxo (stripped)", not stripped_analysis["inputs"][1]["has_witness_utxo"])

        # Sign with Coldcard
        stripped_signed_path = os.path.join(tmp_dir, "mixed_stripped_signed.psbt")
        try:
            ckcc_sign(stripped_path, stripped_signed_path)
            test("ckcc sign of stripped PSBT succeeded", True)
        except RuntimeError as e:
            test("ckcc sign of stripped PSBT succeeded", False, str(e))
            stripped_signed_path = None

        if stripped_signed_path and os.path.exists(stripped_signed_path):
            with open(stripped_signed_path, "rb") as f:
                stripped_signed_bytes = f.read()

            is_psbt_result = stripped_signed_bytes[:5] == b"psbt\xff"
            is_raw_result = is_raw_transaction(stripped_signed_bytes)

            print(f"\n  Coldcard output (stripped): PSBT={is_psbt_result}, rawTx={is_raw_result}")

            if is_psbt_result:
                ss_analysis = analyze_psbt_fields(stripped_signed_bytes)
                print(f"\n  After Coldcard signing (stripped PSBT):")
                for inp in ss_analysis["inputs"]:
                    print(f"    Input {inp['index']}: "
                          f"partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                          f"finalWit={inp['has_final_scriptwitness']}, "
                          f"finalSig={inp['has_final_scriptsig']}")

                test("stripped: CC signed only its input (0)",
                     ss_analysis["inputs"][0]["has_partial_sigs"] or
                     ss_analysis["inputs"][0]["has_final_scriptwitness"])
                test("stripped: input 1 left unsigned",
                     not ss_analysis["inputs"][1]["has_partial_sigs"] and
                     not ss_analysis["inputs"][1]["has_final_scriptwitness"])

                # Now combine: sign input 1 with WIF, then finalize
                combined_psbt = PSBT.parse(stripped_signed_bytes)
                # Restore witnessUtxo for input 1 so we can sign it
                combined_psbt.inputs[1].witness_utxo = wif_witness_utxo
                sigs2 = combined_psbt.sign_with(wif_privkey)
                test("stripped+WIF: signed input 1", sigs2 > 0, f"signed {sigs2} inputs")

                try:
                    from embit.finalizer import finalize_psbt
                    finalize_psbt(combined_psbt)
                    final_tx2 = combined_psbt.final_tx()
                    final_hex2 = final_tx2.serialize().hex()

                    test("stripped+WIF: finalized successfully", len(final_hex2) > 0)

                    # Check for the bug
                    for i, inp in enumerate(final_tx2.vin):
                        has_wit = inp.witness and len(inp.witness.items) > 0
                        has_sig = len(inp.script_sig.data) > 0
                        test(f"stripped+WIF finalized input {i}: has witness", has_wit)
                        test(f"stripped+WIF finalized input {i}: empty scriptSig",
                             not has_sig)

                    txid2 = cli.run("sendrawtransaction", final_hex2)
                    test("stripped+WIF tx broadcast succeeded", len(txid2) == 64,
                         f"txid: {txid2}")

                    api_post(server_url, "/api/mine", {"blocks": 1})
                    decoded2 = cli.run_json("getrawtransaction", txid2, "true")
                    test("stripped+WIF tx confirmed", decoded2.get("confirmations", 0) >= 1)

                except Exception as e:
                    test("stripped+WIF finalization", False, str(e))
                    traceback.print_exc()

            elif is_raw_result:
                print(f"  ⚠ Coldcard still produced raw tx even with stripped witnessUtxo!")
                test("stripped: Coldcard should NOT produce raw tx", False,
                     "Coldcard found the key via key pool even without witnessUtxo")

        # --------------------------------------------------------
        # Test 5c: Virtual disk auto-sign (if available)
        # --------------------------------------------------------
        if os.path.isdir(COLDCARD_VOLUME):
            section("5c. Virtual Disk Auto-sign Test")

            # Create a fresh single-input PSBT for Coldcard
            cc_addr_2_path = f"{CC_DERIVATION_BASE}/0/2"
            cc_addr_2 = coldcard_get_address(cc_addr_2_path)
            cc_pubkey_2 = coldcard_get_pubkey(cc_addr_2_path)

            fund_cc3 = api_post(server_url, "/api/faucet",
                                {"address": cc_addr_2, "amount": "0.2"})
            test("vdisk: funded CC addr 0/2", fund_cc3.get("success") is True)

            cc3_utxos = json.loads(
                urlopen(f"{server_url}/api/address/{cc_addr_2}/utxo", timeout=10).read()
            )
            cc3_utxo = cc3_utxos[0]

            cc3_raw_hex = urlopen(
                f"{server_url}/api/tx/{cc3_utxo['txid']}/hex", timeout=10
            ).read().decode()
            cc3_raw_tx = Transaction.from_string(cc3_raw_hex)
            cc3_witness_utxo = cc3_raw_tx.vout[cc3_utxo["vout"]]

            vdisk_recipient = cli.run("getnewaddress", "", "bech32", wallet="recipient")
            vdisk_send = 19_900_000

            vdisk_tx = Transaction(
                version=2,
                vin=[TransactionInput(
                    txid=bytes.fromhex(cc3_utxo["txid"]),
                    vout=cc3_utxo["vout"],
                    sequence=0xffffffff,
                )],
                vout=[TransactionOutput(
                    value=vdisk_send,
                    script_pubkey=Script.from_address(vdisk_recipient),
                )],
                locktime=0,
            )

            vdisk_psbt = PSBT(vdisk_tx)
            vdisk_psbt.inputs[0].witness_utxo = cc3_witness_utxo
            vdisk_psbt.inputs[0].bip32_derivations[embit_ec.PublicKey.parse(bytes.fromhex(cc_pubkey_2))] = DerivationPath(
                fingerprint=xfp_bytes,
                derivation=[84 | 0x80000000, 1 | 0x80000000, 0 | 0x80000000, 0, 2]
            )

            vdisk_psbt_path = os.path.join(tmp_dir, "vdisk_test.psbt")
            with open(vdisk_psbt_path, "wb") as f:
                f.write(vdisk_psbt.serialize())

            try:
                signed_vdisk_path = ckcc_sign_virtual_disk(vdisk_psbt_path, timeout=30)
                test("vdisk: auto-sign produced output", True,
                     f"file: {os.path.basename(signed_vdisk_path)}")

                with open(signed_vdisk_path, "rb") as f:
                    vdisk_signed = f.read()

                is_psbt_v = vdisk_signed[:5] == b"psbt\xff"
                is_raw_v = is_raw_transaction(vdisk_signed)
                test("vdisk: output type",
                     is_psbt_v or is_raw_v,
                     f"PSBT={is_psbt_v}, rawTx={is_raw_v}")

                if is_psbt_v:
                    va = analyze_psbt_fields(vdisk_signed)
                    print(f"    PSBT: {va['inputs'][0]}")
                elif is_raw_v:
                    print(f"    Raw tx: {len(vdisk_signed)} bytes")

            except TimeoutError as e:
                test("vdisk: auto-sign produced output", False, str(e))
                # List what's on the volume
                print(f"  Files on volume: {os.listdir(COLDCARD_VOLUME)}")

            # Clean up volume
            for f in os.listdir(COLDCARD_VOLUME):
                fpath = os.path.join(COLDCARD_VOLUME, f)
                if f.endswith((".psbt", ".txn")) and os.path.isfile(fpath):
                    os.remove(fpath)

    except Exception as e:
        print(f"\n  ❌ Unexpected error: {e}")
        traceback.print_exc()

    finally:
        # Clean up
        if server_proc:
            stop_server(server_proc)
            print("\n  Server stopped.")
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 60)
    print("  Coldcard MK4 Signing Tests")
    print("=" * 60)

    if not check_prerequisites():
        print("\n❌ Prerequisites not met. Aborting.")
        sys.exit(1)

    run_tests()

    # Summary
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
