#!/usr/bin/env python3
"""
Test real Coldcard MK4 signing via ckcc CLI.

Requires:
  - Coldcard MK4 plugged in and unlocked
  - ckcc CLI: pip install ckcc-protocol
  - bitcoind/bitcoin-cli in PATH
  - embit: pip install embit
  - server/server.py for regtest node

This test:
1. Starts a regtest node
2. Funds the Coldcard's first BIP84 address + a WIF address
3. Creates a PSBT with proper bip32Derivation for the CC input
4. Pre-signs the WIF input (single partially-signed PSBT approach)
5. Sends to Coldcard via `ckcc sign` (user approves on device)
6. Analyzes the signed result — checks for P2PKH bug
7. Finalizes and broadcasts on regtest

Usage:
  python3 tests/_test_coldcard_regtest.py
"""

import glob
import json
import os
import signal
import socket
import subprocess
import sys
import time
import traceback
from urllib.request import urlopen, Request

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TEST_DIR)

# Derivation path for the Coldcard input (BIP84, first receive address)
CC_DERIV_PATH = "m/84'/1'/0'/0/0"


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

    result = subprocess.run(["ckcc", "pubkey", CC_DERIV_PATH],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ckcc pubkey failed: {result.stderr.strip()}")
    pubkey = result.stdout.strip()

    # Derive address locally from pubkey (avoids ckcc addr which
    # shows address on Coldcard screen and blocks USB until dismissed)
    addr = pubkey_to_p2wpkh(pubkey, "regtest")

    return xfp, addr, pubkey


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
# Helpers
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
    for _ in range(90):
        try:
            resp = urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "ok" and data.get("regtest"):
                return proc, data
        except Exception:
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


def api_post(base_url, path, data):
    body = json.dumps(data).encode("utf-8")
    req = Request(f"{base_url}{path}", data=body,
                  headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urlopen(req, timeout=30).read().decode("utf-8"))


# ============================================================
# Main test
# ============================================================

def run_tests():
    from embit.psbt import PSBT, DerivationPath
    from embit.transaction import Transaction, TransactionInput, TransactionOutput
    from embit.script import Script
    from embit import ec as embit_ec, script as embit_script
    from embit.networks import NETWORKS
    from embit.finalizer import finalize_psbt

    # ========================================================
    section("1. Start Server & Detect Coldcard")
    # ========================================================

    # Start regtest server FIRST (before ckcc calls) to avoid the server
    # subprocess inheriting HID file descriptors from ckcc processes
    port = find_free_port()
    server_url = f"http://127.0.0.1:{port}"
    server_proc = None
    tmp_dir = os.path.join(_TEST_DIR, "_tmp_cc")
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        server_proc, health = start_server(port)
        test("regtest server started", True)

        # Detect Coldcard AFTER server is started
        print("  Auto-detecting Coldcard device info...")
        try:
            xfp, cc_addr_0, cc_pubkey_0 = detect_coldcard()
        except RuntimeError as e:
            test("ckcc can reach Coldcard", False, str(e))
            print("  ❌ Cannot reach Coldcard. Is it plugged in and unlocked?")
            return

        test("ckcc can reach Coldcard", len(xfp) == 8, f"xfp='{xfp}'")

        # Verify chain is regtest
        time.sleep(0.5)  # let USB settle after detect_coldcard
        chain_result = subprocess.run(["ckcc", "chain"], capture_output=True, text=True, timeout=30)
        chain = chain_result.stdout.strip()
        test("Coldcard chain is XRT (regtest)", chain == "XRT", f"got '{chain}'")
        if chain != "XRT":
            print("  ❌ Switch Coldcard to regtest (XRT)")
            return
        print(f"  XFP:    {xfp}")
        print(f"  Addr:   {cc_addr_0}")
        print(f"  Pubkey: {cc_pubkey_0}")

        # ========================================================
        section("2. Fund Addresses")
        # ========================================================

        # Fund Coldcard address (1.0 BTC)
        fund_cc = api_post(server_url, "/api/faucet",
                           {"address": cc_addr_0, "amount": "1.0"})
        test("funded CC addr", fund_cc.get("success"), str(fund_cc))

        # Generate WIF key for second input
        wif_privkey = embit_ec.PrivateKey(b'\x07' * 32)
        wif_key = wif_privkey.wif(NETWORKS["regtest"])
        wif_pubkey = wif_privkey.get_public_key()
        wif_addr = embit_script.p2wpkh(wif_pubkey).address(NETWORKS["regtest"])

        # Fund WIF address (0.5 BTC)
        fund_wif = api_post(server_url, "/api/faucet",
                            {"address": wif_addr, "amount": "0.5"})
        test("funded WIF addr", fund_wif.get("success"))

        print(f"  CC addr:  {cc_addr_0}")
        print(f"  WIF addr: {wif_addr}")

        # Get UTXOs
        cc_utxos = json.loads(urlopen(f"{server_url}/api/address/{cc_addr_0}/utxo").read())
        wif_utxos = json.loads(urlopen(f"{server_url}/api/address/{wif_addr}/utxo").read())
        test("CC UTXO found", len(cc_utxos) >= 1)
        test("WIF UTXO found", len(wif_utxos) >= 1)

        cc_utxo = cc_utxos[0]
        wif_utxo = wif_utxos[0]

        # Get raw transactions for witnessUtxo
        cc_raw_tx = Transaction.from_string(
            urlopen(f"{server_url}/api/tx/{cc_utxo['txid']}/hex").read().decode())
        wif_raw_tx = Transaction.from_string(
            urlopen(f"{server_url}/api/tx/{wif_utxo['txid']}/hex").read().decode())

        # ========================================================
        section("3. Build & Partially Sign PSBT")
        # ========================================================

        # Recipient address
        recip_addr = embit_script.p2wpkh(
            embit_ec.PrivateKey(b'\x08' * 32).get_public_key()
        ).address(NETWORKS["regtest"])
        send_sats = 149_500_000  # 1.495 BTC, leaving room for fee

        # Build PSBT: CC input (index 0) + WIF input (index 1)
        tx = Transaction(version=2,
            vin=[
                TransactionInput(txid=bytes.fromhex(cc_utxo["txid"]),
                                 vout=cc_utxo["vout"], sequence=0xffffffff),
                TransactionInput(txid=bytes.fromhex(wif_utxo["txid"]),
                                 vout=wif_utxo["vout"], sequence=0xffffffff),
            ],
            vout=[TransactionOutput(value=send_sats,
                                    script_pubkey=Script.from_address(recip_addr))],
            locktime=0)

        psbt = PSBT(tx)

        # Set witnessUtxo for both inputs
        psbt.inputs[0].witness_utxo = cc_raw_tx.vout[cc_utxo["vout"]]
        psbt.inputs[1].witness_utxo = wif_raw_tx.vout[wif_utxo["vout"]]

        # Add bip32Derivation for CC input (Coldcard needs this to recognize its input)
        xfp_bytes = bytes.fromhex(xfp)
        cc_pubkey_obj = embit_ec.PublicKey.parse(bytes.fromhex(cc_pubkey_0))
        psbt.inputs[0].bip32_derivations[cc_pubkey_obj] = DerivationPath(
            fingerprint=xfp_bytes,
            derivation=[84 | 0x80000000, 1 | 0x80000000, 0 | 0x80000000, 0, 0]
        )
        test("bip32Derivation set on CC input", bool(psbt.inputs[0].bip32_derivations))

        # Pre-sign WIF input (single partially-signed PSBT approach)
        wif_sigs = psbt.sign_with(wif_privkey)
        test("WIF input pre-signed", wif_sigs > 0, f"signed {wif_sigs} inputs")

        # Print PSBT state
        print(f"\n  PSBT state before Coldcard:")
        for i, inp in enumerate(psbt.inputs):
            label = "CC" if i == 0 else "WIF"
            print(f"    Input {i} ({label}): "
                  f"witnessUtxo={'✓' if inp.witness_utxo else '✗'}, "
                  f"bip32={'✓' if inp.bip32_derivations else '✗'}, "
                  f"partial_sigs={len(inp.partial_sigs) if inp.partial_sigs else 0}")

        test("CC input: unsigned", not bool(psbt.inputs[0].partial_sigs))
        test("WIF input: has partial_sigs", bool(psbt.inputs[1].partial_sigs))
        test("CC input: has witnessUtxo", psbt.inputs[0].witness_utxo is not None)
        test("WIF input: has witnessUtxo", psbt.inputs[1].witness_utxo is not None)

        # Write PSBT to temp file
        psbt_in_path = os.path.join(tmp_dir, "mixed-unsigned.psbt")
        psbt_out_path = os.path.join(tmp_dir, "mixed-signed.psbt")
        with open(psbt_in_path, "wb") as f:
            f.write(psbt.serialize())
        print(f"\n  PSBT written: {psbt_in_path} ({os.path.getsize(psbt_in_path)} bytes)")

        # ========================================================
        section("4. Coldcard Signing via ckcc CLI")
        # ========================================================

        # Remove old signed file if it exists
        if os.path.exists(psbt_out_path):
            os.remove(psbt_out_path)

        print("  Sending PSBT to Coldcard for signing...")
        print("  >>> APPROVE THE TRANSACTION ON YOUR COLDCARD <<<")
        print()

        # ckcc sign: uploads PSBT, waits for user approval, downloads signed result
        # Do NOT use -f (finalize) — we want partial_sigs so we can inspect them
        sign_result = subprocess.run(
            ["ckcc", "sign", psbt_in_path, psbt_out_path],
            capture_output=True, text=True, timeout=300)  # 5 min timeout for user approval

        test("ckcc sign succeeded", sign_result.returncode == 0,
             f"stderr: {sign_result.stderr.strip()}")
        if sign_result.returncode != 0:
            print(f"  stdout: {sign_result.stdout.strip()}")
            print(f"  stderr: {sign_result.stderr.strip()}")
            return

        test("signed file created", os.path.exists(psbt_out_path))
        if not os.path.exists(psbt_out_path):
            return

        # ========================================================
        section("5. Analyze Signed PSBT")
        # ========================================================

        with open(psbt_out_path, "rb") as f:
            signed_data = f.read()
        print(f"  Signed file size: {len(signed_data)} bytes")

        # Check if it's a PSBT or raw tx
        is_psbt = signed_data[:5] == b"psbt\xff"
        test("output is PSBT format", is_psbt,
             f"first bytes: {signed_data[:10].hex()}")

        if is_psbt:
            signed_psbt = PSBT.parse(signed_data)

            for i, inp in enumerate(signed_psbt.inputs):
                label = "CC" if i == 0 else "WIF"
                n_partial = len(inp.partial_sigs) if inp.partial_sigs else 0
                print(f"    Input {i} ({label}): "
                      f"partial_sigs={n_partial}, "
                      f"final_scriptwitness={'✓' if inp.final_scriptwitness else '✗'}, "
                      f"final_scriptsig={'✓' if inp.final_scriptsig else '✗'}, "
                      f"witnessUtxo={'✓' if inp.witness_utxo else '✗'}")

            # CC should have signed its input
            cc_has_partial = bool(signed_psbt.inputs[0].partial_sigs)
            cc_has_final_wit = signed_psbt.inputs[0].final_scriptwitness is not None
            cc_signed = cc_has_partial or cc_has_final_wit
            test("CC signed input 0", cc_signed)

            # Critical: check for P2PKH finalization bug
            cc_has_scriptsig = signed_psbt.inputs[0].final_scriptsig is not None
            test("CC did NOT use final_scriptsig (no P2PKH bug)", not cc_has_scriptsig,
                 "P2PKH finalization bug detected! Coldcard put sig in scriptSig instead of witness")

            # WIF sig should be preserved
            wif_has_partial = bool(signed_psbt.inputs[1].partial_sigs)
            wif_has_final_wit = signed_psbt.inputs[1].final_scriptwitness is not None
            wif_preserved = wif_has_partial or wif_has_final_wit
            test("WIF sig preserved on input 1", wif_preserved)

            # WIF witnessUtxo should still be present (not stripped)
            test("WIF witnessUtxo preserved", signed_psbt.inputs[1].witness_utxo is not None,
                 "witnessUtxo was stripped — this would cause finalization issues")

            # ========================================================
            section("6. Finalize & Broadcast")
            # ========================================================

            try:
                final_tx = finalize_psbt(signed_psbt)
                final_hex = final_tx.serialize().hex()
                test("finalization succeeded", True)

                for i, inp in enumerate(final_tx.vin):
                    label = "CC" if i == 0 else "WIF"
                    has_wit = inp.witness and len(inp.witness.items) > 0
                    has_sig = len(inp.script_sig.data) > 0
                    test(f"input {i} ({label}): has witness", has_wit)
                    test(f"input {i} ({label}): empty scriptSig", not has_sig,
                         f"scriptSig len={len(inp.script_sig.data)}")

                # Broadcast via bitcoin-cli
                cli_base = ["bitcoin-cli",
                            f"-datadir={health['datadir']}", "-regtest",
                            f"-rpcport={health.get('rpc_port', 18443)}",
                            "-rpcuser=test", "-rpcpassword=test"]
                bc_result = subprocess.run(cli_base + ["sendrawtransaction", final_hex],
                                           capture_output=True, text=True, timeout=30)
                if bc_result.returncode == 0:
                    txid = bc_result.stdout.strip()
                    test("transaction broadcast accepted", len(txid) == 64)
                    api_post(server_url, "/api/mine", {"blocks": 1})
                    print(f"\n  TXID: {txid}")

                    # Verify tx is confirmed in a block
                    tx_info = subprocess.run(
                        cli_base + ["gettransaction", txid],
                        capture_output=True, text=True, timeout=30)
                    if tx_info.returncode == 0:
                        tx_data = json.loads(tx_info.stdout)
                        confirmations = tx_data.get("confirmations", 0)
                        test("transaction confirmed in block", confirmations >= 1,
                             f"confirmations={confirmations}")
                        print(f"  Confirmations: {confirmations}")
                    else:
                        # gettransaction needs the wallet — try getrawtransaction
                        tx_raw = subprocess.run(
                            cli_base + ["getrawtransaction", txid, "true"],
                            capture_output=True, text=True, timeout=30)
                        if tx_raw.returncode == 0:
                            tx_data = json.loads(tx_raw.stdout)
                            confirmations = tx_data.get("confirmations", 0)
                            test("transaction confirmed in block", confirmations >= 1,
                                 f"confirmations={confirmations}")
                            print(f"  Confirmations: {confirmations}")
                        else:
                            test("transaction query", False, tx_raw.stderr.strip())

                    # Verify recipient received the funds
                    recip_utxos = json.loads(
                        urlopen(f"{server_url}/api/address/{recip_addr}/utxo").read())
                    recip_total = sum(u["value"] for u in recip_utxos)
                    test("recipient received funds", recip_total == send_sats,
                         f"expected {send_sats}, got {recip_total}")
                    print(f"  Recipient balance: {recip_total} sats ({recip_total/1e8:.8f} BTC)")
                    print(f"\n  ✅ FULL SUCCESS — tx mined and funds verified!")

                else:
                    test("transaction broadcast", False, bc_result.stderr.strip())

            except Exception as e:
                test("finalization", False, str(e))
                traceback.print_exc()

        else:
            # Coldcard returned a raw transaction (fully finalized)
            print("  Coldcard returned a raw transaction (fully signed)")
            raw_hex = signed_data.hex()
            raw_tx = Transaction.from_string(raw_hex)

            for i, inp in enumerate(raw_tx.vin):
                label = "CC" if i == 0 else "WIF"
                has_wit = inp.witness and len(inp.witness.items) > 0
                has_sig = len(inp.script_sig.data) > 0
                print(f"    Input {i} ({label}): witness={'✓' if has_wit else '✗'}, "
                      f"scriptSig={'✓' if has_sig else '✗'}")
                test(f"input {i} ({label}): has witness", has_wit)
                test(f"input {i} ({label}): empty scriptSig", not has_sig)

            # Broadcast
            cli_base = ["bitcoin-cli",
                        f"-datadir={health['datadir']}", "-regtest",
                        f"-rpcport={health.get('rpc_port', 18443)}",
                        "-rpcuser=test", "-rpcpassword=test"]
            bc_result = subprocess.run(cli_base + ["sendrawtransaction", raw_hex],
                                       capture_output=True, text=True, timeout=30)
            if bc_result.returncode == 0:
                txid = bc_result.stdout.strip()
                test("raw tx broadcast accepted", len(txid) == 64)
                api_post(server_url, "/api/mine", {"blocks": 1})
                print(f"\n  TXID: {txid}")

                # Verify tx is confirmed
                tx_raw = subprocess.run(
                    cli_base + ["getrawtransaction", txid, "true"],
                    capture_output=True, text=True, timeout=30)
                if tx_raw.returncode == 0:
                    tx_data = json.loads(tx_raw.stdout)
                    confirmations = tx_data.get("confirmations", 0)
                    test("transaction confirmed in block", confirmations >= 1,
                         f"confirmations={confirmations}")

                # Verify recipient received the funds
                recip_utxos = json.loads(
                    urlopen(f"{server_url}/api/address/{recip_addr}/utxo").read())
                recip_total = sum(u["value"] for u in recip_utxos)
                test("recipient received funds", recip_total == send_sats,
                     f"expected {send_sats}, got {recip_total}")
                print(f"  Recipient balance: {recip_total} sats ({recip_total/1e8:.8f} BTC)")
                print(f"\n  ✅ FULL SUCCESS — tx mined and funds verified!")
            else:
                test("raw tx broadcast", False, bc_result.stderr.strip())

    except Exception as e:
        print(f"\n  ❌ Error: {e}")
        traceback.print_exc()

    finally:
        if server_proc:
            stop_server(server_proc)
        # Clean up temp files
        for f in glob.glob(os.path.join(tmp_dir, "*.psbt")):
            os.remove(f)


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 60)
    print("  Coldcard MK4 CLI Signing Test")
    print("  (Real device — approve transaction on Coldcard)")
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
