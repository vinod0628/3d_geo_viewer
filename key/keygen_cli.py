"""GeoViewer License Keygen - console (the VENDOR's tool).

  python keygen_cli.py genkeys                               # make a vendor keypair (once)
  python keygen_cli.py issue --name "Acme" --days 365 --machine 4KQ7X-...
  python keygen_cli.py issue --name "Site" --perpetual --edition ultimate
  python keygen_cli.py verify --file acme.key
  python keygen_cli.py info                                  # public key + its sha256

Keep this tool and vendor_private_key.pem secret; never ship either.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import license_format as lf  # noqa: E402

DEFAULT_KEY = HERE / "vendor_private_key.pem"


def cmd_genkeys(a) -> int:
    out = Path(a.out)
    if out.exists() and not a.force:
        sys.exit(f"{out} exists - use --force to overwrite (invalidates every issued key!)")
    info = lf.generate_keypair(out)
    print(f"private key written to {out}  - KEEP SECRET, back it up, never ship it\n")
    print("paste these into geoviewer/licensing.py (app) AND license_format.py (here):")
    print(f'    _PK_A = "{info["PK_A"]}"')
    print(f'    _PK_B = "{info["PK_B"]}"')
    print(f'    _PUBKEY_SHA256 = "{info["SHA256"]}"')
    return 0


def _edition(text: str) -> int:
    t = text.strip().lower()
    for code, nm in lf.EDITIONS.items():
        if t in (str(code), nm.lower(), nm.lower()[:3]):
            return code
    sys.exit("edition must be one of: " + ", ".join(lf.EDITIONS.values()))


def cmd_issue(a) -> int:
    pem = Path(a.private)
    if not pem.is_file():
        sys.exit(f"private key not found: {pem}\nrun 'genkeys' first.")
    edition = _edition(a.edition)
    machine = b"" if a.machine.strip().lower() in ("", "any", "-") else lf.machine_id_from_code(a.machine)
    expires = dt.date.fromisoformat(a.expiry) if a.expiry else lf._today() + dt.timedelta(days=int(a.days))
    years = lf.issue_max_years(edition, not machine)     # unbound -> at most 1 year
    cap = lf.max_expiry(years)
    if expires > cap:
        what = "an any-computer key" if not machine else lf.EDITIONS[edition]
        sys.exit(f"{what} expiry {expires} is more than {years} year(s) away (max {cap})")
    if expires <= lf._today():
        sys.exit(f"expiry {expires} must be in the future")
    lic = lf.License(name=a.name.strip() or "Unnamed", issued=lf._today(), expires=expires,
                     edition=edition, features=lf.edition_features(edition), machine=machine)
    key = lf.sign_payload(lf.load_private_key(pem), lic)
    chk = lf.decode(key)
    exp = "perpetual" if chk.expires is None else chk.expires.isoformat()
    print(f"# {chk.name} | {chk.edition_name} | {', '.join(chk.feature_names) or 'no features'}")
    print(f"# issued {chk.issued}  expires {exp}  machine: {'any PC' if not chk.machine else a.machine.upper()}")
    print(key)
    if a.out:
        Path(a.out).write_text(key + "\n", encoding="utf-8")
        print(f"\nwritten to {a.out}", file=sys.stderr)
    return 0


def cmd_verify(a) -> int:
    key = a.key or (Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read())
    try:
        lc = lf.decode(key)
    except lf.LicenseError as e:
        print(f"INVALID: {e}")
        return 1
    exp = "perpetual" if lc.expires is None else f"{lc.expires} ({lc.days_left()} days left)"
    print(f"valid\nname:     {lc.name}\nedition:  {lc.edition_name}")
    print(f"features: {', '.join(lc.feature_names) or 'none'}\nissued:   {lc.issued}\nexpires:  {exp}")
    print(f"machine:  {'any PC' if not lc.machine else lc.machine.hex()}")
    return 0


def cmd_info(a) -> int:
    import base64
    import hashlib

    print(f"public key (base64): {base64.b64encode(lf._PUBKEY).decode()}")
    print(f"public key sha256:   {hashlib.sha256(lf._PUBKEY).hexdigest()}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="keygen", description="GeoViewer license keygen (vendor)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("genkeys"); g.add_argument("--out", default=str(DEFAULT_KEY)); g.add_argument("--force", action="store_true"); g.set_defaults(func=cmd_genkeys)  # noqa: E702
    i = sub.add_parser("issue")
    i.add_argument("--name", required=True)
    i.add_argument("--private", default=str(DEFAULT_KEY))
    grp = i.add_mutually_exclusive_group()
    grp.add_argument("--days", type=int, default=365,
                     help="valid for N days (Basic up to 30 years, Professional/Ultimate up to 5)")
    grp.add_argument("--expiry", help="expiry date YYYY-MM-DD (Basic up to 30 years, Pro/Ultimate up to 5)")
    i.add_argument("--machine", default="any")
    i.add_argument("--edition", default="professional")
    i.add_argument("--out")
    i.set_defaults(func=cmd_issue)
    v = sub.add_parser("verify"); v.add_argument("--key"); v.add_argument("--file"); v.set_defaults(func=cmd_verify)  # noqa: E702
    sub.add_parser("info").set_defaults(func=cmd_info)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
