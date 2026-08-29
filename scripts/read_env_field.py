#!/usr/bin/env python3
"""Read a single field from a .env file without ever exposing the rest of it.

Exists because the unsafe path -- `cat`/`grep` a .env file to check one field, which
prints the whole line (or file) including embedded credentials -- has caused five
separate live MongoDB credential exposures in this project's session transcripts
(2026-07-20, 07-30, 08-21, 08-27, 08-29; see the project's `mongo_credential_incident`
memory). This script makes that path structurally unnecessary: it parses the .env file
properly and prints ONLY the one requested field's value (or a masked summary of it) --
nothing else, no other lines, no debug output.

Default output is MASKED. This is the advised path for any "does this value exist / did
it change" check -- it never puts the real secret in a transcript. Only pass --reveal
when the raw value is genuinely needed downstream (e.g. piped straight into another
command); never use --reveal just to eyeball or confirm a value.

Usage:
    read_env_field.py --file /path/to/.env --var DATABASE_URL              # masked (default, safe)
    read_env_field.py --file /path/to/.env --var DATABASE_URL --reveal     # full raw value

Masking rules:
    - Value not found in the file  -> "NOT SET"                (exit code 1)
    - Empty value                  -> "SET (length=0)"
    - URL-shaped value with
      embedded credentials         -> credentials replaced with ***, e.g.
                                       mongodb://***:***@host:27017/dbname?authSource=***
                                       (parsed with urllib.parse, not a hand-rolled regex --
                                       a regex-shape mismatch is exactly how the
                                       2026-08-27 incident leaked a real password). Query
                                       param values and any fragment are masked too, since
                                       either can carry a second secret independent of the
                                       userinfo credentials (e.g. a signed-URL token) --
                                       only param names survive, mapped to ***.
    - Any other value              -> "SET (length=N)"

Exit codes: 0 = field found, 1 = field not set, 2 = file could not be read.
"""
import argparse
import sys
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit


def parse_env_file(path):
    """Parse a .env file into a dict. Handles comments, blank lines, an optional
    leading `export `, and single/double-quoted values. Last occurrence of a
    duplicate key wins, matching standard dotenv behavior."""
    values = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            values[key] = val
    return values


def mask_value(value):
    if value == "":
        return "SET (length=0)"
    if "://" in value:
        try:
            parts = urlsplit(value)
            if parts.username or parts.password:
                host = parts.hostname or ""
                if host and ":" in host:  # IPv6 literal -- urlsplit strips the brackets
                    host = f"[{host}]"
                if parts.port:
                    host = f"{host}:{parts.port}"
                netloc = f"***:***@{host}" if host else "***:***"
                # A query string or fragment can carry its own secret (API key, signed-URL
                # token, ...) independent of the userinfo credentials -- mask each value,
                # keep only param names, and blank the fragment outright.
                query = "&".join(
                    f"{quote(k, safe='')}=***" for k, _ in parse_qsl(parts.query, keep_blank_values=True)
                )
                fragment = "***" if parts.fragment else ""
                return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))
        except ValueError:
            pass
    return f"SET (length={len(value)})"


def main():
    parser = argparse.ArgumentParser(
        description="Print exactly one field from a .env file -- masked by default, "
                     "nothing else on stdout. See module docstring for why this exists."
    )
    parser.add_argument("--file", required=True, help="Path to the .env file")
    parser.add_argument("--var", required=True, help="Name of the variable to read")
    parser.add_argument(
        "--reveal",
        action="store_true",
        help="Print the FULL raw value instead of a masked summary. Only use this "
             "when the value itself is genuinely needed downstream -- never to "
             "eyeball or verify a value; use masked (default) mode for that.",
    )
    args = parser.parse_args()

    try:
        values = parse_env_file(args.file)
    except (OSError, UnicodeDecodeError) as e:
        print(f"ERROR: cannot read {args.file}: {e}", file=sys.stderr)
        sys.exit(2)

    if args.var not in values:
        print("NOT SET")
        sys.exit(1)

    value = values[args.var]
    print(value if args.reveal else mask_value(value))
    sys.exit(0)


if __name__ == "__main__":
    main()
