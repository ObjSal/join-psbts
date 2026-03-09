#!/usr/bin/env python3
"""
Test real Coldcard MK4 signing on testnet4 with broadcast.

Spends both CC UTXO + WIF UTXO into a single output back to the WIF address,
returning all funds (minus fee) to the testnet4 wallet.

Requires:
  - Coldcard MK4 plugged in, unlocked, set to XTN (testnet)
  - ckcc CLI: pip install ckcc-protocol
  - embit: pip install embit
  - TESTNET4_WIF and TESTNET4_ADDRESS env vars set
"""

import json
import os
import subprocess
import sys
import time
import traceback
from urllib.request import urlopen, Request

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))

# Derivation path for the Coldcard input (BIP84, first receive address)
CC_DERIV_PATH = "m/84'/1'/0'/0/0"

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

    result = subprocess.run(["ckcc", "pubkey", CC_DERIV_PATH],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ckcc pubkey failed: {result.stderr.strip()}")
    pubkey = result.stdout.strip()

    # Derive address locally from pubkey (avoids ckcc addr which
    # shows address on Coldcard screen and blocks USB until dismissed)
    addr = pubkey_to_p2wpkh(pubkey, "test")

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


def fetch_json(url):
    return json.loads(urlopen(url, timeout=30).read().decode())


def broadcast_tx(raw_hex):
    """Broadcast raw tx hex via mempool.space testnet4 API."""
    req = Request(f"{MEMPOOL_API}/tx", data=raw_hex.encode(),
                  headers={"Content-Type": "text/plain"}, method="POST")
    resp = urlopen(req, timeout=30)
    return resp.read().decode().strip()


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
    section("1. Setup & Verify")
    # ========================================================

    # Check env vars
    wif_key = os.environ.get("TESTNET4_WIF")
    wif_address = os.environ.get("TESTNET4_ADDRESS")
    test("TESTNET4_WIF set", bool(wif_key))
    test("TESTNET4_ADDRESS set", bool(wif_address))
    if not wif_key or not wif_address:
        print("  ❌ Set TESTNET4_WIF and TESTNET4_ADDRESS env vars")
        return

    # Verify Coldcard chain
    time.sleep(0.5)  # let USB settle
    result = subprocess.run(["ckcc", "chain"], capture_output=True, text=True, timeout=30)
    chain = result.stdout.strip()
    test("Coldcard chain is XTN", chain == "XTN", f"got '{chain}'")
    if chain != "XTN":
        print("  ❌ Switch Coldcard to testnet (XTN)")
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
    print(f"  WIF addr: {wif_address}")

    # ========================================================
    section("2. Fetch UTXOs")
    # ========================================================

    cc_utxos = fetch_json(f"{MEMPOOL_API}/address/{CC_ADDR}/utxo")
    wif_utxos = fetch_json(f"{MEMPOOL_API}/address/{wif_address}/utxo")

    test("CC has UTXOs", len(cc_utxos) >= 1, f"found {len(cc_utxos)}")
    test("WIF has UTXOs", len(wif_utxos) >= 1, f"found {len(wif_utxos)}")
    if not cc_utxos or not wif_utxos:
        print("  ❌ Both addresses need UTXOs")
        return

    cc_utxo = cc_utxos[0]
    wif_utxo = wif_utxos[0]

    cc_sats = cc_utxo["value"]
    wif_sats = wif_utxo["value"]
    total_sats = cc_sats + wif_sats

    print(f"  CC UTXO:  {cc_sats} sats (txid: {cc_utxo['txid'][:16]}...)")
    print(f"  WIF UTXO: {wif_sats} sats (txid: {wif_utxo['txid'][:16]}...)")
    print(f"  Total:    {total_sats} sats ({total_sats/1e8:.8f} BTC)")

    # Fetch raw transactions for witnessUtxo
    cc_raw_hex = urlopen(f"{MEMPOOL_API}/tx/{cc_utxo['txid']}/hex", timeout=30).read().decode()
    time.sleep(0.3)  # rate limiting
    wif_raw_hex = urlopen(f"{MEMPOOL_API}/tx/{wif_utxo['txid']}/hex", timeout=30).read().decode()

    cc_raw_tx = Transaction.from_string(cc_raw_hex)
    wif_raw_tx = Transaction.from_string(wif_raw_hex)

    # Fetch fee rate
    time.sleep(0.3)
    fees = fetch_json(f"{MEMPOOL_API}/v1/fees/recommended")
    fee_rate = max(fees.get("minimumFee", 1), 1)  # at least 1 sat/vB
    # 2-in, 1-out P2WPKH: ~141 vbytes
    vsize = 141
    fee = vsize * fee_rate
    send_sats = total_sats - fee

    print(f"  Fee rate: {fee_rate} sat/vB")
    print(f"  Est fee:  {fee} sats")
    print(f"  Send:     {send_sats} sats → {wif_address}")

    test("send amount is positive", send_sats > 0, f"send_sats={send_sats}")
    if send_sats <= 0:
        return

    # ========================================================
    section("3. Build & Partially Sign PSBT")
    # ========================================================

    # Parse WIF
    wif_privkey = embit_ec.PrivateKey.from_wif(wif_key)
    wif_pubkey = wif_privkey.get_public_key()

    # Build PSBT: CC input (0) + WIF input (1) → single output back to WIF address
    tx = Transaction(version=2,
        vin=[
            TransactionInput(txid=bytes.fromhex(cc_utxo["txid"]),
                             vout=cc_utxo["vout"], sequence=0xffffffff),
            TransactionInput(txid=bytes.fromhex(wif_utxo["txid"]),
                             vout=wif_utxo["vout"], sequence=0xffffffff),
        ],
        vout=[TransactionOutput(value=send_sats,
                                script_pubkey=Script.from_address(wif_address))],
        locktime=0)

    psbt = PSBT(tx)

    # witnessUtxo for both
    psbt.inputs[0].witness_utxo = cc_raw_tx.vout[cc_utxo["vout"]]
    psbt.inputs[1].witness_utxo = wif_raw_tx.vout[wif_utxo["vout"]]

    # bip32Derivation for CC input
    xfp_bytes = bytes.fromhex(CC_XFP)
    cc_pubkey_obj = embit_ec.PublicKey.parse(bytes.fromhex(CC_PUBKEY))
    psbt.inputs[0].bip32_derivations[cc_pubkey_obj] = DerivationPath(
        fingerprint=xfp_bytes,
        derivation=[84 | 0x80000000, 1 | 0x80000000, 0 | 0x80000000, 0, 0]
    )
    test("bip32Derivation set on CC input", bool(psbt.inputs[0].bip32_derivations))

    # Pre-sign WIF input
    wif_sigs = psbt.sign_with(wif_privkey)
    test("WIF input pre-signed", wif_sigs > 0, f"signed {wif_sigs}")

    # Print state
    for i, inp in enumerate(psbt.inputs):
        label = "CC" if i == 0 else "WIF"
        n_partial = len(inp.partial_sigs) if inp.partial_sigs else 0
        print(f"    Input {i} ({label}): witnessUtxo={'✓' if inp.witness_utxo else '✗'}, "
              f"bip32={'✓' if inp.bip32_derivations else '✗'}, partial_sigs={n_partial}")

    test("CC input unsigned", not bool(psbt.inputs[0].partial_sigs))
    test("WIF input has partial_sigs", bool(psbt.inputs[1].partial_sigs))

    # Write to temp file
    tmp_dir = os.path.join(_TEST_DIR, "_tmp_cc")
    os.makedirs(tmp_dir, exist_ok=True)
    psbt_in = os.path.join(tmp_dir, "testnet4-unsigned.psbt")
    psbt_out = os.path.join(tmp_dir, "testnet4-signed.psbt")
    with open(psbt_in, "wb") as f:
        f.write(psbt.serialize())
    print(f"\n  PSBT: {psbt_in} ({os.path.getsize(psbt_in)} bytes)")

    # ========================================================
    section("4. Coldcard Signing")
    # ========================================================

    if os.path.exists(psbt_out):
        os.remove(psbt_out)

    print("  Sending PSBT to Coldcard...")
    print("  >>> APPROVE THE TRANSACTION ON YOUR COLDCARD <<<")
    print()

    sign_result = subprocess.run(
        ["ckcc", "sign", psbt_in, psbt_out],
        capture_output=True, text=True, timeout=300)

    test("ckcc sign succeeded", sign_result.returncode == 0,
         f"stderr: {sign_result.stderr.strip()}")
    if sign_result.returncode != 0:
        print(f"  stdout: {sign_result.stdout}")
        print(f"  stderr: {sign_result.stderr}")
        return

    test("signed file created", os.path.exists(psbt_out))

    # ========================================================
    section("5. Analyze Signed PSBT")
    # ========================================================

    with open(psbt_out, "rb") as f:
        signed_data = f.read()

    is_psbt = signed_data[:5] == b"psbt\xff"
    test("output is PSBT format", is_psbt)

    if not is_psbt:
        print(f"  First bytes: {signed_data[:10].hex()}")
        print("  Unexpected format — cannot continue")
        return

    signed_psbt = PSBT.parse(signed_data)

    for i, inp in enumerate(signed_psbt.inputs):
        label = "CC" if i == 0 else "WIF"
        n_partial = len(inp.partial_sigs) if inp.partial_sigs else 0
        print(f"    Input {i} ({label}): partial_sigs={n_partial}, "
              f"final_wit={'✓' if inp.final_scriptwitness else '✗'}, "
              f"final_sig={'✓' if inp.final_scriptsig else '✗'}")

    cc_signed = (bool(signed_psbt.inputs[0].partial_sigs) or
                 signed_psbt.inputs[0].final_scriptwitness is not None)
    test("CC signed input 0", cc_signed)
    test("no P2PKH bug (no final_scriptsig)", signed_psbt.inputs[0].final_scriptsig is None)

    wif_preserved = (bool(signed_psbt.inputs[1].partial_sigs) or
                     signed_psbt.inputs[1].final_scriptwitness is not None)
    test("WIF sig preserved on input 1", wif_preserved)
    test("WIF witnessUtxo preserved", signed_psbt.inputs[1].witness_utxo is not None)

    # ========================================================
    section("6. Finalize & Broadcast to Testnet4")
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
            test(f"input {i} ({label}): empty scriptSig", not has_sig)

        # Broadcast
        print(f"\n  Broadcasting to testnet4...")
        txid = broadcast_tx(final_hex)
        test("broadcast accepted", len(txid) == 64, f"response: {txid[:80]}")
        print(f"  TXID: {txid}")
        print(f"  https://mempool.space/testnet4/tx/{txid}")

        # Wait briefly and verify
        print(f"\n  Waiting 5s for mempool propagation...")
        time.sleep(5)

        # Check mempool for the tx
        try:
            tx_data = fetch_json(f"{MEMPOOL_API}/tx/{txid}")
            test("tx visible in mempool", tx_data.get("txid") == txid)
            status = tx_data.get("status", {})
            print(f"  Confirmed: {status.get('confirmed', False)}")
        except Exception as e:
            test("tx visible in mempool", False, str(e))

        # Verify funds returned to WIF address
        try:
            wif_utxos_after = fetch_json(f"{MEMPOOL_API}/address/{wif_address}/utxo")
            returned = any(u["txid"] == txid for u in wif_utxos_after)
            if returned:
                returned_utxo = next(u for u in wif_utxos_after if u["txid"] == txid)
                test("funds returned to WIF address", returned_utxo["value"] == send_sats,
                     f"expected {send_sats}, got {returned_utxo['value']}")
                print(f"  Returned: {returned_utxo['value']} sats to {wif_address}")
            else:
                # May not show in UTXOs yet if unconfirmed, check the tx outputs
                test("return output in tx", any(
                    vout.get("scriptpubkey_address") == wif_address
                    for vout in tx_data.get("vout", [])
                ), "output not found in tx")
        except Exception as e:
            print(f"  (UTXO check skipped: {e})")

        print(f"\n  ✅ SUCCESS — tx broadcast to testnet4!")
        print(f"  All funds returned to {wif_address}")

    except Exception as e:
        test("finalize/broadcast", False, str(e))
        traceback.print_exc()

    # Clean up temp files
    for f in [psbt_in, psbt_out]:
        if os.path.exists(f):
            os.remove(f)


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 60)
    print("  Coldcard MK4 Testnet4 Signing Test")
    print("  (Real device + real testnet4 broadcast)")
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
