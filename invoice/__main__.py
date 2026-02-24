#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import tomllib


def money_qr(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"

def money_de(x: Decimal) -> str:
    return money_qr(x).replace(".", ",")

def money_en(x: Decimal) -> str:
    return money_qr(x)

def date_de(d: dt.date) -> str:
    return d.strftime("%d.%m.%Y")

def date_en(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")


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


def resolve_config(cli_config: str | None) -> Path:
    if cli_config:
        p = Path(cli_config).expanduser()
        if p.exists():
            return p
        raise FileNotFoundError(f"Config not found: {p}")

    env = os.environ.get("INVOICE_CONFIG")
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
        raise FileNotFoundError(f"Config not found: {p}")

    candidates = [
        Path.cwd() / "config.toml",
        Path.home() / ".config/invoice/config.toml",
    ]
    for p in candidates:
        if p.exists():
            return p

    raise FileNotFoundError("No config.toml found. Use --config or set INVOICE_CONFIG.")


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
    return "\n".join(
        [
            "BCD",
            "002",
            "1",
            "SCT",
            (bic or ""),
            name,
            iban.replace(" ", ""),
            f"EUR{money_qr(amount)}",
            "",
            remittance,
            "",
        ]
    )


def try_make_qr_svg(payload: str) -> str:
    try:
        import segno  # type: ignore
    except Exception:
        return ""
    return segno.make(payload, error="M").svg_inline(scale=3, omitsize=True)


def render_html(template: str, data: dict[str, str]) -> str:
    out = template
    for k, v in data.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="invoice")
    ap.add_argument("invoice_toml", help="Path to invoice TOML (e.g. data/2026-0001.toml)")
    ap.add_argument("--config", help="Path to config.toml")
    ap.add_argument("--out", help="Output directory (default: alongside invoice file in .out)")
    args = ap.parse_args()

    invoice_path = Path(args.invoice_toml).expanduser().resolve()
    if not invoice_path.exists():
        print(f"Invoice file not found: {invoice_path}", file=sys.stderr)
        return 2

    invoice_no = invoice_path.stem
    config_path = resolve_config(args.config)

    cfg = load_toml(config_path)
    inv = load_toml(invoice_path)

    lang = str(inv.get("lang", "de")).lower()
    if lang not in {"de", "en"}:
        lang = "de"

    repo_dir = Path(__file__).resolve().parent
    template_name = f"template.{lang}.html"
    template = (repo_dir / template_name).read_text(encoding="utf-8")

    money_fmt = money_de if lang == "de" else money_en
    date_fmt = date_de if lang == "de" else date_en

    labels = {
        "de": {
            "service_period": "Leistungszeitraum",
            "klein": "Gemäß §19 UStG wird keine Umsatzsteuer berechnet.",
            "unit_hours": "Std.",
        },
        "en": {
            "service_period": "Service period",
            "klein": "No VAT charged under §19 UStG (small business regulation).",
            "unit_hours": "hours",
        },
    }[lang]

    seller = cfg["seller"]
    payment = cfg["payment"]

    if "date" not in inv:
        print('Invoice TOML missing required field: date = "YYYY-MM-DD"', file=sys.stderr)
        return 2

    invoice_date = parse_date(str(inv["date"]))
    terms_days = int(payment.get("terms_days", 14))
    due_date = invoice_date + dt.timedelta(days=terms_days)
    currency = str(payment.get("currency", "EUR"))

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
        unit_raw = str(it.get("unit", "")).strip()
        if unit_raw.lower() in {"h", "hour", "hours", "std", "std.", "stunde", "stunden"}:
            unit = esc(labels["unit_hours"])
        else:
            unit = esc(unit_raw)
        unit_price = Decimal(str(it["unit_price"]))
        line_total = (qty * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += line_total
        rows.append(
            "<tr>"
            f"<td>{desc}</td>"
            f"<td class='num'>{esc(str(qty))}</td>"
            f"<td>{unit}</td>"
            f"<td class='num'>{money_fmt(unit_price)}</td>"
            f"<td class='num'>{money_fmt(line_total)}</td>"
            "</tr>"
        )

    iban = str(payment.get("iban", "")).strip()
    if not iban:
        print("Config missing required field: [payment].iban", file=sys.stderr)
        return 2

    bic = str(payment.get("bic", "")).strip() or None
    payload = epc_qr_payload(
        name=str(seller["name"]),
        iban=iban,
        amount=total,
        remittance=invoice_no,
        bic=bic,
    )
    qr_svg = try_make_qr_svg(payload)

    out_dir = Path(args.out).expanduser().resolve() if args.out else (invoice_path.parent / ".out")
    out_dir.mkdir(parents=True, exist_ok=True)
    html_out = out_dir / f"{invoice_no}.html"

    sp = str(inv.get("service_period", "")).strip()
    service_period_line = (
        f"<div><span class='muted'>{esc(labels['service_period'])}:</span> {esc(sp)}</div>"
        if sp
        else ""
    )

    data = {
        "INVOICE_NO": esc(invoice_no),
        "INVOICE_DATE": esc(date_fmt(invoice_date)),
"DUE_DATE": esc(date_fmt(due_date)),
        "SERVICE_PERIOD_LINE": service_period_line,

        "SELLER_NAME": esc(str(seller["name"])),
        "SELLER_ADDRESS1": esc(str(seller["address1"])),
        "SELLER_ADDRESS2": esc(str(seller["address2"])),
        "SELLER_EMAIL": esc(str(seller.get("email", ""))),
        "SELLER_WEBSITE": esc(str(seller.get("website", ""))),
        "SELLER_TAXNO": esc(str(seller.get("tax_number", "PENDING"))),

        "CLIENT_NAME": esc(str(client["name"])),
        "CLIENT_ADDRESS1": esc(str(client["address1"])),
        "CLIENT_ADDRESS2": esc(str(client["address2"])),

        "ITEM_ROWS": "\n".join(rows),
        "TOTAL": esc(money_fmt(total)),
        "VAT": esc(money_fmt(Decimal("0.00"))),
        "CURRENCY": esc(currency),

        "IBAN": esc(iban),
        "BIC": esc(str(payment.get("bic", ""))),
        "BANK": esc(str(payment.get("bank", ""))),

        "QR_SVG": qr_svg,
        "KLEINUNTERNEHMER_NOTE": esc(labels["klein"]),
    }

    html_out.write_text(render_html(template, data), encoding="utf-8")
    print(html_out)

    if sys.platform == "darwin":
        os.system(f"open {str(html_out)!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())