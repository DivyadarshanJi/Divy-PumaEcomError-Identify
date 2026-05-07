import os
import pyodbc
from datetime import date, datetime, timedelta, timezone

# India Standard Time = UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
SPIKE_RATIO       = 1.5
SPIKE_MIN_ORDERS  = 100
DOWN_RATIO        = 0.5
DISCOUNT_MIN_PCT  = 0.70
DISCOUNT_MIN_ORD  = 5
MIN_WEEKS         = 3

# ─────────────────────────────────────────────
# CHANNEL → PLATFORM MAP
# ─────────────────────────────────────────────
CHANNEL_MAP = {
    "MYNTRAPPMP":    "Myntra",
    "AMAZON_SC":     "Amazon",
    "AMAZON_SC_ES":  "Amazon",
    "FLIPKARTV3":    "Flipkart",
    "NYKAA_FASHION": "Nykaa",
    "NYKAA_NEW":     "Nykaa",
    "AJIO":          "Ajio",
    "AJIO_VMS":      "Ajio",
    "TATACLIQ":      "TataCliq",
    "TATACLIQ_L":    "TataCliqLux",
    "SFCC":          "Puma.com",
    "PUMA_APP":      "Puma.com",
    "RCB":           "RCB",
    "MAGICPIN":      "Magicpin",
    "CRED":          "Cred",
    "GOFYND-PUMA":   "GoFynd",
    "FIRSTCRY_BLR":  "Firstcry",
}

ALL_CHANNELS   = list(CHANNEL_MAP.keys())
CHANNEL_FILTER = "p.sales_channel IN ({})".format(
    ",".join(f"'{c}'" for c in ALL_CHANNELS)
)

# ─────────────────────────────────────────────
# DB CONNECTION
# ─────────────────────────────────────────────
def get_connection():
    server   = os.environ.get("DB_HOST",     "rpro.pumaindia.in")
    database = os.environ.get("DB_NAME",     "PUMA_ECOM")
    username = os.environ.get("DB_USER",     "Vinod.P")
    password = os.environ.get("DB_PASSWORD", "")
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        "TrustServerCertificate=yes;"
        "Encrypt=yes;"
    )
    return pyodbc.connect(conn_str, timeout=30)

# ─────────────────────────────────────────────
# SLOT HELPERS
# ─────────────────────────────────────────────
def get_check_slots():
    """
    Run at X:
      Primary slot = X-1hr   to X-30min
      Safety slot  = X-1.5hr to X-1hr
    Example: run at 10:30 AM
      Primary = 9:30 AM - 10:00 AM
      Safety  = 9:00 AM - 9:30 AM
    """
    now  = datetime.now(IST).replace(second=0, microsecond=0, tzinfo=None)
    mins = 0 if now.minute < 30 else 30
    run_time = now.replace(minute=mins)

    primary_end   = run_time - timedelta(minutes=30)
    primary_start = run_time - timedelta(minutes=60)
    safety_end    = run_time - timedelta(minutes=60)
    safety_start  = run_time - timedelta(minutes=90)

    return [
        (primary_start, primary_end),
        (safety_start,  safety_end),
    ]

def slot_label(start, end):
    def fmt(dt):
        h  = dt.hour
        m  = dt.minute
        suffix = "AM" if h < 12 else "PM"
        h12 = h if h <= 12 else h - 12
        if h12 == 0: h12 = 12
        return f"{h12}:{m:02d}{suffix}"
    return f"{fmt(start)}-{fmt(end)}"

def get_baseline_dates(slot_start):
    """Return last 8 dates with same weekday as today."""
    today   = datetime.now(IST).date()
    weekday = today.weekday()
    dates   = []
    weeks   = 0
    while len(dates) < 8:
        weeks += 1
        candidate = today - timedelta(weeks=weeks)
        if candidate.weekday() == weekday:
            dates.append(candidate.strftime("%Y-%m-%d"))
    return dates

