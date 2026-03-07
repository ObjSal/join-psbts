#!/usr/bin/env python3
"""Sign a PSBT file with a WIF private key.

Usage: python3 sign-psbt.py <psbt-file> <wif>

Outputs: <psbt-file>-signed.psbt in the same directory.
Requires: pip install embit
"""

import argparse
import os
import sys

try:
    from embit import ec
    from embit.psbt import PSBT
except ImportError:
    print("Error: embit library required. Install with: pip install embit", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Sign a PSBT file with a WIF private key")
    parser.add_argument("psbt_file", help="Path to the unsigned PSBT file")
    parser.add_argument("wif", help="WIF-encoded private key")
    args = parser.parse_args()

    if not os.path.isfile(args.psbt_file):
        print(f"Error: file not found: {args.psbt_file}", file=sys.stderr)
        sys.exit(1)

    with open(args.psbt_file, "rb") as f:
        raw = f.read()

    try:
        psbt = PSBT.parse(raw)
    except Exception as e:
        print(f"Error parsing PSBT: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        key = ec.PrivateKey.from_wif(args.wif)
    except Exception as e:
        print(f"Error parsing WIF: {e}", file=sys.stderr)
        sys.exit(1)

    sigs_added = psbt.sign_with(key)

    if sigs_added == 0:
        print("Warning: no inputs matched this key (0 signatures added)", file=sys.stderr)
    else:
        print(f"Signed {sigs_added} input(s)")

    base, ext = os.path.splitext(args.psbt_file)
    out_path = f"{base}-signed{ext}"

    with open(out_path, "wb") as f:
        f.write(psbt.serialize())

    print(f"Saved to {out_path}")


if __name__ == "__main__":
    main()
