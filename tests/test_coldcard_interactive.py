#!/usr/bin/env python3
"""
Interactive Coldcard signing test.

Creates PSBTs and signs them via ckcc sign, with long timeouts
for the user to physically approve on the Coldcard device.

Usage:
    python3 tests/test_coldcard_interactive.py
"""

import hashlib
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
CC_DERIVATION_BASE = "m/84'/1'/0'"

_pass_count = 0
_fail_count = 0


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


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port):
    proc = subprocess.Popen(
        [sys.executable, os.path.join(_PROJECT_ROOT, "server", "server.py"),
         str(port), "--regtest"],
        cwd=_PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
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
            raise RuntimeError(f"bitcoin-cli failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def run_json(self, *args, wallet=None):
        return json.loads(self.run(*args, wallet=wallet))


def api_post(base_url, path, data):
    body = json.dumps(data).encode("utf-8")
    req = Request(f"{base_url}{path}", data=body,
                  headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urlopen(req, timeout=30).read().decode("utf-8"))


def ckcc_sign(psbt_path, output_path, finalize=False, timeout=120):
    """Sign PSBT via ckcc. User must approve on Coldcard within timeout."""
    cmd = ["ckcc", "sign"]
    if finalize:
        cmd.append("--finalize")
    cmd.extend([psbt_path, output_path])

    print(f"\n    ╔══════════════════════════════════════════════╗")
    print(f"    ║  👆 APPROVE on Coldcard now! ({timeout}s timeout)  ║")
    print(f"    ╚══════════════════════════════════════════════╝")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"ckcc sign failed: {result.stderr.strip()}")
    return output_path


def is_raw_transaction(data):
    if len(data) < 10:
        return False
    version = int.from_bytes(data[:4], 'little')
    return version in (1, 2)


def analyze_psbt(psbt_bytes):
    """Return per-input analysis of a PSBT."""
    from embit.psbt import PSBT
    psbt = PSBT.parse(psbt_bytes)
    inputs = []
    for i, inp in enumerate(psbt.inputs):
        info = {
            "index": i,
            "has_witness_utxo": inp.witness_utxo is not None,
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


def main():
    from embit.psbt import PSBT, DerivationPath
    from embit.transaction import Transaction, TransactionInput, TransactionOutput
    from embit.script import Script
    from embit import ec as embit_ec, script as embit_script
    from embit.networks import NETWORKS

    print("=" * 60)
    print("  Interactive Coldcard Signing Test")
    print("  You will need to APPROVE transactions on the Coldcard!")
    print("=" * 60)

    # Detect Coldcard
    section("Setup")

    xfp_result = subprocess.run(["ckcc", "xfp"], capture_output=True, text=True, timeout=10)
    cc_xfp = xfp_result.stdout.strip().lower()
    xpub_result = subprocess.run(["ckcc", "xpub", CC_DERIVATION_BASE],
                                  capture_output=True, text=True, timeout=10)
    cc_xpub = xpub_result.stdout.strip()

    print(f"  Coldcard XFP: {cc_xfp}")
    print(f"  Coldcard xpub: {cc_xpub[:30]}...")

    xfp_bytes = bytes.fromhex(cc_xfp)

    # Get first address and pubkey
    addr_result = subprocess.run(["ckcc", "addr", "-s", "-q", f"{CC_DERIVATION_BASE}/0/0"],
                                  capture_output=True, text=True, timeout=10)
    cc_addr_0 = addr_result.stdout.strip()

    pk_result = subprocess.run(["ckcc", "pubkey", f"{CC_DERIVATION_BASE}/0/0"],
                                capture_output=True, text=True, timeout=10)
    cc_pubkey_0 = pk_result.stdout.strip()

    # Get second address and pubkey for mixed test
    addr1_result = subprocess.run(["ckcc", "addr", "-s", "-q", f"{CC_DERIVATION_BASE}/0/1"],
                                   capture_output=True, text=True, timeout=10)
    cc_addr_1 = addr1_result.stdout.strip()

    pk1_result = subprocess.run(["ckcc", "pubkey", f"{CC_DERIVATION_BASE}/0/1"],
                                 capture_output=True, text=True, timeout=10)
    cc_pubkey_1 = pk1_result.stdout.strip()

    print(f"  CC addr 0/0: {cc_addr_0}")
    print(f"  CC addr 0/1: {cc_addr_1}")

    # Start server
    port = find_free_port()
    server_url = f"http://127.0.0.1:{port}"
    server_proc = None
    tmp_dir = tempfile.mkdtemp(prefix="cc_test_")

    try:
        server_proc, health = start_server(port)
        cli = BitcoinCLI(health["datadir"], health.get("rpc_port", 18443))
        print(f"  Regtest server started on port {port}")

        for name in ["cc_faucet", "recipient"]:
            try:
                cli.run("createwallet", name)
            except RuntimeError:
                pass

        # ============================================================
        # TEST 1: Pure Coldcard P2WPKH signing
        # ============================================================
        section("TEST 1: Pure Coldcard P2WPKH Signing")

        # Fund CC address
        fund = api_post(server_url, "/api/faucet", {"address": cc_addr_0, "amount": "1.0"})
        test("funded CC addr", fund.get("success"))

        utxos = json.loads(urlopen(f"{server_url}/api/address/{cc_addr_0}/utxo").read())
        test("CC UTXO found", len(utxos) >= 1)
        utxo = utxos[0]

        # Get witnessUtxo from raw tx
        raw_hex = urlopen(f"{server_url}/api/tx/{utxo['txid']}/hex").read().decode()
        raw_tx = Transaction.from_string(raw_hex)
        witness_utxo = raw_tx.vout[utxo["vout"]]

        # Create PSBT
        recipient = cli.run("getnewaddress", "", "bech32", wallet="recipient")
        tx = Transaction(version=2,
            vin=[TransactionInput(txid=bytes.fromhex(utxo["txid"]), vout=utxo["vout"],
                                  sequence=0xffffffff)],
            vout=[TransactionOutput(value=99_900_000,
                                    script_pubkey=Script.from_address(recipient))],
            locktime=0)

        psbt = PSBT(tx)
        psbt.inputs[0].witness_utxo = witness_utxo
        pubkey_obj = embit_ec.PublicKey.parse(bytes.fromhex(cc_pubkey_0))
        psbt.inputs[0].bip32_derivations[pubkey_obj] = DerivationPath(
            fingerprint=xfp_bytes,
            derivation=[84 | 0x80000000, 1 | 0x80000000, 0 | 0x80000000, 0, 0])

        psbt_path = os.path.join(tmp_dir, "test1.psbt")
        signed_path = os.path.join(tmp_dir, "test1-signed.psbt")
        with open(psbt_path, "wb") as f:
            f.write(psbt.serialize())

        # Analyze unsigned
        analysis = analyze_psbt(psbt.serialize())
        print(f"\n  Unsigned: type={analysis[0]['type']}, bip32={analysis[0]['has_bip32']}")

        # Sign!
        try:
            ckcc_sign(psbt_path, signed_path)
            test("ckcc sign succeeded", True)

            with open(signed_path, "rb") as f:
                signed_bytes = f.read()

            is_psbt = signed_bytes[:5] == b"psbt\xff"
            is_raw = is_raw_transaction(signed_bytes)

            if is_psbt:
                sa = analyze_psbt(signed_bytes)
                print(f"  Signed: partial_sigs={sa[0]['has_partial_sigs']}, "
                      f"finalWit={sa[0]['has_final_scriptwitness']}, "
                      f"finalSig={sa[0]['has_final_scriptsig']}")
                test("has signature", sa[0]['has_partial_sigs'] or sa[0]['has_final_scriptwitness'])
                test("NO P2PKH scriptsig", not sa[0]['has_final_scriptsig'],
                     "BUG: Coldcard signed as P2PKH!")

                # Finalize and broadcast
                from embit.finalizer import finalize_psbt
                final = PSBT.parse(signed_bytes)
                finalize_psbt(final)
                final_tx = final.final_tx()
                final_hex = final_tx.serialize().hex()

                txid = cli.run("sendrawtransaction", final_hex)
                test("broadcast succeeded", len(txid) == 64, f"txid: {txid}")
                api_post(server_url, "/api/mine", {"blocks": 1})

            elif is_raw:
                print(f"  ⚠ Coldcard produced raw tx (finalized)")
                raw = Transaction.from_string(signed_bytes.hex())
                has_wit = any(inp.witness and len(inp.witness.items) > 0 for inp in raw.vin)
                has_sig = any(len(inp.script_sig.data) > 0 for inp in raw.vin)
                test("raw tx uses witness", has_wit)
                test("raw tx empty scriptSig", not has_sig, "BUG: P2PKH!")

                txid = cli.run("sendrawtransaction", signed_bytes.hex())
                test("broadcast succeeded", len(txid) == 64, f"txid: {txid}")
                api_post(server_url, "/api/mine", {"blocks": 1})

        except RuntimeError as e:
            test("ckcc sign succeeded", False, str(e))

        # ============================================================
        # TEST 2: Mixed WIF + Coldcard — Pre-signed WIF input
        # ============================================================
        section("TEST 2: Mixed WIF + Coldcard (pre-signed WIF input)")
        print("  This is the BUG scenario!")

        # Fund CC address 0/1
        fund2 = api_post(server_url, "/api/faucet", {"address": cc_addr_1, "amount": "0.5"})
        test("funded CC addr 0/1", fund2.get("success"))

        # Generate WIF key and address
        wif_privkey = embit_ec.PrivateKey(b'\x01' * 32)
        wif_key = wif_privkey.wif(NETWORKS["regtest"])
        wif_pubkey = wif_privkey.get_public_key()
        wif_addr = embit_script.p2wpkh(wif_pubkey).address(NETWORKS["regtest"])

        fund_wif = api_post(server_url, "/api/faucet", {"address": wif_addr, "amount": "0.3"})
        test("funded WIF addr", fund_wif.get("success"))

        # Get UTXOs
        cc_utxos = json.loads(urlopen(f"{server_url}/api/address/{cc_addr_1}/utxo").read())
        wif_utxos = json.loads(urlopen(f"{server_url}/api/address/{wif_addr}/utxo").read())
        test("CC UTXO found", len(cc_utxos) >= 1)
        test("WIF UTXO found", len(wif_utxos) >= 1)

        cc_utxo = cc_utxos[0]
        wif_utxo = wif_utxos[0]

        print(f"\n  Input 0 (CC):  {cc_utxo['txid'][:16]}... = {cc_utxo['value']} sats")
        print(f"  Input 1 (WIF): {wif_utxo['txid'][:16]}... = {wif_utxo['value']} sats")

        # Get witness UTXOs
        cc_raw = Transaction.from_string(
            urlopen(f"{server_url}/api/tx/{cc_utxo['txid']}/hex").read().decode())
        wif_raw = Transaction.from_string(
            urlopen(f"{server_url}/api/tx/{wif_utxo['txid']}/hex").read().decode())

        # Build 2-input PSBT
        recip2 = cli.run("getnewaddress", "", "bech32", wallet="recipient")
        mixed_tx = Transaction(version=2,
            vin=[
                TransactionInput(txid=bytes.fromhex(cc_utxo["txid"]),
                                 vout=cc_utxo["vout"], sequence=0xffffffff),
                TransactionInput(txid=bytes.fromhex(wif_utxo["txid"]),
                                 vout=wif_utxo["vout"], sequence=0xffffffff),
            ],
            vout=[TransactionOutput(value=79_800_000,
                                    script_pubkey=Script.from_address(recip2))],
            locktime=0)

        mixed_psbt = PSBT(mixed_tx)

        # Input 0: CC with bip32Derivation
        mixed_psbt.inputs[0].witness_utxo = cc_raw.vout[cc_utxo["vout"]]
        cc_pk1_obj = embit_ec.PublicKey.parse(bytes.fromhex(cc_pubkey_1))
        mixed_psbt.inputs[0].bip32_derivations[cc_pk1_obj] = DerivationPath(
            fingerprint=xfp_bytes,
            derivation=[84 | 0x80000000, 1 | 0x80000000, 0 | 0x80000000, 0, 1])

        # Input 1: WIF (witnessUtxo only, NO bip32)
        mixed_psbt.inputs[1].witness_utxo = wif_raw.vout[wif_utxo["vout"]]

        # Pre-sign input 1 with WIF
        sigs = mixed_psbt.sign_with(wif_privkey)
        test("WIF pre-signed", sigs > 0, f"signed {sigs} inputs")

        analysis_presigned = analyze_psbt(mixed_psbt.serialize())
        print(f"\n  After WIF pre-signing:")
        for inp in analysis_presigned:
            print(f"    Input {inp['index']}: partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                  f"finalWit={inp['has_final_scriptwitness']}, finalSig={inp['has_final_scriptsig']}")

        # Save and sign with Coldcard
        mixed_path = os.path.join(tmp_dir, "test2_mixed.psbt")
        mixed_signed_path = os.path.join(tmp_dir, "test2_mixed-signed.psbt")
        with open(mixed_path, "wb") as f:
            f.write(mixed_psbt.serialize())

        try:
            ckcc_sign(mixed_path, mixed_signed_path)
            test("ckcc sign mixed PSBT succeeded", True)

            with open(mixed_signed_path, "rb") as f:
                mixed_signed = f.read()

            is_psbt = mixed_signed[:5] == b"psbt\xff"
            is_raw = is_raw_transaction(mixed_signed)

            print(f"\n  Coldcard output: PSBT={is_psbt}, rawTx={is_raw}")
            print(f"  Size: {len(mixed_signed)} bytes")

            if is_psbt:
                sa = analyze_psbt(mixed_signed)
                print(f"\n  After Coldcard signing:")
                for inp in sa:
                    print(f"    Input {inp['index']}: "
                          f"partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                          f"finalWit={inp['has_final_scriptwitness']}, "
                          f"finalSig={inp['has_final_scriptsig']}")

                # KEY TESTS
                test("CC input (0): signed",
                     sa[0]['has_partial_sigs'] or sa[0]['has_final_scriptwitness'])
                test("CC input (0): NOT P2PKH", not sa[0]['has_final_scriptsig'],
                     "BUG: Coldcard finalized CC input as P2PKH!")
                test("WIF input (1): signature preserved",
                     sa[1]['has_partial_sigs'] or sa[1]['has_final_scriptwitness'])
                test("WIF input (1): NOT P2PKH", not sa[1]['has_final_scriptsig'],
                     "BUG: Coldcard finalized WIF input as P2PKH!")

                # Try to finalize and broadcast
                try:
                    from embit.finalizer import finalize_psbt
                    final = PSBT.parse(mixed_signed)
                    finalize_psbt(final)
                    final_tx = final.final_tx()
                    final_hex = final_tx.serialize().hex()

                    # Check witness vs scriptsig in finalized tx
                    for i, inp in enumerate(final_tx.vin):
                        has_wit = inp.witness and len(inp.witness.items) > 0
                        has_sig = len(inp.script_sig.data) > 0
                        test(f"finalized input {i}: witness={has_wit}", has_wit)
                        test(f"finalized input {i}: empty_scriptSig", not has_sig,
                             f"BUG: P2PKH finalization! scriptSig={inp.script_sig.data.hex()[:40]}...")

                    txid2 = cli.run("sendrawtransaction", final_hex)
                    test("mixed tx broadcast succeeded", len(txid2) == 64, f"txid: {txid2}")
                    api_post(server_url, "/api/mine", {"blocks": 1})

                except Exception as e:
                    test("mixed PSBT finalization", False, str(e))
                    traceback.print_exc()

            elif is_raw:
                print(f"\n  ⚠ Coldcard produced raw transaction!")
                raw = Transaction.from_string(mixed_signed.hex())
                for i, inp in enumerate(raw.vin):
                    has_wit = inp.witness and len(inp.witness.items) > 0
                    has_sig = len(inp.script_sig.data) > 0
                    test(f"raw input {i}: witness={has_wit}", has_wit)
                    test(f"raw input {i}: empty_scriptSig", not has_sig,
                         f"BUG: P2PKH! scriptSig={inp.script_sig.data.hex()[:40]}...")

                try:
                    txid2 = cli.run("sendrawtransaction", mixed_signed.hex())
                    test("mixed raw tx broadcast", len(txid2) == 64, f"txid: {txid2}")
                except RuntimeError as e:
                    test("mixed raw tx broadcast", False, str(e))
                    if "scriptSig" in str(e) or "script-verify" in str(e):
                        print(f"\n  ❌ CONFIRMED BUG: P2WPKH finalized as P2PKH")

        except RuntimeError as e:
            test("ckcc sign mixed PSBT succeeded", False, str(e))

        # ============================================================
        # TEST 3: Stripped witnessUtxo approach
        # ============================================================
        section("TEST 3: Stripped witnessUtxo (prevent CC from signing WIF input)")

        # Fund fresh addresses
        addr2_result = subprocess.run(["ckcc", "addr", "-s", "-q", f"{CC_DERIVATION_BASE}/0/2"],
                                       capture_output=True, text=True, timeout=10)
        cc_addr_2 = addr2_result.stdout.strip()
        pk2_result = subprocess.run(["ckcc", "pubkey", f"{CC_DERIVATION_BASE}/0/2"],
                                     capture_output=True, text=True, timeout=10)
        cc_pubkey_2 = pk2_result.stdout.strip()

        fund3 = api_post(server_url, "/api/faucet", {"address": cc_addr_2, "amount": "0.4"})
        test("funded CC addr 0/2", fund3.get("success"))

        # Generate another WIF
        wif2_privkey = embit_ec.PrivateKey(b'\x02' * 32)
        wif2_addr = embit_script.p2wpkh(wif2_privkey.get_public_key()).address(NETWORKS["regtest"])
        fund_wif2 = api_post(server_url, "/api/faucet", {"address": wif2_addr, "amount": "0.2"})
        test("funded WIF2 addr", fund_wif2.get("success"))

        cc3_utxos = json.loads(urlopen(f"{server_url}/api/address/{cc_addr_2}/utxo").read())
        wif2_utxos = json.loads(urlopen(f"{server_url}/api/address/{wif2_addr}/utxo").read())

        cc3_utxo = cc3_utxos[0]
        wif2_utxo = wif2_utxos[0]

        cc3_raw = Transaction.from_string(
            urlopen(f"{server_url}/api/tx/{cc3_utxo['txid']}/hex").read().decode())
        wif2_raw = Transaction.from_string(
            urlopen(f"{server_url}/api/tx/{wif2_utxo['txid']}/hex").read().decode())

        recip3 = cli.run("getnewaddress", "", "bech32", wallet="recipient")
        stripped_tx = Transaction(version=2,
            vin=[
                TransactionInput(txid=bytes.fromhex(cc3_utxo["txid"]),
                                 vout=cc3_utxo["vout"], sequence=0xffffffff),
                TransactionInput(txid=bytes.fromhex(wif2_utxo["txid"]),
                                 vout=wif2_utxo["vout"], sequence=0xffffffff),
            ],
            vout=[TransactionOutput(value=59_800_000,
                                    script_pubkey=Script.from_address(recip3))],
            locktime=0)

        stripped_psbt = PSBT(stripped_tx)

        # Input 0: CC with full info
        stripped_psbt.inputs[0].witness_utxo = cc3_raw.vout[cc3_utxo["vout"]]
        cc_pk2_obj = embit_ec.PublicKey.parse(bytes.fromhex(cc_pubkey_2))
        stripped_psbt.inputs[0].bip32_derivations[cc_pk2_obj] = DerivationPath(
            fingerprint=xfp_bytes,
            derivation=[84 | 0x80000000, 1 | 0x80000000, 0 | 0x80000000, 0, 2])

        # Input 1: NO witnessUtxo (stripped!) — CC can't sign this
        # (we'll add witnessUtxo back later for WIF signing)

        stripped_path = os.path.join(tmp_dir, "test3_stripped.psbt")
        stripped_signed_path = os.path.join(tmp_dir, "test3_stripped-signed.psbt")
        with open(stripped_path, "wb") as f:
            f.write(stripped_psbt.serialize())

        analysis_stripped = analyze_psbt(stripped_psbt.serialize())
        print(f"\n  Stripped PSBT:")
        for inp in analysis_stripped:
            print(f"    Input {inp['index']}: witnessUtxo={inp['has_witness_utxo']}, "
                  f"bip32={inp['has_bip32']}")

        try:
            ckcc_sign(stripped_path, stripped_signed_path)
            test("ckcc sign stripped PSBT succeeded", True)

            with open(stripped_signed_path, "rb") as f:
                stripped_signed = f.read()

            is_psbt = stripped_signed[:5] == b"psbt\xff"
            is_raw = is_raw_transaction(stripped_signed)

            if is_psbt:
                sa = analyze_psbt(stripped_signed)
                print(f"\n  After Coldcard signing (stripped):")
                for inp in sa:
                    print(f"    Input {inp['index']}: "
                          f"partial_sigs={inp['has_partial_sigs']}({inp['num_partial_sigs']}), "
                          f"finalWit={inp['has_final_scriptwitness']}, "
                          f"finalSig={inp['has_final_scriptsig']}")

                test("CC input (0) signed", sa[0]['has_partial_sigs'] or sa[0]['has_final_scriptwitness'])
                test("WIF input (1) NOT signed (stripped)",
                     not sa[1]['has_partial_sigs'] and not sa[1]['has_final_scriptwitness'],
                     "Coldcard signed it anyway via keypool!")

                # Now add witnessUtxo back and sign with WIF
                combined = PSBT.parse(stripped_signed)
                combined.inputs[1].witness_utxo = wif2_raw.vout[wif2_utxo["vout"]]
                sigs3 = combined.sign_with(wif2_privkey)
                test("WIF signed input 1", sigs3 > 0)

                from embit.finalizer import finalize_psbt
                finalize_psbt(combined)
                final_tx3 = combined.final_tx()
                final_hex3 = final_tx3.serialize().hex()

                for i, inp in enumerate(final_tx3.vin):
                    has_wit = inp.witness and len(inp.witness.items) > 0
                    has_sig = len(inp.script_sig.data) > 0
                    test(f"stripped+WIF input {i}: witness={has_wit}", has_wit)
                    test(f"stripped+WIF input {i}: empty_scriptSig", not has_sig)

                txid3 = cli.run("sendrawtransaction", final_hex3)
                test("stripped+WIF broadcast", len(txid3) == 64, f"txid: {txid3}")
                api_post(server_url, "/api/mine", {"blocks": 1})

            elif is_raw:
                test("stripped: should NOT be raw tx", False,
                     "Coldcard finalized despite missing witnessUtxo!")

        except RuntimeError as e:
            test("ckcc sign stripped PSBT succeeded", False, str(e))

    except Exception as e:
        print(f"\n  ❌ Error: {e}")
        traceback.print_exc()
    finally:
        if server_proc:
            stop_server(server_proc)
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Summary
    print(f"\n{'='*60}")
    print(f"  Results: {_pass_count} passed, {_fail_count} failed")
    print(f"{'='*60}\n")
    sys.exit(1 if _fail_count > 0 else 0)


if __name__ == "__main__":
    main()
