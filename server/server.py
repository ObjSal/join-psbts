"""
Local development server for Bitcoin Address Sweeper.

Serves the HTML frontend and provides mempool.space-compatible API endpoints
backed by a local Bitcoin Core regtest node. When running without --regtest,
acts as a simple static file server (mainnet/testnet use mempool.space directly
from the browser).

Requires:
  - Bitcoin Core (bitcoind + bitcoin-cli) in PATH (only for --regtest)

Usage:
    python3 server/server.py [port] [--regtest]
"""

import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

# Resolve paths relative to this script
_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_DIR)

# Global regtest node — set when running with --regtest
_regtest_node = None


# ============================================================
# Managed regtest node
# ============================================================

class RegtestNode:
    """Manage a Bitcoin Core regtest node for local development/testing."""

    def __init__(self):
        self.datadir = tempfile.mkdtemp(prefix="psbt_regtest_")
        self.process = None
        self.rpc_port = 18443
        self.wallet_name = "psbt_faucet"

    def _cli(self, *args, wallet=None, timeout=30):
        """Run bitcoin-cli with managed node credentials."""
        cmd = [
            "bitcoin-cli",
            f"-datadir={self.datadir}",
            "-regtest",
            f"-rpcport={self.rpc_port}",
            "-rpcuser=test",
            "-rpcpassword=test",
        ]
        if wallet:
            cmd.append(f"-rpcwallet={wallet}")
        cmd.extend(args)

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise RuntimeError(
                f"bitcoin-cli {' '.join(args)} timed out after {timeout}s"
            )

        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            raise RuntimeError(
                f"bitcoin-cli {' '.join(args)} failed (rc={proc.returncode}): "
                f"{stderr_str}"
            )
        return stdout_str

    def _cli_json(self, *args, wallet=None, timeout=30):
        """Run bitcoin-cli and parse JSON output."""
        return json.loads(self._cli(*args, wallet=wallet, timeout=timeout))

    def start(self):
        """Start bitcoind in regtest mode with a funded wallet."""
        print(f"  Starting bitcoind (datadir: {self.datadir})...")

        # Detect version
        try:
            ver_out = subprocess.run(
                ["bitcoind", "--version"], capture_output=True, text=True,
                timeout=10,
            ).stdout
            print(f"  {ver_out.strip().splitlines()[0]}")
        except Exception:
            pass

        # Write bitcoin.conf
        conf_path = os.path.join(self.datadir, "bitcoin.conf")
        with open(conf_path, "w") as f:
            f.write("regtest=1\nserver=1\ntxindex=1\n")
            f.write("rpcuser=test\nrpcpassword=test\n")
            f.write("dnsseed=0\nlisten=0\nlistenonion=0\n")
            f.write("[regtest]\n")
            f.write(f"rpcport={self.rpc_port}\n")
            f.write("fallbackfee=0.00001\n")

        # macOS fix: concrete file descriptor limit
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft == resource.RLIM_INFINITY or soft < 1024:
            resource.setrlimit(resource.RLIMIT_NOFILE, (4096, hard))

        # Start bitcoind
        self.process = subprocess.Popen(
            ["bitcoind", f"-datadir={self.datadir}", "-regtest", "-daemon=0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Wait for ready
        for i in range(30):
            try:
                info = self._cli("getblockchaininfo", timeout=10)
                if "regtest" in info:
                    print("  bitcoind is ready.")
                    break
            except RuntimeError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("bitcoind failed to start within 30 seconds")

        # Create descriptor wallet
        try:
            self._cli("-named", "createwallet",
                      f"wallet_name={self.wallet_name}",
                      "descriptors=true")
            print("  Created descriptor wallet.")
        except RuntimeError as e:
            if "already exists" in str(e):
                try:
                    self._cli("loadwallet", self.wallet_name)
                    print("  Loaded existing wallet.")
                except RuntimeError as e2:
                    if "already loaded" in str(e2):
                        print("  Wallet already loaded.")
                    else:
                        raise
            else:
                raise

        # Mine initial blocks (101 for mature coinbase)
        mining_addr = self._cli("getnewaddress", wallet=self.wallet_name)
        self._cli("generatetoaddress", "101", mining_addr,
                  wallet=self.wallet_name)
        print("  Mined 101 blocks (coinbase mature).")

    def stop(self):
        """Stop bitcoind and clean up temp datadir."""
        if self.process:
            try:
                self._cli("stop", timeout=10)
                self.process.wait(timeout=15)
            except Exception:
                try:
                    self.process.kill()
                    self.process.wait(timeout=5)
                except Exception:
                    pass
        if os.path.exists(self.datadir):
            shutil.rmtree(self.datadir, ignore_errors=True)
        print("  bitcoind stopped and cleaned up.")

    def fund_address(self, address, amount_btc="1.0"):
        """Fund an address: create tx -> sign -> broadcast -> mine 1 block."""
        outputs_json = json.dumps([{address: float(amount_btc)}])
        raw_hex = self._cli("createrawtransaction", "[]", outputs_json,
                            wallet=self.wallet_name)
        funded_json = self._cli("fundrawtransaction", raw_hex,
                                wallet=self.wallet_name)
        funded = json.loads(funded_json)
        signed_json = self._cli("signrawtransactionwithwallet", funded["hex"],
                                wallet=self.wallet_name)
        signed = json.loads(signed_json)
        if not signed.get("complete"):
            raise RuntimeError(f"Signing incomplete: {signed}")
        txid = self._cli("sendrawtransaction", signed["hex"])
        self.mine(1)
        return txid

    def mine(self, blocks=1):
        """Mine blocks to confirm pending transactions."""
        mining_addr = self._cli("getnewaddress", wallet=self.wallet_name)
        self._cli("generatetoaddress", str(blocks), mining_addr,
                  wallet=self.wallet_name)


# ============================================================
# API helpers
# ============================================================

def _fetch_utxos_regtest(address):
    """Fetch UTXOs from local regtest node using scantxoutset."""
    node = _regtest_node
    if not node:
        raise RuntimeError("No regtest node available")
    result = node._cli_json("scantxoutset", "start",
                            json.dumps([f"addr({address})"]))
    utxos = []
    for u in result.get("unspents", []):
        utxos.append({
            "txid": u["txid"],
            "vout": u["vout"],
            "value": int(round(u["amount"] * 1e8)),
            "status": {"confirmed": True, "block_height": u.get("height", 0)},
        })
    return utxos


def _get_raw_tx_regtest(txid):
    """Get raw transaction hex from local regtest node."""
    node = _regtest_node
    if not node:
        raise RuntimeError("No regtest node available")
    return node._cli("getrawtransaction", txid)


def _broadcast_regtest(raw_hex):
    """Broadcast raw transaction to local regtest node + auto-mine."""
    node = _regtest_node
    if not node:
        raise RuntimeError("No regtest node available")
    txid = node._cli("sendrawtransaction", raw_hex)
    # Auto-mine so the tx is confirmed immediately
    try:
        node.mine(1)
    except Exception:
        pass  # non-fatal
    return txid


# ============================================================
# HTTP Handler
# ============================================================

class PsbtServerHandler(SimpleHTTPRequestHandler):
    """Serves static files from project root + API endpoints for regtest."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=_PROJECT_ROOT, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            self._handle_health()
        elif path == "/api/v1/fees/recommended":
            self._handle_fees()
        elif re.match(r"^/api/address/.+/utxo$", path):
            address = path.split("/api/address/")[1].rsplit("/utxo", 1)[0]
            self._handle_utxos(address)
        elif re.match(r"^/api/tx/[a-fA-F0-9]{64}/hex$", path):
            txid = path.split("/api/tx/")[1].split("/hex")[0]
            self._handle_raw_tx(txid)
        else:
            # Serve static files
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
        body_str = body_bytes.decode("utf-8", errors="replace").strip()

        if path == "/api/tx":
            self._handle_broadcast(body_str)
        elif path == "/api/faucet":
            self._handle_faucet(json.loads(body_str) if body_str else {})
        elif path == "/api/mine":
            self._handle_mine(json.loads(body_str) if body_str else {})
        else:
            self._send_json({"error": "Not found"}, 404)

    # -- API handlers -----------------------------------------------

    def _handle_health(self):
        resp = {
            "status": "ok",
            "regtest": _regtest_node is not None,
        }
        if _regtest_node:
            resp["rpc_port"] = _regtest_node.rpc_port
            resp["datadir"] = _regtest_node.datadir
        self._send_json(resp)

    def _handle_fees(self):
        self._send_json({
            "fastestFee": 1, "halfHourFee": 1, "hourFee": 1,
            "economyFee": 1, "minimumFee": 1,
        })

    def _handle_utxos(self, address):
        if not _regtest_node:
            self._send_json({"error": "Regtest not running"}, 503)
            return
        try:
            utxos = _fetch_utxos_regtest(address)
            self._send_json(utxos)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_raw_tx(self, txid):
        if not _regtest_node:
            self._send_text("Regtest not running", 503)
            return
        try:
            raw_hex = _get_raw_tx_regtest(txid)
            self._send_text(raw_hex)
        except Exception as e:
            self._send_text(str(e), 404)

    def _handle_broadcast(self, raw_hex):
        if not _regtest_node:
            self._send_text("Regtest not running", 503)
            return
        if not raw_hex:
            self._send_text("Empty transaction hex", 400)
            return
        try:
            txid = _broadcast_regtest(raw_hex)
            self._send_text(txid)
        except Exception as e:
            self._send_text(str(e), 400)

    def _handle_faucet(self, params):
        if not _regtest_node:
            self._send_json({"error": "Faucet requires --regtest mode"}, 400)
            return
        address = params.get("address")
        amount = params.get("amount", "1.0")
        if not address:
            self._send_json({"error": "Missing address"}, 400)
            return
        try:
            amount_str = str(float(amount))
            txid = _regtest_node.fund_address(address, amount_str)
            self._send_json({
                "success": True,
                "txid": txid,
                "address": address,
                "amount_btc": amount_str,
                "amount_sat": int(float(amount_str) * 1e8),
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_mine(self, params):
        if not _regtest_node:
            self._send_json({"error": "Mining requires --regtest mode"}, 400)
            return
        blocks = int(params.get("blocks", 1))
        if blocks < 1 or blocks > 100:
            self._send_json({"error": "blocks must be 1-100"}, 400)
            return
        try:
            _regtest_node.mine(blocks)
            self._send_json({"success": True, "blocks_mined": blocks})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # -- Response helpers -------------------------------------------

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        sys.stderr.write(f"[Server] {args[0]}\n")


class ReusableTCPServer(HTTPServer):
    allow_reuse_address = True
    allow_reuse_port = True

    def process_request(self, request, client_address):
        """Handle each request in a new thread to prevent single-threaded blocking."""
        import threading
        t = threading.Thread(target=self.process_request_thread,
                             args=(request, client_address), daemon=True)
        t.start()

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


# ============================================================
# Main
# ============================================================

def run_server(port=8000, regtest=False):
    """Start the HTTP server, optionally with a managed regtest node."""
    global _regtest_node

    if regtest:
        for binary in ["bitcoind", "bitcoin-cli"]:
            if shutil.which(binary) is None:
                print(f"ERROR: '{binary}' not found in PATH.")
                print("Install Bitcoin Core: brew install bitcoin (macOS)")
                sys.exit(1)

        print("=" * 60)
        print("Starting Bitcoin Core regtest node...")
        print("=" * 60)
        _regtest_node = RegtestNode()
        _regtest_node.start()
        print()

    server = ReusableTCPServer(("0.0.0.0", port), PsbtServerHandler)
    print(f"Address Sweeper Server running on http://localhost:{port}")
    if regtest:
        print(f"  Mode: REGTEST (test coins, no real value)")
    print("\nPress Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()
        if _regtest_node:
            _regtest_node.stop()
            _regtest_node = None
        print("Done.")


if __name__ == "__main__":
    port = 8000
    regtest = False
    for arg in sys.argv[1:]:
        if arg == "--regtest":
            regtest = True
        else:
            try:
                port = int(arg)
            except ValueError:
                print(f"Unknown argument: {arg}")
                print("Usage: python3 server/server.py [port] [--regtest]")
                sys.exit(1)

    run_server(port, regtest=regtest)
