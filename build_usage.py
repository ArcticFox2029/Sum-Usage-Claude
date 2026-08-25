"""Work-Mode/Sum-Usage-Claude/build_usage.py — turns Claude Code's own transcripts into a dashboard.

Nothing here collects data. Claude Code already writes one JSONL line per API request to
~/.claude/projects/<project>/<session>.jsonl, and every line carries the model, the timestamp,
and the full token breakdown. This script only aggregates what is already on disk, prices it,
and renders template.html into a self-contained index.html.

Four things in here are load-bearing, each learned from the data rather than assumed:

1. READ EVERY TRANSCRIPT, NOT JUST THE TOP LEVEL. Claude Code writes subagent runs to
   <project>/<session>/subagents/ and workflow runs to <project>/<session>/wf_*/. A
   "*/*.jsonl" glob sees none of them: 112 of 138 files, 3,203 requestIds that appear
   nowhere else. This under-counted silently for months — nothing errors, the dashboard
   just quietly reports a smaller number. Found 2026-08-19 only by diffing against ccusage,
   which reads recursively; before the fix the two tools disagreed by 3.4%, after it they
   agree to within the session still being written.

2. DEDUP BY requestId, KEEPING THE FULLEST RECORD. A streamed response is written to the
   transcript repeatedly as it grows and its usage block is cumulative, so the same
   requestId appears many times — 26,963 of 55,798 records here. Of the repeats that
   differ, the later line is the complete one (output 5 -> 159). Keeping the first, as this
   did until 2026-08-19, discards the finished half of every one; skipping dedup entirely
   would roughly double the totals instead. Neither failure announces itself.

3. CACHE TOKENS ARE PRICED DIFFERENTLY FROM INPUT. Cache reads bill at 0.1x input and cache
   writes at 1.25x (5-minute TTL) or 2x (1-hour). Cache reads are ~99% of all input tokens
   here, so treating them as ordinary input overstates the total by about ten times.

4. DAYS ARE LOCAL, NOT UTC. Transcript timestamps are UTC ('...Z'). In UTC+7 an evening
   session lands on the following UTC day, which would smear every late-night burst across
   two dates and make the daily view disagree with the owner's memory of when they worked.

Dated prices in prices.json are NOT a differentiator, contrary to what this file used to
imply: ccusage applies Claude Sonnet 5's introductory rate too (verified 2026-08-19 — both
tools return $197.73 for the same day, where the standard rate would give $289.64). The
dating still earns its place, because it keeps history correct when the promotion ends on
2026-08-31, but it is not a reason to prefer this over an existing tool.

Incremental by default: a byte offset per file is kept in data/.cursor.json, and JSONL is
append-only, so a re-run reads only what was added. Full parse of 464MB took 6.2s, so the
cursor is about staying fast as the archive grows (one session file is already 302MB), not
about the current runtime.

  python3 Work-Mode/Sum-Usage-Claude/build_usage.py            # incremental update
  python3 Work-Mode/Sum-Usage-Claude/build_usage.py --rebuild  # discard state, re-read everything
  python3 Work-Mode/Sum-Usage-Claude/build_usage.py --quiet    # no stdout except errors

Privacy: only counts and costs are written out. No prompt text, no reply text, no file paths
beyond the project directory name — which matters because this folder, unlike data/, is not
gitignored.
"""
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CURSOR_FILE = DATA / ".cursor.json"
SEEN_FILE = DATA / ".seen_requests.txt"
PRICES_FILE = HERE / "prices.json"
# Optional durable copy of the billing cycles. The page edits them in localStorage, which already
# survives this script rewriting index.html (localStorage is keyed by origin, not file contents), so
# the owner does not have to re-enter invoices after the 3-hour rebuild. This file is the belt to
# that braces: a cleared browser store, a different browser, or a machine move would otherwise lose
# figures that only exist in one browser profile. Seeded into the page and used only when
# localStorage has nothing — anything typed in the UI always wins.
CYCLES_FILE = HERE / "billing_cycles.json"
TEMPLATE_FILE = HERE / "template.html"
OUTPUT_HTML = HERE / "index.html"
OUTPUT_JSON = DATA / "usage_data.json"

# Recursive: Claude Code no longer keeps one flat file per session. Subagent runs live in
# <project>/<session>/subagents/agent-*.jsonl and workflow runs in <project>/<session>/wf_*/,
# and the old "*/*.jsonl" pattern saw none of them — measured 2026-08-19: 112 of 138 transcript
# files (41 MB) invisible, carrying 3,203 requestIds that appeared nowhere in the files it did
# read. Those are real API calls, so every figure was under-reported until this changed.
TRANSCRIPT_GLOB = "**/*.jsonl"
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