# ─────────────────────────────────────────────
# SMART BASELINE
# drop highest + lowest, average remaining 6
# ─────────────────────────────────────────────
def smart_baseline(vals):
    non_zero = sum(1 for v in vals if v > 0)
    if non_zero < MIN_WEEKS:
        return 0.0, non_zero
    if len(vals) < 3:
        return sum(vals) / len(vals), non_zero
    trimmed = sorted(vals)[1:-1]
    avg = sum(trimmed) / len(trimmed) if trimmed else 0.0
    return avg, non_zero

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run():
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    slots     = get_check_slots()

    print(f"Surge Watchdog running at {datetime.now(IST).strftime('%d-%b-%Y %H:%M')} IST")
    for s, e in slots:
        print(f"  Checking: {slot_label(s, e)}")

    conn = get_connection()
    cur  = conn.cursor()

    base_cond = (
        "p.order_status NOT IN ('cancelled','unfulfillable') "
        "AND p.order_type='SALES' "
        "AND (p.order_qty - p.cancelled_qty) > 0 "
        f"AND {CHANNEL_FILTER}"
    )

    # Results collectors
    # plat_slot_issues: (platform, slot_label) → set("Spike","Down")
    # art_tracker:      (platform, article)    → dict
    plat_slot_issues = {}
    art_tracker      = {}

    for slot_start, slot_end in slots:
        label  = slot_label(slot_start, slot_end)
        time_s = slot_start.strftime("%H:%M:%S")
        time_e = slot_end.strftime("%H:%M:%S")

        base_dates = get_baseline_dates(slot_start)
        base_in    = ",".join(f"'{d}'" for d in base_dates)

        week_case = (
            "CASE CAST(p.channel_order_time AS DATE) " +
            " ".join(f"WHEN '{d}' THEN {i+1}" for i, d in enumerate(base_dates)) +
            " END"
        )

        # ── Query 1: Today orders per channel ──
        q_today = f"""
            SELECT p.sales_channel,
                   SUM(p.order_qty - p.cancelled_qty) AS orders
            FROM PUMA_ECOM.dbo.PUMA_Discount_ALert p
            WHERE {base_cond}
              AND CAST(p.channel_order_time AS DATE) = '{today_str}'
              AND CAST(p.channel_order_time AS TIME) >= '{time_s}'
              AND CAST(p.channel_order_time AS TIME) <  '{time_e}'
            GROUP BY p.sales_channel
        """

        # ── Query 2: Baseline per channel per week ──
        q_base = f"""
            SELECT p.sales_channel,
                   {week_case} AS week_num,
                   SUM(p.order_qty - p.cancelled_qty) AS orders
            FROM PUMA_ECOM.dbo.PUMA_Discount_ALert p
            WHERE {base_cond}
              AND CAST(p.channel_order_time AS DATE) IN ({base_in})
              AND CAST(p.channel_order_time AS TIME) >= '{time_s}'
              AND CAST(p.channel_order_time AS TIME) <  '{time_e}'
            GROUP BY p.sales_channel, {week_case}
        """

        # ── Query 3: Article orders + discount today ──
        q_art = f"""
            SELECT p.sales_channel,
                   TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT) AS article,
                   SUM(p.order_qty - p.cancelled_qty) AS orders,
                   SUM(p.order_mrp_amount) AS mrp_total,
                   SUM(CASE WHEN p.order_qty = 0 THEN 0
                            ELSE p.order_amount * (p.order_qty - p.cancelled_qty) / p.order_qty
                       END) AS fwd_realised
            FROM PUMA_ECOM.dbo.PUMA_Discount_ALert p
            WHERE {base_cond}
              AND CAST(p.channel_order_time AS DATE) = '{today_str}'
              AND CAST(p.channel_order_time AS TIME) >= '{time_s}'
              AND CAST(p.channel_order_time AS TIME) <  '{time_e}'
              AND TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT) IS NOT NULL
            GROUP BY p.sales_channel,
                     TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT)
        """

        # Fetch today
        cur.execute(q_today)
        today_by_ch = {row[0]: float(row[1]) for row in cur.fetchall()}

        # Fetch baseline
        base_by_ch = {}
        cur.execute(q_base)
        for row in cur.fetchall():
            ch = row[0]
            wk = int(row[1]) - 1 if row[1] is not None else None
            if wk is None:
                continue
            if ch not in base_by_ch:
                base_by_ch[ch] = [0.0] * 8
            base_by_ch[ch][wk] = float(row[2])

        # Fetch articles
        cur.execute(q_art)
        art_rows = []
        for row in cur.fetchall():
            ch    = row[0]
            art   = str(int(row[1])) if row[1] is not None else None
            ords  = float(row[2]) if row[2] else 0.0
            mrp   = float(row[3]) if row[3] else 0.0
            fwd   = float(row[4]) if row[4] else 0.0
            disc  = (1.0 - fwd / mrp) if mrp > 0 else 0.0
            art_rows.append((ch, art, ords, disc))

        # ── Aggregate channels → platforms ──
        plat_today = {}
        plat_base  = {}
        for ch, pname in CHANNEL_MAP.items():
            tod = today_by_ch.get(ch, 0.0)
            plat_today[pname] = plat_today.get(pname, 0.0) + tod
            b = base_by_ch.get(ch, [0.0] * 8)
            if pname not in plat_base:
                plat_base[pname] = [0.0] * 8
            for i in range(8):
                plat_base[pname][i] += b[i]

        # ── Spike / Down check ──
        for pname in set(CHANNEL_MAP.values()):
            tod            = plat_today.get(pname, 0.0)
            base, wks      = smart_baseline(plat_base.get(pname, [0.0] * 8))

            if wks < MIN_WEEKS and base < 5:
                continue
            if tod == 0 and base == 0:
                continue

            issues = set()
            if base > 0 and tod >= SPIKE_MIN_ORDERS and (tod / base) >= SPIKE_RATIO:
                issues.add("Spike")
            if base > 0 and tod > 0 and (tod / base) <= DOWN_RATIO:
                issues.add("Down")

            if issues:
                key = (pname, label)
                if key not in plat_slot_issues:
                    plat_slot_issues[key] = set()
                plat_slot_issues[key].update(issues)

        # ── High Discount check ──
        for ch, art, ords, disc in art_rows:
            if art is None or ords < DISCOUNT_MIN_ORD or disc < DISCOUNT_MIN_PCT:
                continue
            pname = CHANNEL_MAP.get(ch, ch)
            akey  = (pname, art)
            if akey not in art_tracker:
                art_tracker[akey] = {"issues": set(), "slots": set(),
                                     "orders": 0.0,   "discount": 0.0}
            art_tracker[akey]["issues"].add("High Discount")
            art_tracker[akey]["slots"].add(label)
            art_tracker[akey]["orders"]   += ords
            art_tracker[akey]["discount"]  = max(art_tracker[akey]["discount"], disc)

    conn.close()

    # ── Attach Spike to articles on spiking platforms ──
    for (pname, art), adata in art_tracker.items():
        for lbl in list(adata["slots"]):
            if "Spike" in plat_slot_issues.get((pname, lbl), set()):
                adata["issues"].add("Spike")

    # ── Build final rows ──
    final_rows = []

    # DOWN rows (platform level, no article)
    down_done = set()
    for (pname, lbl), issues in plat_slot_issues.items():
        if "Down" in issues:
            key = (pname, lbl)
            if key not in down_done:
                final_rows.append({
                    "platform": pname,
                    "slot":     lbl,
                    "issue":    "Down",
                    "article":  "-"
                })
                down_done.add(key)

    # Article rows (Spike / High Discount / both)
    for (pname, art), adata in art_tracker.items():
        issue_str = ", ".join(sorted(adata["issues"]))
        slot_str  = " & ".join(sorted(adata["slots"]))
        final_rows.append({
            "platform": pname,
            "slot":     slot_str,
            "issue":    issue_str,
            "article":  art
        })

    # Spike rows where no article crossed threshold
    spike_covered = {
        (r["platform"], s)
        for r in final_rows
        for s in r["slot"].split(" & ")
        if r["article"] != "-"
    }
    for (pname, lbl), issues in plat_slot_issues.items():
        if "Spike" in issues and (pname, lbl) not in spike_covered:
            final_rows.append({
                "platform": pname,
                "slot":     lbl,
                "issue":    "Spike",
                "article":  "-"
            })

    if not final_rows:
        print("No issues found. No email needed.")
        return False, ""

    # Sort
    final_rows.sort(key=lambda r: (r["platform"], r["slot"]))

    # ── Format table ──
    w = [13, 22, 26, 14]
    sep = "-" * (sum(w) + 3)
    hdr = (f"{'Platform':<{w[0]}} {'Time Slot':<{w[1]}} "
           f"{'Issue':<{w[2]}} {'Article':<{w[3]}}")

    lines = []
    lines.append("=" * (sum(w) + 3))
    lines.append(f"PUMA ECOM ALERT  |  {datetime.now().strftime('%d-%b-%Y %H:%M')}")
    lines.append("=" * (sum(w) + 3))
    lines.append(hdr)
    lines.append(sep)
    for r in final_rows:
        lines.append(
            f"{r['platform']:<{w[0]}} {r['slot']:<{w[1]}} "
            f"{r['issue']:<{w[2]}} {r['article']:<{w[3]}}"
        )
    lines.append("=" * (sum(w) + 3))

    report = "\n".join(lines)
    print(report)

    with open("surge_report.txt", "w") as f:
        f.write(report)

    print(f"\n{len(final_rows)} issue(s) found.")
    return True, report

# ─────────────────────────────────────────────
if __name__ == "__main__":
    found, report = run()
    if found:
        print("\nIssues detected — email will be sent here once configured.")
    else:
        print("\nAll clear — no email sent.")
