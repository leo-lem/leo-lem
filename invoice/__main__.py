#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import tomllib


def money(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def default_config_candidates() -> list[Path]:
    home = Path.home()
    icloud = home / "Library/Mobile Documents/com~apple~CloudDocs/Invoices/config.toml"
    return [
        Path.cwd() / "config.toml",
        home / ".config/invoice/config.toml",
        icloud,
    ]


def resolve_config(cli_config: str | None) -> Path:
    env = os.environ.get("INVOICE_CONFIG")
    if cli_config:
        p = Path(cli_config).expanduser()
        if p.exists():
            return p
        raise FileNotFoundError(f"Config not found: {p}")
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
        raise FileNotFoundError(f"Config not found: {p}")
    for c in default_config_candidates():
        if c.exists():
            return c
    raise FileNotFoundError("No config.toml found. Provide --config or set INVOICE_CONFIG.")


def parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def epc_qr_payload(
    *,
    name: str,
    iban: str,
    amount: Decimal,
    remittance: str,
    bic: str | None = None,
) -> str:
    # EPC069-12 "SCT" payment QR payload.
    lines = [
        "BCD",
        "002",  # version
        "1",    # charset (1 = UTF-8)
        "SCT",
        (bic or ""),
        name,
        iban.replace(" ", ""),
        f"EUR{money(amount)}",
        "",          # purpose
        remittance,  # remittance info
        "",          # info
    ]
    return "\n".join(lines)


def try_make_qr_svg(payload: str) -> str | None:
    # Hard default: QR on. If segno isn't installed, we just omit the image.
    try:
        import segno  # type: ignore
    except Exception:
        return None
    qr = segno.make(payload, error="M")
    return qr.svg_inline(scale=3, omitsize=True)


def render_html(template: str, data: dict[str, str]) -> str:
    out = template
    for k, v in data.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="invoice",
        description="Generate invoice HTML from TOML files (QR default).",
    )
    ap.add_argument("invoice_toml", help="Path to invoice TOML (e.g. data/2026-0001.toml)")
    ap.add_argument("--config", help="Path to config.toml (otherwise auto-detected)")
    ap.add_argument("--out", help="Output directory (default: alongside invoice file in .out)")
    ap.add_argument("--open", action="store_true", help="Open generated HTML in browser (macOS open)")
    args = ap.parse_args()

    invoice_path = Path(args.invoice_toml).expanduser().resolve()
    if not invoice_path.exists():
        print(f"Invoice file not found: {invoice_path}", file=sys.stderr)
        return 2

    invoice_no = invoice_path.stem  # <-- derived from filename

    config_path = resolve_config(args.config)
    cfg = load_toml(config_path)
    inv = load_toml(invoice_path)

    repo_dir = Path(__file__).resolve().parent
    template_path = repo_dir / "template.html"
    template = template_path.read_text(encoding="utf-8")

    seller = cfg["seller"]
    payment = cfg.get("payment", {})

    terms_days = int(payment.get("terms_days", 14))
    currency = str(payment.get("currency", "EUR"))

    # New schema: invoice date is stored under "date"
    if "date" not in inv:
        print("Invoice TOML missing required field: date = \"YYYY-MM-DD\"", file=sys.stderr)
        return 2

    invoice_date = parse_date(str(inv["date"]))
    due_date = invoice_date + dt.timedelta(days=terms_days)

    client = inv["client"]
    items = inv.get("items", [])
    if not items:
        print("Invoice has no items.", file=sys.stderr)
        return 2

    total = Decimal("0.00")
    rows: list[str] = []
    for it in items:
        desc = esc(str(it["description"]))
        qty = Decimal(str(it.get("quantity", 1)))
        unit = esc(str(it.get("unit", "")))
        unit_price = Decimal(str(it["unit_price"]))
        line_total = (qty * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += line_total
        rows.append(
            "<tr>"
            f"<td>{desc}</td>"
            f"<td class='num'>{esc(str(qty))}</td>"
            f"<td>{unit}</td>"
            f"<td class='num'>{money(unit_price)}</td>"
            f"<td class='num'>{money(line_total)}</td>"
            "</tr>"
        )
    items_html = "\n".join(rows)

    service_period = esc(str(inv.get("service_period", "")))

    # QR always on: remittance from invoice_no, amount from total
    iban = str(payment.get("iban", "")).strip()
    if not iban:
        print("Config missing required field: [payment].iban", file=sys.stderr)
        return 2

    bic = str(payment.get("bic", "")).strip() or None
    payload = epc_qr_payload(
        name=str(seller["name"]),
        iban=iban,
        amount=total,
        remittance=f"Invoice {invoice_no}",
        bic=bic,
    )
    qr_svg = try_make_qr_svg(payload) or ""

    out_dir = Path(args.out).expanduser().resolve() if args.out else (invoice_path.parent / ".out")
    out_dir.mkdir(parents=True, exist_ok=True)
    html_out = out_dir / f"{invoice_no}.html"

    data = {
        "INVOICE_NO": esc(invoice_no),
        "INVOICE_DATE": esc(invoice_date.isoformat()),
        "DUE_DATE": esc(due_date.isoformat()),
        "SERVICE_PERIOD": service_period,

        "SELLER_NAME": esc(str(seller["name"])),
        "SELLER_ADDRESS1": esc(str(seller["address1"])),
        "SELLER_ADDRESS2": esc(str(seller["address2"])),
        "SELLER_EMAIL": esc(str(seller.get("email", ""))),
        "SELLER_WEBSITE": esc(str(seller.get("website", ""))),
        "SELLER_TAXNO": esc(str(seller.get("tax_number", "PENDING"))),

        "CLIENT_NAME": esc(str(client["name"])),
        "CLIENT_ADDRESS1": esc(str(client["address1"])),
        "CLIENT_ADDRESS2": esc(str(client["address2"])),

        "ITEM_ROWS": items_html,
        "TOTAL": esc(money(total)),
        "CURRENCY": esc(currency),

        "IBAN": esc(iban),
        "BIC": esc(str(payment.get("bic", ""))),
        "BANK": esc(str(payment.get("bank", ""))),

        "QR_SVG": qr_svg,
        "KLEINUNTERNEHMER_NOTE": "Gemäß §19 UStG wird keine Umsatzsteuer berechnet.",
    }

    html = render_html(template, data)
    html_out.write_text(html, encoding="utf-8")

    print(f"Config:  {config_path}")
    print(f"Invoice: {invoice_path}")
    print(f"Invoice#: {invoice_no}")
    print(f"Output:  {html_out}")
    if not qr_svg:
        print("Note: QR not embedded (install `segno` to include QR SVG).")

    if args.open and sys.platform == "darwin":
        os.system(f"open {str(html_out)!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())