# 🐛 [fixed 2026-08-25] A second Claude account writes somewhere else entirely, and only the first
# root was ever read. This machine runs two: the default under ~/.claude, and a second signed in as
# a different address whose config lives under ~/.config/claude-account2. Work done in the second
# one was invisible to every figure on the dashboard.
#
# It has been nearly invisible rather than badly wrong so far, and only by luck: account 2's
# Lumin-App project is a SYMLINK back into ~/.claude/projects, so those sessions were already being
# read through the first root. What actually went missing was work done in the second account from
# any OTHER folder — one session, from the home directory, on 2026-08-19. The exposure grows the
# moment a new folder is opened there, and it fails the same silent way as the glob bug above: no
# error, just a smaller number.
#
# Resolve before reading, and skip a path already seen. Without that the symlinked project is walked
# twice — harmless for totals, since requestId dedup catches it, but it would double the file and
# byte counts in the run summary and give each copy its own cursor entry.
TRANSCRIPT_ROOTS = [
    TRANSCRIPT_ROOT,
    Path.home() / ".config" / "claude-account2" / "projects",
]

# Model names that appear in transcripts but never reach the API, so they are neither a cost
# nor a call worth counting. Without this they surface every run as "no price row for
# <synthetic>", training the reader to skip a warning that does have a real form: a genuinely
# new model that needs a prices.json entry.
NON_BILLABLE_MODELS = {"<synthetic>"}

# Project labels are decoded from a lossy directory name, so they are display strings only.
MAX_PROJECT_LABEL = 30

# The token in template.html that the serialised dataset replaces. Inlining rather than
# fetching keeps index.html openable by double-click: a file:// page cannot fetch() a
# sibling JSON file, so a dashboard that loaded its data over HTTP would silently show
# nothing unless the owner first started a web server.
DATA_PLACEHOLDER = "/*__USAGE_DATA__*/null"


def log(*args, quiet=False):
    if not quiet:
        print(*args)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


_SNAPSHOT_SUFFIX = re.compile(r"-\d{8}$")


def canonical_model(model):
    """Strips a dated snapshot suffix so 'claude-haiku-4-5-20251001' prices as 'claude-haiku-4-5'.

    Transcripts carry both forms for the same model. Only the undated id is in prices.json, so
    without this the dated one is reported as an unknown model and silently costs nothing —
    57 records on this machine. Deliberately does NOT guess at bare aliases like 'sonnet' or
    'opus': those name a family, not a version, and inventing a price for them would put a
    made-up number in the total rather than an honest line in the unpriced notice."""
    return _SNAPSHOT_SUFFIX.sub("", model)


def price_for(prices, model, day):
    """The price row in effect on `day` — the latest entry whose 'from' is not after it.

    Returns None for a model with no price row, which is how `<synthetic>` entries and any
    model released after this file was last updated get excluded from cost while still being
    counted as calls. Silently pricing an unknown model at zero would make a new model look
    free; the summary reports them instead."""
    rows = prices["models"].get(canonical_model(model))
    if not rows:
        return None
    chosen = None
    for row in sorted(rows, key=lambda r: r["from"]):
        if row["from"] <= day:
            chosen = row
    return chosen


def project_name(path):
    """Human-readable project from the transcript directory name.

    Claude Code encodes the working directory by replacing separators with dashes, e.g.
    '-Users-you-Documents-My-Project'. The encoding is lossy — a folder whose own name
    contains a dash ('My-Project') is indistinguishable from a separator — so there is no way
    to recover the real path, and this is a display label, never an identifier. Splitting on
    the first dash after the home prefix would turn 'My-Project' into 'My', so the whole
    remainder is kept and merely truncated when it runs long."""
    # First component under TRANSCRIPT_ROOT, not path.parent: with the recursive glob a
    # transcript's parent is often a session UUID or a "subagents" folder, which would label
    # every subagent run as a project of its own.
    raw = None
    for root in TRANSCRIPT_ROOTS:
        try:
            raw = path.relative_to(root).parts[0]
            break
        except ValueError:
            continue
    if raw is None:
        raw = path.parent.name
    if raw.startswith("-private-tmp") or raw.startswith("-tmp"):
        return "ชั่วคราว (tmp)"
    name = raw.strip("-")
    if "-Documents-" in name:
        label = name.split("-Documents-", 1)[1]
    elif name.endswith("-Documents"):
        label = "Documents"
    else:
        label = name
    if len(label) > MAX_PROJECT_LABEL:
        label = label[:MAX_PROJECT_LABEL - 1].rstrip("-") + "…"
    return label or "unknown"


