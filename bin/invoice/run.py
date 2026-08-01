#!/usr/bin/env python3
"""
plainkeep invoice <client> --amount <net> [--desc "<line item>"] — DRAFT an invoice (§4.1). Reads the
tax rule from templates/tax-formula.md, computes net/VAT/gross, and writes a draft into the client's
~/files/clients/<slug>/out/. It NEVER sends — you review and send by hand (§3). draft_only.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, vaultio  # noqa: E402

FORMULA = paths.PLAINKEEP_HOME / "templates" / "tax-formula.md"


def _next_inv_no(out: Path, day: str) -> str:
    n = 0
    if out.exists():
        for f in out.glob(f"invoice-{day}-*.md"):
            try:
                n = max(n, int(f.stem.rsplit("-", 1)[1]))
            except (ValueError, IndexError):
                pass
    return f"INV-{day}-{n + 1:02d}"


def main(argv):
    _, argv = output.parse_argv(argv)
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    if not argv:
        output.fail(output.EXIT_USAGE,
                    'usage: plainkeep invoice <client> --amount <net> [--desc "<text>"]', verb="invoice")
    slug = paths.slugify(argv[0])
    amount = desc = None
    i = 1
    while i < len(argv):
        if argv[i] == "--amount" and i + 1 < len(argv):
            amount = argv[i + 1]; i += 2; continue
        if argv[i] == "--desc" and i + 1 < len(argv):
            desc = argv[i + 1]; i += 2; continue
        i += 1
    hub = paths.WIKI / "clients" / f"{slug}.md"
    if not hub.exists():
        output.fail(output.EXIT_UNEXPECTED,
                    f"no client '{slug}' (wiki/clients/{slug}.md). Create it: plainkeep new client \"{argv[0]}\"",
                    verb="invoice")
    if amount is None:
        output.fail(output.EXIT_USAGE, "an --amount (net) is required", verb="invoice")
    try:
        net = float(amount)
    except ValueError:
        output.fail(output.EXIT_USAGE, f"--amount must be a number, got {amount!r}", verb="invoice")

    rate = float(paths.fm_field(FORMULA, "vat_rate") or 0) if FORMULA.exists() else 0.0
    cur = (paths.fm_field(FORMULA, "currency") or "").strip() if FORMULA.exists() else ""
    seller = (paths.fm_field(FORMULA, "seller_name") or "Your Name") if FORMULA.exists() else "Your Name"
    terms = int(paths.fm_field(FORMULA, "payment_terms_days") or 15) if FORMULA.exists() else 15
    vat = round(net * rate, 2)
    gross = round(net + vat, 2)

    client_name = paths.fm_field(hub, "title") or argv[0]
    day = date.today().strftime("%Y%m%d")
    out = paths.FILES_ROOT / "clients" / slug / "out"
    due = (date.today() + timedelta(days=terms)).isoformat()

    if dry:
        inv = _next_inv_no(out, day)
        draft = out / f"invoice-{day}-{inv.rsplit('-', 1)[1]}.md"
        data = {"dry_run": True, "client": client_name, "invoice_no": inv,
                "net": net, "vat": vat, "gross": gross, "currency": cur,
                "rate": rate, "would_write": str(draft)}

        def render_dry(_):
            print(f"would DRAFT invoice {inv} for {client_name}:")
            print(f"  net {net:.2f} + VAT {vat:.2f} ({rate*100:.0f}%) = gross {gross:.2f} {cur}")
            print(f"  -> {draft}  (dry run — nothing written)")
        return output.emit(data, "invoice", human=render_dry)

    vaultio.mkdir(out)
    inv = _next_inv_no(out, day)
    draft = out / f"invoice-{day}-{inv.rsplit('-', 1)[1]}.md"
    vaultio.write_text(draft,
        f"# DRAFT invoice {inv}\n\n"
        f"> **DRAFT — not sent.** Review, export, and send by hand. `plainkeep` never transmits (§3).\n\n"
        f"- From: {seller}\n- To: {client_name}\n- Date: {paths.today()}   Due: {due} ({terms} days)\n\n"
        f"| Item | Net |\n|---|---|\n| {desc or 'Services rendered'} | {net:.2f} {cur} |\n\n"
        f"- Net:   {net:.2f} {cur}\n- VAT ({rate*100:.0f}%): {vat:.2f} {cur}\n- **Gross: {gross:.2f} {cur}**\n",
        encoding="utf-8")
    paths.append_journal(f"invoice DRAFT {inv} for {slug} ({gross:.2f} {cur})")
    data = {"client": client_name, "invoice_no": inv, "net": net, "vat": vat,
            "gross": gross, "currency": cur, "rate": rate, "path": str(draft)}

    def render(_):
        print(f"DRAFT invoice {inv} for {client_name}:")
        print(f"  net {net:.2f} + VAT {vat:.2f} ({rate*100:.0f}%) = gross {gross:.2f} {cur}")
        print(f"  -> {draft}")
        print(f"  NOT sent — review and send by hand (the system never transmits).")

    return output.emit(data, "invoice", human=render)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
