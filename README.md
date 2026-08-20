# Sum-Usage-Claude

**[ภาษาไทย](README.th.md)** · English

A dashboard for how much Claude Code you actually use — built from the transcripts Claude Code
already writes, so there is nothing to install and nothing new to collect.

```bash
python3 build_usage.py     # then open index.html
```

Double-click `index.html`. The data is inlined into the page, so it works offline and needs no
web server — a `file://` page cannot fetch a sibling JSON file, which is why the build step
renders `template.html` into a self-contained `index.html` rather than loading data at runtime.

The three tabs are `#overview`, `#daily`, and `#day`, so a view is linkable and survives a
reload. To check a change without clicking through, Chrome can shoot it headless:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu \
  --screenshot=shot.png --window-size=1300,2600 \
  "file://$PWD/index.html#overview"
```

## What it answers

- **คุ้มไหม** — what the same usage would have cost at API list prices, divided by the monthly
  plan price. That ratio is the headline number.
- **โทเคนต่อบาท** — total tokens per baht actually paid.
- **วันไหนหนัก** — value per day, and per hour once you drill into a day.
- **ใช้โมเดลอะไร** — the model mix, plus which project the work belonged to.

## The three things that make the numbers right

1. **Dedup by `requestId`.** 48% of usage records in the first real run were duplicates of a
   requestId already counted. Without this step every figure is roughly doubled.
2. **Cache tokens priced separately.** Cache reads bill at 0.1× input and are ~99% of all input
   tokens here — pricing them as ordinary input overstates the total about tenfold.
3. **Local days, not UTC.** Transcripts are stamped in UTC; in UTC+7 an evening session lands on
   the next UTC date, which would split every late-night burst across two days.

## The cost figures are a valuation, not a bill

On a subscription you pay the plan price, full stop. Every dollar figure in the dashboard is
*what this usage would have cost on the API* — a measure of value received, not money owed. The
plan price and the exchange rate are yours to set in the panel on the overview tab (Claude Code
does not store which plan you are on), and they persist in the browser, so changing either is
instant and never needs a rebuild.

## Thai or English

The dashboard ships in both. It picks from your browser's language on the first visit and remembers
what you choose after that, in the browser rather than in the file — so the choice survives a
rebuild. The button is in the top right, beside the light/dark switch.

Both tables live in `template.html` under `const I18N`. A key with no English entry falls back to
the Thai string rather than printing the key name: a missing translation should look unfinished, not
broken.

## Files

| File | Role |
|---|---|
| `build_usage.py` | Reads transcripts, prices them, renders `index.html`. Incremental by default (`--rebuild` to start over). |
| `prices.json` | API list prices, **dated** — Sonnet 5's introductory rate ends 2026-08-31, and a flat number would silently reprice history. |
| `template.html` | The dashboard. `build_usage.py` substitutes the dataset into it. |
| `billing_cycles.example.json` | The shape a cycle takes. Copy it to `billing_cycles.json` and put your own invoices in — that file is gitignored, because your invoices are nobody else's business. The page keeps them in the browser anyway; the file is only the durable copy for a fresh browser or a new machine. |
| `data/`, `index.html` | Generated — gitignored. |

Only counts and costs are written out: no prompt text, no replies, no file paths beyond the
project directory name. This folder is not gitignored the way `data/` is, so that matters.

## License

MIT — see [LICENSE](LICENSE)