def blank_bucket():
    return {"cost": 0.0, "calls": 0, "in": 0, "out": 0, "cache_read": 0, "cache_write": 0}


def add_to(bucket, cost, inp, out, cread, cwrite):
    bucket["cost"] += cost
    bucket["calls"] += 1
    bucket["in"] += inp
    bucket["out"] += out
    bucket["cache_read"] += cread
    bucket["cache_write"] += cwrite


def scan(prices, cursor, seen, quiet=False):
    """Read every transcript from its stored offset onward, returning per-day aggregates.

    A file that has shrunk since the last run (rotated, truncated, replaced) is re-read from
    zero — its stored offset now points into the middle of unrelated content, and continuing
    from it would parse garbage. Re-reading is safe precisely because of the requestId set:
    already-counted records are recognised and skipped."""
    days = defaultdict(lambda: {
        "bucket": blank_bucket(),
        "models": defaultdict(blank_bucket),
        "projects": defaultdict(blank_bucket),
        "hours": [0.0] * 24,
        "hour_calls": [0] * 24,
    })
    unpriced = defaultdict(int)
    new_ids = []
    stats = {"lines": 0, "records": 0, "dupes": 0, "files": 0, "bytes": 0,
             "already": 0, "stale": 0}
    mult = prices["multipliers"]

    # requestId -> the fullest record seen for it this run, and the records that carry no
    # requestId at all (nothing to collapse them against, so each counts once).
    pending, anonymous = {}, []

    def tokens_of(entry):
        return entry[3] + entry[4] + entry[5] + entry[6] + entry[7]

    def apply(entry):
        when, model, proj, inp, out, cread, w5m, w1h = entry
        day = when.strftime("%Y-%m-%d")
        row = price_for(prices, model, day)
        if row:
            cost = (
                inp * row["input"]
                + out * row["output"]
                + cread * row["input"] * mult["cache_read"]
                + w5m * row["input"] * mult["cache_write_5m"]
                + w1h * row["input"] * mult["cache_write_1h"]
            ) / 1e6
        else:
            cost = 0.0
            unpriced[model] += 1
        cwrite = w1h + w5m
        d = days[day]
        add_to(d["bucket"], cost, inp, out, cread, cwrite)
        add_to(d["models"][model], cost, inp, out, cread, cwrite)
        add_to(d["projects"][proj], cost, inp, out, cread, cwrite)
        d["hours"][when.hour] += cost
        d["hour_calls"][when.hour] += 1

    seen_files = set()
    all_paths = []
    for root in TRANSCRIPT_ROOTS:
        if not root.exists():
            continue
        for path in root.glob(TRANSCRIPT_GLOB):
            real = path.resolve()
            if real in seen_files:
                continue
            seen_files.add(real)
            all_paths.append(path)

    for path in sorted(all_paths):
        key = str(path)
        size = path.stat().st_size
        start = cursor.get(key, 0)
        if start > size:
            start = 0  # file was truncated or replaced — the old offset is meaningless
        if start == size:
            continue
        stats["files"] += 1
        stats["bytes"] += size - start
        proj = project_name(path)

        with open(path, "r", errors="replace") as fh:
            fh.seek(start)
            for line in fh:
                stats["lines"] += 1
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a partially-flushed final line; the next run re-reads it
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                stats["records"] += 1

                if (msg.get("model") or "") in NON_BILLABLE_MODELS:
                    continue

                ts = rec.get("timestamp")
                if not ts:
                    continue
                try:
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                except ValueError:
                    continue

                inp = usage.get("input_tokens") or 0
                out = usage.get("output_tokens") or 0
                cread = usage.get("cache_read_input_tokens") or 0
                creation = usage.get("cache_creation") or {}
                w1h = creation.get("ephemeral_1h_input_tokens") or 0
                w5m = creation.get("ephemeral_5m_input_tokens") or 0
                if not (w1h or w5m):
                    # Older records carry only the flat total with no TTL split. Price it at
                    # the 5-minute rate: that is the cheaper of the two, so an unknown TTL
                    # under-states rather than inflates.
                    w5m = usage.get("cache_creation_input_tokens") or 0

                # Canonical here, not at pricing time, so the dated and undated ids for one
                # model share a single row and a single display name instead of splitting the
                # dashboard into "Haiku 4.5" and an unlabelled "claude-haiku-4-5-20251001".
                entry = (when, canonical_model(msg.get("model") or "unknown"),
                         proj, inp, out, cread, w5m, w1h)
                req = rec.get("requestId")
                if not req:
                    anonymous.append(entry)
                    continue
                # Keep the FULLEST record for a requestId, not the first one. A streamed
                # response is written repeatedly as it grows, and the usage block is
                # cumulative — measured 2026-08-19: of 19,780 repeated requestIds, 17,694
                # were byte-identical (either choice is fine) but 2,086 differed, with the
                # later line carrying the larger count (output 5 -> 159, 4 -> 151).
                # First-wins therefore threw away the completed half of every one of them.
                prior = pending.get(req)
                if prior is not None:
                    stats["dupes"] += 1
                if prior is None or tokens_of(entry) > tokens_of(prior):
                    pending[req] = entry

        cursor[key] = size

    # Aggregation waits until every file has been read, so each requestId is collapsed to its
    # fullest record before it is priced.
    for req, entry in pending.items():
        if req in seen:
            stats["already"] += 1
            # A run that happened mid-stream counted a partial record and wrote it to the seen
            # file. The day totals it fed cannot be corrected in place, so report it and let
            # --rebuild fix it rather than silently carrying a number known to be low.
            if seen[req] >= 0 and tokens_of(entry) > seen[req]:
                stats["stale"] += 1
            continue
        seen[req] = tokens_of(entry)
        new_ids.append((req, tokens_of(entry)))
        apply(entry)
    for entry in anonymous:
        apply(entry)

    return days, unpriced, new_ids, stats


