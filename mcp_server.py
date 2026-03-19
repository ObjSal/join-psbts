#!/usr/bin/env python3
"""
MCP Server for Bitcoin Address Sweeper.

Provides tools to fetch UTXOs, create/sign/combine PSBTs, show QR codes
inline for hardware wallet signing, and broadcast transactions.

Dependencies:
    pip install mcp embit "qrcode[pil]"

Usage:
    python3 mcp_server.py

    # Add to Claude Code MCP settings (.claude/settings.json):
    # {
    #   "mcpServers": {
    #     "bitcoin-sweeper": {
    #       "command": "python3",
    #       "args": ["/absolute/path/to/mcp_server.py"]
    #     }
    #   }
    # }
"""

import base64
import io
import json
import sys
import urllib.request
import urllib.error

from mcp.server.fastmcp import FastMCP

# Bitcoin operations
from embit import ec
from embit import script as sc
from embit.psbt import PSBT
from embit.transaction import Transaction, TransactionInput, TransactionOutput
from embit.script import Script, Witness
from embit.networks import NETWORKS

# QR code generation
import qrcode
from PIL import Image

mcp = FastMCP("bitcoin-sweeper")


# ============================================================
# Constants
# ============================================================

MEMPOOL_URLS = {
    "mainnet": "https://mempool.space/api",
    "testnet4": "https://mempool.space/testnet4/api",
    "regtest": "http://localhost:8000/api",
}

NETWORK_MAP = {
    "mainnet": NETWORKS["main"],
    "testnet4": NETWORKS["test"],
    "regtest": NETWORKS["regtest"],
}


# ============================================================
# Helpers
# ============================================================

def _get_network(network: str):
    """Get embit network object."""
    if network not in NETWORK_MAP:
        raise ValueError(f"Unknown network: {network}. Use: mainnet, testnet4, regtest")
    return NETWORK_MAP[network]


def _mempool_url(network: str) -> str:
    """Get mempool.space API base URL for the given network."""
    if network not in MEMPOOL_URLS:
        raise ValueError(f"Unknown network: {network}. Use: mainnet, testnet4, regtest")
    return MEMPOOL_URLS[network]


