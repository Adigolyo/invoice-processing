# Test fixtures

## `tig_pairs/` (committed)

32 synthetic **DEMO** TIG/invoice pairs (fictional companies and numbers, made for testing).
Each folder `<TIG number>__<invoice file stem>/` holds:

- `TIG-*.pdf`: the performance certificate (*TELJESÍTÉSIGAZOLÁS*), issued by
  MEGBÍZÓ KFT. to a contractor (A Kft. to K Kft.).
- the other PDF: the contractor's invoice (*SZÁMLA*).

`TIG_szamla_parok_tablazat.pdf` classifies the pairs:

|                 | jó számla (match) | hibás számla (mismatch) | total |
|-----------------|------------------:|------------------------:|------:|
| egysoros TIG    | 17 | 3 | 20 |
| többsoros TIG   |  8 | 4 | 12 |
| total           | 25 | 7 | 32 |

A single-line TIG has one item and no total row. A multi-line TIG has 2 or 3 items and an
`Összesen (nettó)` row. A mismatching invoice differs from its TIG in at least one item's
quantity or unit price.

### `tig_pairs/answer_key.json`

This file is transcribed by hand from the PDFs. Every amount appears twice: as printed
(`*_raw`, e.g. `"1 073,00 EUR"`, `"43 db"`) and as a normalised decimal string with no
thousands separator and a dot decimal, keeping the printed precision (`"1073.00"`, `"43"`).

```
{ "source": str, "notes": [str],          # notes: transcription vs. table conflicts (none)
  "pairs": [ {
    "pair": folder name,
    "expected_outcome": "match" | "mismatch",            # from the table PDF
    "tig_kind": "single_line" | "multi_line",            # from the table PDF
    "tig": { file, number, project, period, period_range_raw, contractor,
             contractor_tax_id, issued_raw, currency,
             lines: [line], total_net_raw, total_net },  # total null for single-line
    "invoice": { file, supplier, supplier_tax_id, supplier_country, invoice_number,
                 issue_date_raw, supply_date_raw, due_date_raw, performance_date_raw,
                 payment_method_raw, currency_raw, currency, vat_exempt_aam,
                 net_raw, net, vat_label_raw, vat_raw, vat,
                 gross_label_raw, gross_raw, gross,
                 lines: [line + vat_raw, vat, gross_raw, gross],
                 expected_ledger: { provider, inv_id_ext, currency, net, gross,
                                    due_date, performance_date, yymm } },
    "discrepancies": [ { line_index, field, invoice_value, tig_value,
                         invoice_raw, tig_raw } ] } ] }
line = { description, quantity_raw, quantity, unit, unit_price_raw, unit_price, net_raw, net }
```

Conventions:

- `performance_date_raw` is the *Teljesítés kelte* value. The invoices print no separate
  supply date, so `supply_date_raw` repeats *Teljesítés kelte*. `issue_date_raw` is
  *Kiállítás kelte*, and `due_date_raw` is *Fizetési határidő*.
- `supplier_country` (`HU`) is derived from the Hungarian address and tax-number format.
  The PDFs do not print a country.
- The invoices are not all VAT-exempt. Of the 32, 11 are AAM (*alanyi adómentes*): they
  print only *Fizetendő összesen*, so `net_raw`/`net` repeat that value and `vat_*` is null.
  The other 21 print 27% VAT with a net total, a VAT amount and a gross amount, both per
  line and as totals. Whichever applies, gross is *Fizetendő* as printed.
- Currencies: 15 pairs are in HUF (`Ft`) and 17 in EUR (`EUR`). The TIG and the invoice
  always use the same currency.
- `discrepancies` compares invoice line *i* with TIG line *i* on `quantity` and
  `unit_price`. For multi-line TIGs it also adds `{"line_index": null, "field":
  "total_net"}` when the invoice's net total differs from the TIG total. The list is empty
  exactly when `expected_outcome` is `match`.
- `expected_ledger`: dates are `YYYY.MM.DD`, `yymm` comes from the performance date, and
  amounts are the normalised net and gross (HUF amounts are whole numbers already).

`tests/unit/test_answer_key.py` checks that the key is internally consistent: the counts,
the line arithmetic, the totals, that discrepancies are empty exactly for matches, and
that every referenced file exists.

## `invoice_series/` (local only, never committed)

`tests/fixtures/invoice_series/` is listed in `.gitignore`. It holds real-looking foreign
supplier invoices (Services Unlimited Co, Ireland: HUF amounts in `HUF29,299.20`
notation, dates as `24/04/2026`). Those PDFs carry a buyer's personal details, so they stay
on the developer's machine. The PDFs themselves sit in the repo-root `számla sorozat/`
folder, which is also gitignored. Its local `answer_key.json` uses the same `invoice` shape
inside `{"invoices": [...]}` and records supplier-side data only. Tests that need it skip
when it is absent.