def merge_into_existing(days):
    """Fold this run's aggregates into the stored dataset.

    Incremental runs only ever see new records, so day totals accumulate across runs — which
    is why the previous output has to be read back rather than overwritten. Losing this step
    would silently reduce every historical day to whatever the latest run happened to read."""
    prior = load_json(OUTPUT_JSON, {})
    merged = {d["date"]: d for d in prior.get("days", [])}

    for day, agg in days.items():
        existing = merged.get(day)
        if existing is None:
            existing = {
                "date": day, **blank_bucket(),
                "models": {}, "projects": {},
                "hours": [0.0] * 24, "hour_calls": [0] * 24,
            }
            merged[day] = existing
        for field in ("cost", "calls", "in", "out", "cache_read", "cache_write"):
            existing[field] += agg["bucket"][field]
        for scope, source in (("models", agg["models"]), ("projects", agg["projects"])):
            for name, vals in source.items():
                target = existing[scope].setdefault(name, blank_bucket())
                for field in vals:
                    target[field] += vals[field]
        for hour in range(24):
            existing["hours"][hour] += agg["hours"][hour]
            existing["hour_calls"][hour] += agg["hour_calls"][hour]

    return [merged[k] for k in sorted(merged)]


def roll_up(days):
    """Month totals and whole-range totals, derived from the day list rather than counted
    separately — one source of truth means a month can never disagree with its own days."""
    months, totals = {}, blank_bucket()
    models, projects = defaultdict(blank_bucket), defaultdict(blank_bucket)

    for day in days:
        month = day["date"][:7]
        m = months.setdefault(month, {
            "month": month, **blank_bucket(), "days": 0, "models": {}, "projects": {},
        })
        m["days"] += 1
        for field in ("cost", "calls", "in", "out", "cache_read", "cache_write"):
            m[field] += day[field]
            totals[field] += day[field]
        for scope, store in (("models", models), ("projects", projects)):
            for name, vals in day[scope].items():
                mt = m[scope].setdefault(name, blank_bucket())
                for field in vals:
                    mt[field] += vals[field]
                    store[name][field] += vals[field]

    return [months[k] for k in sorted(months)], totals, dict(models), dict(projects)


def round_floats(obj, places=4):
    """Trim float noise before serialising. Costs carry many meaningless decimals after the
    per-token division, and they roughly double the size of the inlined payload."""
    if isinstance(obj, float):
        return round(obj, places)
    if isinstance(obj, dict):
        return {k: round_floats(v, places) for k, v in obj.items()}
    if isinstance(obj, list):
        return [round_floats(v, places) for v in obj]
    return obj


