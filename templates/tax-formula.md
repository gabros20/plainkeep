---
currency: HUF
vat_rate: 0.27
seller_name: Your Name / Company Kft.
seller_tax_id: 00000000-0-00
payment_terms_days: 15
---
# Tax formula

The single source of truth for how `plainkeep invoice` computes a draft.

> **The values above are an EXAMPLE, not a default to keep.** They are one jurisdiction's (Hungary:
> HUF, 27% ÁFA) and are here so the file parses on day one. Replace every one of them with yours
> before you invoice anybody. Nothing in the engine knows your currency or your rate — this file is
> the only place either is written down, which is why it lives in your vault and not in the tool.

`plainkeep invoice` reads the frontmatter above:

- `vat_rate` — applied as `gross = net * (1 + vat_rate)`. (0.27 = Hungarian ÁFA.)
- `currency` — printed on the draft.
- `seller_*` — your details on the draft header.
- `payment_terms_days` — due date = invoice date + this many days.

`plainkeep invoice` only ever produces a DRAFT in the client's `~/files/.../out/` folder. It never
sends anything — you review and send by hand (§3).