def _http_get(url: str, timeout: int = 15) -> bytes:
    """HTTP GET request."""
    req = urllib.request.Request(url, headers={"User-Agent": "bitcoin-sweeper-mcp/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_post(url: str, data: bytes, content_type: str = "text/plain", timeout: int = 15) -> bytes:
    """HTTP POST request."""
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": "bitcoin-sweeper-mcp/1.0", "Content-Type": content_type},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _generate_qr_base64(data: str, size: int = 400) -> str:
    """Generate a QR code as a base64-encoded PNG string."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    img = img.resize((size, size), Image.NEAREST)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _estimate_vsize(n_inputs: int, n_outputs: int) -> int:
    """Rough vsize estimate for a segwit transaction."""
    return int(10.5 + 68 * n_inputs + 31 * n_outputs + 0.5)


def _finalize_psbt(psbt: PSBT) -> None:
    """Finalize a signed PSBT by setting final_scriptwitness on each input.

    Handles P2WPKH (partial_sigs → witness) and P2TR (already finalized by embit).
    """
    for inp in psbt.inputs:
        if inp.final_scriptwitness:
            # Already finalized (P2TR key-path or previously finalized)
            continue
        if inp.partial_sigs:
            # P2WPKH: witness = [signature, pubkey]
            for pk, sig in inp.partial_sigs.items():
                inp.final_scriptwitness = Witness([sig, pk.sec()])
                break
            inp.partial_sigs = {}
        else:
            raise ValueError("Input has no signatures — cannot finalize")


def _address_info(wif: str, network: str) -> dict:
    """Derive addresses from a WIF private key."""
    net = _get_network(network)
    key = ec.PrivateKey.from_wif(wif)
    pubkey = key.get_public_key()

    p2wpkh_script = sc.p2wpkh(pubkey)
    p2wpkh_addr = p2wpkh_script.address(net)

    p2tr_script = sc.p2tr(pubkey)
    p2tr_addr = p2tr_script.address(net)

    return {
        "p2wpkh": p2wpkh_addr,
        "p2tr": p2tr_addr,
        "pubkey": pubkey.sec().hex(),
    }


# ============================================================
# MCP Tools
# ============================================================

@mcp.tool()
def fetch_utxos(address: str, network: str = "testnet4") -> str:
    """Fetch unspent transaction outputs (UTXOs) for a Bitcoin address.

    Args:
        address: Bitcoin address (bc1q..., tb1q..., bcrt1q..., etc.)
        network: Network to query (mainnet, testnet4, regtest)

    Returns:
        JSON list of UTXOs with txid, vout, value (sats), and address.
    """
    base_url = _mempool_url(network)
    url = f"{base_url}/address/{address}/utxo"

    try:
        raw = _http_get(url)
        utxos = json.loads(raw)
    except urllib.error.HTTPError as e:
        return json.dumps({"error": f"HTTP {e.code}: {e.reason}", "url": url})
    except Exception as e:
        return json.dumps({"error": str(e)})

    # Enrich with address and scriptPubKey
    script_pubkey = Script.from_address(address)
    spk_hex = script_pubkey.serialize().hex()

    result = []
    for u in utxos:
        result.append({
            "txid": u["txid"],
            "vout": u["vout"],
            "value": u["value"],
            "address": address,
            "scriptPubKey": spk_hex,
        })

    total = sum(u["value"] for u in result)
    return json.dumps({
        "address": address,
        "utxo_count": len(result),
        "total_sats": total,
        "total_btc": f"{total / 1e8:.8f}",
        "utxos": result,
    }, indent=2)


@mcp.tool()
def wif_info(wif: str, network: str = "testnet4") -> str:
    """Derive Bitcoin addresses from a WIF private key.

    Args:
        wif: WIF-encoded private key
        network: Network (mainnet, testnet4, regtest)

    Returns:
        P2WPKH and P2TR addresses derived from this key.
    """
    try:
        info = _address_info(wif, network)
        return json.dumps({
            "p2wpkh_address": info["p2wpkh"],
            "p2tr_address": info["p2tr"],
            "compressed_pubkey": info["pubkey"],
            "network": network,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def create_psbt(
    inputs_json: str,
    outputs_json: str,
    fee_rate: int = 1,
    network: str = "testnet4",
) -> list:
    """Create an unsigned PSBT and display its QR code for hardware wallet signing.

    The QR code is shown inline so users can scan it with their hardware wallet
    (e.g., Coldcard Q) to sign the transaction.

    Args:
        inputs_json: JSON array of inputs. Each: {"txid": "hex", "vout": int, "value": int, "address": "addr"}
                     Optional per-input: "wif" (signs that input inline), "non_witness_utxo_hex" (raw tx hex)
        outputs_json: JSON array of outputs. Each: {"address": "addr", "value": int}
                      Use "value": "wipe" on ONE output to auto-fill remaining balance.
        fee_rate: Fee rate in sat/vB (default: 1)
        network: Network (mainnet, testnet4, regtest)

    Returns:
        PSBT base64 string and QR code image for hardware wallet signing.
    """
    from mcp.types import TextContent, ImageContent

    try:
        inputs = json.loads(inputs_json)
        outputs = json.loads(outputs_json)
    except json.JSONDecodeError as e:
        return [TextContent(type="text", text=f"JSON parse error: {e}")]

    if not inputs:
        return [TextContent(type="text", text="Error: no inputs provided")]
    if not outputs:
        return [TextContent(type="text", text="Error: no outputs provided")]

    net = _get_network(network)
    total_input = sum(inp["value"] for inp in inputs)

    # Handle wipe output
    n_outputs = len(outputs)
    estimated_fee = _estimate_vsize(len(inputs), n_outputs) * fee_rate
    available = total_input - estimated_fee

    wipe_idx = None
    fixed_sum = 0
    for i, out in enumerate(outputs):
        if out.get("value") == "wipe" or out.get("value") == 0:
            if wipe_idx is not None:
                return [TextContent(type="text", text="Error: only one wipe output allowed")]
            wipe_idx = i
        else:
            fixed_sum += out["value"]

    if wipe_idx is not None:
        wipe_value = available - fixed_sum
        if wipe_value <= 0:
            return [TextContent(type="text", text=f"Error: not enough funds. Available: {available}, fixed outputs: {fixed_sum}")]
        outputs[wipe_idx]["value"] = wipe_value

    output_sum = sum(out["value"] for out in outputs)
    actual_fee = total_input - output_sum
    if actual_fee < 0:
        return [TextContent(type="text", text=f"Error: outputs ({output_sum}) exceed inputs ({total_input})")]

    # Build transaction
    tx = Transaction(version=2, vin=[], vout=[], locktime=0)

    for inp in inputs:
        tx.vin.append(TransactionInput(
            txid=bytes.fromhex(inp["txid"]),
            vout=inp["vout"],
            sequence=0xfffffffd,  # RBF enabled
        ))

    for out in outputs:
        out_script = Script.from_address(out["address"])
        tx.vout.append(TransactionOutput(value=out["value"], script_pubkey=out_script))

    # Create PSBT
    psbt = PSBT(tx)

    # Set witness_utxo for each input
    for i, inp in enumerate(inputs):
        inp_script = Script.from_address(inp["address"])
        psbt.inputs[i].witness_utxo = TransactionOutput(
            value=inp["value"],
            script_pubkey=inp_script,
        )
        # Add non-witness UTXO if provided
        if "non_witness_utxo_hex" in inp:
            raw_tx = Transaction.parse(bytes.fromhex(inp["non_witness_utxo_hex"]))
            psbt.inputs[i].non_witness_utxo = raw_tx

    # Sign WIF inputs inline
    wif_signed = []
    for i, inp in enumerate(inputs):
        if "wif" in inp:
            key = ec.PrivateKey.from_wif(inp["wif"])
            try:
                n = psbt.sign_with(key)
                if n > 0:
                    wif_signed.append(i)
            except Exception as e:
                pass  # Skip non-matching inputs

    # Serialize
    psbt_b64 = psbt.to_base64()
    psbt_bytes = psbt.serialize()

    # Build text summary
    lines = [
        f"PSBT created successfully ({len(psbt_bytes)} bytes)",
        f"",
        f"Inputs: {len(inputs)} ({total_input} sats = {total_input / 1e8:.8f} BTC)",
    ]
    for i, inp in enumerate(inputs):
        signed_mark = " [SIGNED]" if i in wif_signed else ""
        lines.append(f"  [{i}] {inp['txid'][:16]}...:{inp['vout']} = {inp['value']} sats{signed_mark}")

    lines.append(f"")
    lines.append(f"Outputs: {len(outputs)}")
    for i, out in enumerate(outputs):
        pct = out["value"] / total_input * 100 if total_input > 0 else 0
        wipe_mark = " [WIPE]" if i == wipe_idx else ""
        lines.append(f"  [{i}] {out['address'][:20]}... = {out['value']} sats ({pct:.1f}%){wipe_mark}")

    lines.append(f"")
    lines.append(f"Fee: {actual_fee} sats ({actual_fee / _estimate_vsize(len(inputs), len(outputs)):.1f} sat/vB)")

    all_signed = len(wif_signed) == len(inputs)
    if all_signed:
        # All inputs signed — finalize
        _finalize_psbt(psbt)
        final_tx = psbt.tx.serialize()
        tx_hex = final_tx.hex()
        lines.append(f"")
        lines.append(f"All inputs signed and finalized!")
        lines.append(f"Raw transaction hex ({len(final_tx)} bytes):")
        lines.append(tx_hex)
        return [TextContent(type="text", text="\n".join(lines))]

    if wif_signed:
        lines.append(f"")
        lines.append(f"Signed {len(wif_signed)}/{len(inputs)} inputs with WIF keys.")
        lines.append(f"Remaining inputs need hardware wallet signing.")

    lines.append(f"")
    lines.append(f"PSBT (base64):")
    lines.append(psbt_b64)

    # Generate QR code
    content = [TextContent(type="text", text="\n".join(lines))]

    try:
        # Try single QR code with base64 PSBT
        qr_data = psbt_b64
        qr_b64 = _generate_qr_base64(qr_data, size=400)
        content.append(TextContent(type="text", text="\nScan this QR code with your hardware wallet to sign:"))
        content.append(ImageContent(type="image", data=qr_b64, mimeType="image/png"))
    except Exception:
        # PSBT too large for single QR
        try:
            # Try UR encoding or BBQr
            import bbqr
            parts = bbqr.split_qrs(psbt_bytes, "P", max_version=20)
            content.append(TextContent(type="text", text=f"\nPSBT requires {len(parts)} QR codes (BBQr format). Scan each in sequence:"))
            for idx, part in enumerate(parts):
                qr_b64 = _generate_qr_base64(part, size=400)
                content.append(TextContent(type="text", text=f"\nPart {idx + 1}/{len(parts)}:"))
                content.append(ImageContent(type="image", data=qr_b64, mimeType="image/png"))
        except ImportError:
            content.append(TextContent(type="text", text="\nPSBT too large for a single QR code. Use the base64 text above, or install bbqr: pip install bbqr"))
        except Exception as e:
            content.append(TextContent(type="text", text=f"\nQR generation failed: {e}. Use the base64 text above."))

    return content


@mcp.tool()
def sign_psbt(psbt_base64: str, wif: str) -> str:
    """Sign a PSBT with a WIF private key.

    Args:
        psbt_base64: PSBT in base64 format
        wif: WIF-encoded private key

    Returns:
        Signed PSBT in base64 format.
    """
    try:
        psbt = PSBT.from_base64(psbt_base64)
    except Exception as e:
        return json.dumps({"error": f"Failed to parse PSBT: {e}"})

    try:
        key = ec.PrivateKey.from_wif(wif)
    except Exception as e:
        return json.dumps({"error": f"Failed to parse WIF: {e}"})

    n_signed = psbt.sign_with(key)

    return json.dumps({
        "signed_psbt": psbt.to_base64(),
        "inputs_signed": n_signed,
        "message": f"Signed {n_signed} input(s)" if n_signed > 0 else "Warning: no inputs matched this key",
    }, indent=2)


@mcp.tool()
def combine_psbts(psbts_json: str) -> str:
    """Combine multiple signed PSBTs into one.

    Used after different signers have each signed their copy of the PSBT.

    Args:
        psbts_json: JSON array of PSBT base64 strings to combine

    Returns:
        Combined PSBT in base64 format.
    """
    try:
        psbt_list = json.loads(psbts_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"JSON parse error: {e}"})

    if len(psbt_list) < 2:
        return json.dumps({"error": "Need at least 2 PSBTs to combine"})

    try:
        combined = PSBT.from_base64(psbt_list[0])
        for psbt_b64 in psbt_list[1:]:
            other = PSBT.from_base64(psbt_b64)
            # Merge partial signatures from other into combined
            for i, (ci, oi) in enumerate(zip(combined.inputs, other.inputs)):
                if oi.partial_sigs:
                    if ci.partial_sigs is None:
                        ci.partial_sigs = {}
                    ci.partial_sigs.update(oi.partial_sigs)
                if oi.final_scriptwitness:
                    ci.final_scriptwitness = oi.final_scriptwitness
                if oi.taproot_sigs:
                    if ci.taproot_sigs is None:
                        ci.taproot_sigs = {}
                    ci.taproot_sigs.update(oi.taproot_sigs)
    except Exception as e:
        return json.dumps({"error": f"Failed to combine PSBTs: {e}"})

    return json.dumps({
        "combined_psbt": combined.to_base64(),
        "message": f"Combined {len(psbt_list)} PSBTs successfully",
    }, indent=2)


@mcp.tool()
def finalize_psbt(psbt_base64: str) -> str:
    """Finalize a fully-signed PSBT and extract the raw transaction.

    Args:
        psbt_base64: Fully-signed PSBT in base64 format

    Returns:
        Raw transaction hex ready for broadcasting.
    """
    try:
        psbt = PSBT.from_base64(psbt_base64)
    except Exception as e:
        return json.dumps({"error": f"Failed to parse PSBT: {e}"})

    try:
        _finalize_psbt(psbt)
    except Exception as e:
        return json.dumps({"error": f"Failed to finalize PSBT: {e}"})

    tx_hex = psbt.tx.serialize().hex()
    return json.dumps({
        "tx_hex": tx_hex,
        "tx_size": len(psbt.tx.serialize()),
        "txid": psbt.tx.txid().hex(),
        "message": "PSBT finalized. Ready to broadcast.",
    }, indent=2)


@mcp.tool()
def broadcast_tx(tx_hex: str, network: str = "testnet4") -> str:
    """Broadcast a raw transaction to the Bitcoin network.

    Args:
        tx_hex: Raw transaction hex
        network: Network to broadcast on (mainnet, testnet4, regtest)

    Returns:
        Transaction ID if successful.
    """
    base_url = _mempool_url(network)
    url = f"{base_url}/tx"

    try:
        resp = _http_post(url, tx_hex.encode("utf-8"))
        txid = resp.decode("utf-8").strip()
        return json.dumps({
            "txid": txid,
            "network": network,
            "message": f"Transaction broadcast successfully!",
            "explorer": f"https://mempool.space{'/' + network if network != 'mainnet' else ''}/tx/{txid}"
            if network != "regtest" else f"Regtest tx: {txid}",
        }, indent=2)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        return json.dumps({"error": f"Broadcast failed (HTTP {e.code}): {error_body}"})
    except Exception as e:
        return json.dumps({"error": f"Broadcast failed: {e}"})


@mcp.tool()
def decode_psbt(psbt_base64: str) -> str:
    """Decode a PSBT and display its contents in human-readable format.

    Args:
        psbt_base64: PSBT in base64 format

    Returns:
        Decoded PSBT details including inputs, outputs, and signing status.
    """
    try:
        psbt = PSBT.from_base64(psbt_base64)
    except Exception as e:
        return json.dumps({"error": f"Failed to parse PSBT: {e}"})

    result = {
        "version": psbt.tx.version,
        "locktime": psbt.tx.locktime,
        "inputs": [],
        "outputs": [],
    }

    total_in = 0
    for i, (vin, pin) in enumerate(zip(psbt.tx.vin, psbt.inputs)):
        inp_info = {
            "index": i,
            "txid": vin.txid.hex(),
            "vout": vin.vout,
            "sequence": vin.sequence,
        }
        if pin.witness_utxo:
            inp_info["value"] = pin.witness_utxo.value
            inp_info["scriptPubKey"] = pin.witness_utxo.script_pubkey.serialize().hex()
            total_in += pin.witness_utxo.value

        # Signing status
        if pin.final_scriptwitness:
            inp_info["status"] = "finalized"
        elif pin.partial_sigs:
            inp_info["status"] = f"partially_signed ({len(pin.partial_sigs)} sig(s))"
        elif pin.taproot_sigs:
            inp_info["status"] = f"taproot_signed ({len(pin.taproot_sigs)} sig(s))"
        else:
            inp_info["status"] = "unsigned"

        if pin.bip32_derivations:
            inp_info["bip32_derivations"] = len(pin.bip32_derivations)

        result["inputs"].append(inp_info)

    total_out = 0
    for i, (vout, pout) in enumerate(zip(psbt.tx.vout, psbt.outputs)):
        out_info = {
            "index": i,
            "value": vout.value,
            "scriptPubKey": vout.script_pubkey.serialize().hex(),
        }
        # Try to get address
        try:
            for net_name, net in NETWORKS.items():
                try:
                    addr = vout.script_pubkey.address(net)
                    if addr:
                        out_info["address"] = addr
                        break
                except Exception:
                    continue
        except Exception:
            pass
        total_out += vout.value
        result["outputs"].append(out_info)

    result["total_input_sats"] = total_in
    result["total_output_sats"] = total_out
    result["fee_sats"] = total_in - total_out if total_in > 0 else "unknown"
    result["size_bytes"] = len(psbt.serialize())

    return json.dumps(result, indent=2)


@mcp.tool()
def show_qr(data: str, label: str = "QR Code") -> list:
    """Generate and display a QR code inline in the chat.

    Args:
        data: Text data to encode in the QR code
        label: Label to display above the QR code

    Returns:
        QR code image displayed inline.
    """
    from mcp.types import TextContent, ImageContent

    try:
        qr_b64 = _generate_qr_base64(data, size=400)
        return [
            TextContent(type="text", text=label),
            ImageContent(type="image", data=qr_b64, mimeType="image/png"),
        ]
    except Exception as e:
        return [TextContent(type="text", text=f"QR generation failed: {e}\n\nRaw data:\n{data}")]


@mcp.tool()
def fetch_and_create_sweep(
    addresses_json: str,
    destination: str,
    fee_rate: int = 1,
    network: str = "testnet4",
) -> list:
    """One-step sweep: fetch UTXOs from multiple addresses and create a PSBT sending everything to one destination.

    Shows a QR code of the unsigned PSBT for hardware wallet signing.

    Args:
        addresses_json: JSON array of addresses or WIFs to sweep.
                        Each entry: string (address) or {"address": "addr"} or {"wif": "wif_key"}
        destination: Destination address to send all funds to
        fee_rate: Fee rate in sat/vB (default: 1)
        network: Network (mainnet, testnet4, regtest)

    Returns:
        PSBT with QR code for signing, or finalized transaction if all inputs have WIFs.
    """
    from mcp.types import TextContent, ImageContent

    try:
        sources = json.loads(addresses_json)
    except json.JSONDecodeError as e:
        return [TextContent(type="text", text=f"JSON parse error: {e}")]

    base_url = _mempool_url(network)
    net = _get_network(network)

    all_utxos = []
    errors = []

    for source in sources:
        wif = None
        if isinstance(source, str):
            # Could be address or WIF
            try:
                key = ec.PrivateKey.from_wif(source)
                wif = source
                # Derive addresses
                info = _address_info(source, network)
                addrs_to_check = [info["p2wpkh"], info["p2tr"]]
            except Exception:
                addrs_to_check = [source]
        elif isinstance(source, dict):
            if "wif" in source:
                wif = source["wif"]
                info = _address_info(wif, network)
                addrs_to_check = [info["p2wpkh"], info["p2tr"]]
            else:
                addrs_to_check = [source.get("address", "")]
        else:
            errors.append(f"Invalid source: {source}")
            continue

        for addr in addrs_to_check:
            if not addr:
                continue
            try:
                raw = _http_get(f"{base_url}/address/{addr}/utxo")
                utxos = json.loads(raw)
                spk = Script.from_address(addr)
                for u in utxos:
                    entry = {
                        "txid": u["txid"],
                        "vout": u["vout"],
                        "value": u["value"],
                        "address": addr,
                    }
                    if wif:
                        entry["wif"] = wif
                    all_utxos.append(entry)
            except Exception as e:
                errors.append(f"Failed to fetch {addr}: {e}")

    if not all_utxos:
        msg = "No UTXOs found."
        if errors:
            msg += "\nErrors:\n" + "\n".join(errors)
        return [TextContent(type="text", text=msg)]

    total_input = sum(u["value"] for u in all_utxos)
    n_outputs = 1
    estimated_fee = _estimate_vsize(len(all_utxos), n_outputs) * fee_rate
    sweep_value = total_input - estimated_fee

    if sweep_value <= 0:
        return [TextContent(type="text", text=f"Error: total input ({total_input} sats) doesn't cover fee ({estimated_fee} sats)")]

    # Create the PSBT using the create_psbt tool logic
    outputs = [{"address": destination, "value": sweep_value}]

    return create_psbt(
        inputs_json=json.dumps(all_utxos),
        outputs_json=json.dumps(outputs),
        fee_rate=fee_rate,
        network=network,
    )


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    mcp.run()