def render(dataset, quiet=False):
    try:
        template = TEMPLATE_FILE.read_text(encoding="utf-8")
    except OSError as error:
        print(f"cannot read {TEMPLATE_FILE.name}: {error}", file=sys.stderr)
        return False
    if DATA_PLACEHOLDER not in template:
        print(f"{TEMPLATE_FILE.name} has no {DATA_PLACEHOLDER} placeholder — cannot inline data",
              file=sys.stderr)
        return False
    payload = json.dumps(dataset, ensure_ascii=False, separators=(",", ":"))
    # '</script' inside a string literal would close the host <script> element early. It
    # cannot occur in this dataset (no free text is emitted), but escaping it costs nothing
    # and keeps that guarantee from depending on what future fields carry.
    payload = payload.replace("</", "<\\/")
    OUTPUT_HTML.write_text(template.replace(DATA_PLACEHOLDER, payload), encoding="utf-8")
    log(f"  wrote {OUTPUT_HTML.name}  ({len(payload)/1024:.0f} KB of data inlined)", quiet=quiet)
    return True


def main(argv):
    quiet = "--quiet" in argv
    rebuild = "--rebuild" in argv
    DATA.mkdir(parents=True, exist_ok=True)

    prices = load_json(PRICES_FILE, None)
    if not prices:
        print(f"cannot read {PRICES_FILE.name}", file=sys.stderr)
        return 1
    if not TRANSCRIPT_ROOT.is_dir():
        print(f"no transcripts at {TRANSCRIPT_ROOT}", file=sys.stderr)
        return 1

    if rebuild:
        for stale in (CURSOR_FILE, SEEN_FILE, OUTPUT_JSON):
            stale.unlink(missing_ok=True)
        log("rebuilding from scratch", quiet=quiet)

    cursor = load_json(CURSOR_FILE, {})
    # requestId -> the token total already counted for it. The total is what lets a later run
    # notice it once counted a partial record; lines written before this file gained that
    # second column load as -1, meaning "counted, size unknown".
    seen = {}
    if SEEN_FILE.is_file():
        for line in SEEN_FILE.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if not parts:
                continue
            seen[parts[0]] = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else -1

    started = datetime.now()
    days, unpriced, new_ids, stats = scan(prices, cursor, seen, quiet=quiet)

    if not days and not stats["records"]:
        log("no new records — dashboard already current", quiet=quiet)
        return 0

    all_days = merge_into_existing(days)
    months, totals, models, projects = roll_up(all_days)

    dataset = round_floats({
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "timezone": str(datetime.now().astimezone().tzinfo),
        "range": {"first": all_days[0]["date"], "last": all_days[-1]["date"]} if all_days else {},
        "totals": totals,
        "models": models,
        "projects": projects,
        "display_names": prices.get("display_names", {}),
        "days": all_days,
        "months": months,
        "unpriced": dict(unpriced),
        # Fallback only — see CYCLES_FILE. A malformed file must not take the dashboard down with it,
        # so anything unreadable degrades to "no seed" rather than raising.
        "seed_cycles": load_json(CYCLES_FILE, []),
    })

    OUTPUT_JSON.write_text(json.dumps(dataset, ensure_ascii=False, indent=1), encoding="utf-8")
    CURSOR_FILE.write_text(json.dumps(cursor, indent=1), encoding="utf-8")
    with open(SEEN_FILE, "a", encoding="utf-8") as fh:
        for req, total in new_ids:
            fh.write(f"{req} {total}\n")

    elapsed = (datetime.now() - started).total_seconds()
    log(f"scanned {stats['files']} file(s), {stats['bytes']/1e6:.0f} MB in {elapsed:.1f}s", quiet=quiet)
    log(f"  {stats['records']:,} usage records  ->  {len(new_ids):,} counted "
        f"({stats['dupes']:,} collapsed into the fullest record for their requestId, "
        f"{stats['already']:,} already counted in an earlier run)", quiet=quiet)
    if stats["stale"]:
        log(f"  NOTE: {stats['stale']:,} requestId(s) grew after an earlier run counted them "
            f"mid-stream. Their day totals are low — run with --rebuild to correct.", quiet=quiet)
    log(f"  {len(all_days)} day(s), {len(months)} month(s), "
        f"${totals['cost']:,.2f} API-equivalent total", quiet=quiet)
    if unpriced:
        log(f"  NOTE: no price row for {', '.join(sorted(unpriced))} — counted as calls, "
            f"excluded from cost. Add them to prices.json.", quiet=quiet)

    return 0 if render(dataset, quiet=quiet) else 1


if __name__ == "__main__":
    exit_code = main(sys.argv[1:])
    # Interactive runs pause 5s before exiting (owner's ask, 2026-08-10) so the summary lines are
    # readable before a Terminal window that auto-closes on exit disappears. --quiet marks the
    # automated path (status_bar_app's 3-hour rebuild loop), which must not sleep for no one.
    if "--quiet" not in sys.argv[1:]:
        time.sleep(5)
    sys.exit(exit_code)
