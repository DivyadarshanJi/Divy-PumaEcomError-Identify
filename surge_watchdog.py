import os
import json
import pyodbc
from datetime import date, datetime, timedelta, timezone

# ─────────────────────────────────────────────
# INDIA STANDARD TIME
# ─────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
SPIKE_RATIO      = 1.5
SPIKE_MIN_ORDERS = 100
DOWN_RATIO       = 0.5
DISCOUNT_MIN_PCT = 0.70
DISCOUNT_MIN_ORD = 5
MIN_WEEKS        = 3
TOP_ARTICLES     = 5
FLAGS_FILE       = "flagged_today.json"

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
# DB CONNECTION  (all from GitHub Secrets)
# ─────────────────────────────────────────────
def get_connection():
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={os.environ['DB_HOST']};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "TrustServerCertificate=yes;"
        "Encrypt=yes;"
    )
    return pyodbc.connect(conn_str, timeout=30)

# ─────────────────────────────────────────────
# TIME HELPERS  (all IST)
# ─────────────────────────────────────────────
def now_ist():
    return datetime.now(IST)

def today_ist():
    return now_ist().date()

def get_check_slots():
    """
    Run at X IST:
      Primary = X-1hr   to X-30min
      Safety  = X-1.5hr to X-1hr
    Example: run at 5:00PM IST
      Primary = 3:30PM - 4:00PM
      Safety  = 3:00PM - 3:30PM
    """
    now      = now_ist().replace(second=0, microsecond=0, tzinfo=None)
    mins     = 0 if now.minute < 30 else 30
    run_time = now.replace(minute=mins)
    return [
        (run_time - timedelta(minutes=60), run_time - timedelta(minutes=30)),
        (run_time - timedelta(minutes=90), run_time - timedelta(minutes=60)),
    ]

def slot_label(start, end):
    def fmt(dt):
        h      = dt.hour
        m      = dt.minute
        suffix = "AM" if h < 12 else "PM"
        h12    = h if h <= 12 else h - 12
        if h12 == 0: h12 = 12
        return f"{h12}:{m:02d}{suffix}"
    return f"{fmt(start)}-{fmt(end)}"

def time_label(dt):
    """Format IST datetime as 9:30AM style."""
    return dt.strftime("%I:%M%p").lstrip("0")

def get_baseline_dates():
    """Last 8 same weekdays as today (IST)."""
    today   = today_ist()
    weekday = today.weekday()
    dates, weeks = [], 0
    while len(dates) < 8:
        weeks += 1
        c = today - timedelta(weeks=weeks)
        if c.weekday() == weekday:
            dates.append(c.strftime("%Y-%m-%d"))
    return dates

# ─────────────────────────────────────────────
# SMART BASELINE
# Drop highest + lowest, average remaining 6
# ─────────────────────────────────────────────
def smart_baseline(vals):
    non_zero = sum(1 for v in vals if v > 0)
    if non_zero < MIN_WEEKS:
        return 0.0, non_zero
    trimmed = sorted(vals)[1:-1]
    return (sum(trimmed) / len(trimmed) if trimmed else 0.0), non_zero

# ─────────────────────────────────────────────
# FLAGS FILE
# Stores all flagged issues for today (IST)
# Resets automatically at midnight IST
# Structure:
# {
#   "date": "2026-05-07",
#   "platform_slots": {
#     "Myntra|||3:30PM-4:00PM": {
#       "issue", "orders", "baseline", "ratio",
#       "first_flagged", "articles"
#     }
#   },
#   "articles": {
#     "Myntra|||12345678": {
#       "discount", "first_flagged", "first_orders",
#       "current_orders"
#     }
#   }
# }
# ─────────────────────────────────────────────
def load_flags():
    today_str = today_ist().strftime("%Y-%m-%d")
    empty = {"date": today_str, "platform_slots": {}, "articles": {}}
    if not os.path.exists(FLAGS_FILE):
        return empty
    with open(FLAGS_FILE, "r") as f:
        data = json.load(f)
    if data.get("date") != today_str:
        return empty
    return data

def save_flags(flags):
    with open(FLAGS_FILE, "w") as f:
        json.dump(flags, f, indent=2)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run():
    now        = now_ist()
    today_str  = now.strftime("%Y-%m-%d")
    time_now   = time_label(now)
    slots      = get_check_slots()
    base_dates = get_baseline_dates()
    base_in    = ",".join(f"'{d}'" for d in base_dates)
    week_case  = (
        "CASE CAST(p.channel_order_time AS DATE) " +
        " ".join(f"WHEN '{d}' THEN {i+1}" for i, d in enumerate(base_dates)) +
        " END"
    )

    print(f"Surge Watchdog running at {now.strftime('%d-%b-%Y %H:%M')} IST")
    for s, e in slots:
        print(f"  Checking slot: {slot_label(s, e)}")

    conn = get_connection()
    cur  = conn.cursor()

    base_cond = (
        "p.order_status NOT IN ('cancelled','unfulfillable') "
        "AND p.order_type='SALES' "
        "AND (p.order_qty - p.cancelled_qty) > 0 "
        f"AND {CHANNEL_FILTER}"
    )

    # ─────────────────────────────────────────
    # SECTION 1 — SPIKE / DOWN per slot
    # ─────────────────────────────────────────
    current_s1 = {}   # (platform, slot_label) → row dict

    for slot_start, slot_end in slots:
        label  = slot_label(slot_start, slot_end)
        time_s = slot_start.strftime("%H:%M:%S")
        time_e = slot_end.strftime("%H:%M:%S")

        # Today orders per channel
        cur.execute(f"""
            SELECT p.sales_channel,
                   SUM(p.order_qty - p.cancelled_qty) AS orders
            FROM PUMA_ECOM.dbo.PUMA_Discount_ALert p
            WHERE {base_cond}
              AND CAST(p.channel_order_time AS DATE) = '{today_str}'
              AND CAST(p.channel_order_time AS TIME) >= '{time_s}'
              AND CAST(p.channel_order_time AS TIME) <  '{time_e}'
            GROUP BY p.sales_channel
        """)
        today_by_ch = {row[0]: float(row[1]) for row in cur.fetchall()}

        # Baseline per channel per week
        cur.execute(f"""
            SELECT p.sales_channel,
                   {week_case} AS week_num,
                   SUM(p.order_qty - p.cancelled_qty) AS orders
            FROM PUMA_ECOM.dbo.PUMA_Discount_ALert p
            WHERE {base_cond}
              AND CAST(p.channel_order_time AS DATE) IN ({base_in})
              AND CAST(p.channel_order_time AS TIME) >= '{time_s}'
              AND CAST(p.channel_order_time AS TIME) <  '{time_e}'
            GROUP BY p.sales_channel, {week_case}
        """)
        base_by_ch = {}
        for row in cur.fetchall():
            ch = row[0]
            wk = int(row[1]) - 1 if row[1] is not None else None
            if wk is None: continue
            if ch not in base_by_ch: base_by_ch[ch] = [0.0] * 8
            base_by_ch[ch][wk] = float(row[2])

        # Article orders + discount for this slot
        cur.execute(f"""
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
            ORDER BY SUM(p.order_qty - p.cancelled_qty) DESC
        """)
        art_by_plat = {}
        for row in cur.fetchall():
            ch   = row[0]
            art  = str(int(row[1])) if row[1] is not None else None
            ords = float(row[2]) if row[2] else 0.0
            mrp  = float(row[3]) if row[3] else 0.0
            fwd  = float(row[4]) if row[4] else 0.0
            disc = (1.0 - fwd / mrp) if mrp > 0 else 0.0
            if art is None: continue
            pname = CHANNEL_MAP.get(ch, ch)
            if pname not in art_by_plat: art_by_plat[pname] = []
            art_by_plat[pname].append({"article": art, "orders": ords, "discount": disc})

        # Aggregate channels → platforms
        plat_today, plat_base = {}, {}
        for ch, pname in CHANNEL_MAP.items():
            plat_today[pname] = plat_today.get(pname, 0.0) + today_by_ch.get(ch, 0.0)
            b = base_by_ch.get(ch, [0.0] * 8)
            if pname not in plat_base: plat_base[pname] = [0.0] * 8
            for i in range(8): plat_base[pname][i] += b[i]

        # Spike / Down check
        for pname in set(CHANNEL_MAP.values()):
            tod       = plat_today.get(pname, 0.0)
            base, wks = smart_baseline(plat_base.get(pname, [0.0] * 8))
            if wks < MIN_WEEKS and base < 5: continue
            if tod == 0 and base == 0: continue

            issues = set()
            if base > 0 and tod >= SPIKE_MIN_ORDERS and (tod / base) >= SPIKE_RATIO:
                issues.add("Spike")
            if base > 0 and tod > 0 and (tod / base) <= DOWN_RATIO:
                issues.add("Down")
            if not issues: continue

            ratio    = tod / base if base > 0 else 0.0
            top_arts = []
            if "Spike" in issues:
                top_arts = sorted(
                    art_by_plat.get(pname, []),
                    key=lambda x: -x["orders"]
                )[:TOP_ARTICLES]

            key = (pname, label)
            current_s1[key] = {
                "platform":     pname,
                "slot":         label,
                "issue":        ", ".join(sorted(issues)),
                "orders":       tod,
                "baseline":     base,
                "ratio":        ratio,
                "articles":     top_arts,
                "first_flagged": time_now,
            }

    # ─────────────────────────────────────────
    # SECTION 2 — HIGH DISCOUNT (full day)
    # ─────────────────────────────────────────
    cur.execute(f"""
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
          AND TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT) IS NOT NULL
        GROUP BY p.sales_channel,
                 TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT)
    """)
    current_disc = {}
    for row in cur.fetchall():
        ch   = row[0]
        art  = str(int(row[1])) if row[1] is not None else None
        ords = float(row[2]) if row[2] else 0.0
        mrp  = float(row[3]) if row[3] else 0.0
        fwd  = float(row[4]) if row[4] else 0.0
        disc = (1.0 - fwd / mrp) if mrp > 0 else 0.0
        if art is None or ords < DISCOUNT_MIN_ORD or disc < DISCOUNT_MIN_PCT: continue
        pname = CHANNEL_MAP.get(ch, ch)
        key   = f"{pname}|||{art}"
        if key not in current_disc:
            current_disc[key] = {"platform": pname, "article": art, "orders": 0.0, "discount": 0.0}
        current_disc[key]["orders"]   += ords
        current_disc[key]["discount"]  = max(current_disc[key]["discount"], disc)

    conn.close()

    # ─────────────────────────────────────────
    # COMPARE WITH FLAGS FILE
    # ─────────────────────────────────────────
    flags = load_flags()
    past_s1   = flags.get("platform_slots", {})
    past_disc = flags.get("articles", {})

    # Section 1: split new vs past
    new_s1  = {}
    old_s1  = {}
    for key_tuple, row in current_s1.items():
        str_key = f"{key_tuple[0]}|||{key_tuple[1]}"
        if str_key in past_s1:
            # Already flagged — update orders for reference
            entry = dict(past_s1[str_key])
            entry["orders"] = row["orders"]
            entry["articles"] = row["articles"]
            old_s1[str_key] = entry
        else:
            new_s1[str_key] = row

    # Section 2: split new vs past
    new_disc  = {}
    old_disc  = {}
    for key, data in current_disc.items():
        if key in past_disc:
            entry = dict(past_disc[key])
            entry["current_orders"] = data["orders"]
            entry["discount"]       = data["discount"]
            old_disc[key] = entry
        else:
            new_disc[key] = {
                "platform":      data["platform"],
                "article":       data["article"],
                "orders":        data["orders"],
                "discount":      data["discount"],
                "first_flagged": time_now,
                "first_orders":  data["orders"],
            }

    # ─────────────────────────────────────────
    # DECIDE WHETHER TO SEND
    # ─────────────────────────────────────────
    has_new = bool(new_s1) or bool(new_disc)

    if not has_new:
        print("No new issues. No email sent.")
        # Update current orders in past flags silently
        for k, v in old_s1.items():
            if k in past_s1:
                past_s1[k]["orders"]   = v["orders"]
                past_s1[k]["articles"] = v["articles"]
        for k, v in old_disc.items():
            if k in past_disc:
                past_disc[k]["current_orders"] = v["current_orders"]
        save_flags(flags)
        return False, ""

    # ── Save new flags ──
    for k, v in new_s1.items():
        past_s1[k] = {
            "issue":         v["issue"],
            "orders":        v["orders"],
            "baseline":      v["baseline"],
            "ratio":         v["ratio"],
            "first_flagged": v["first_flagged"],
            "articles":      v["articles"],
        }
    for k, v in old_s1.items():
        past_s1[k]["orders"]   = v["orders"]
        past_s1[k]["articles"] = v["articles"]

    for k, v in new_disc.items():
        past_disc[k] = {
            "platform":      v["platform"],
            "article":       v["article"],
            "discount":      v["discount"],
            "first_flagged": v["first_flagged"],
            "first_orders":  v["first_orders"],
        }
    for k, v in old_disc.items():
        past_disc[k]["current_orders"] = v["current_orders"]
        past_disc[k]["discount"]       = v["discount"]

    flags["platform_slots"] = past_s1
    flags["articles"]       = past_disc
    save_flags(flags)

    # ─────────────────────────────────────────
    # BUILD REPORT
    # ─────────────────────────────────────────
    def art_str(articles):
        if not articles: return "-"
        parts = []
        for a in articles:
            d = f"{a['discount']*100:.0f}%"
            parts.append(f"{a['article']} ({int(a['orders'])} orders, {d} disc)")
        return",  ".join(parts)

    def s1_row_line(r, w):
        return (
            f"{r['platform']:<{w[0]}} {r['slot']:<{w[1]}} "
            f"{r['issue']:<{w[2]}} {int(r['orders']):>{w[3]},} "
            f"{int(r['baseline']):>{w[4]},} {r['ratio']:>{w[5]}.2f}x  "
            f"{art_str(r['articles'])}"
        )

    W   = [13, 18, 7, 7, 9, 5]
    HDR = (f"{'Platform':<{W[0]}} {'Time Slot':<{W[1]}} {'Issue':<{W[2]}} "
           f"{'Orders':>{W[3]}} {'Baseline':>{W[4]}} {'Ratio':>{W[5]+1}}  "
           f"Top Articles (Orders, Discount%)")
    SEP = "-" * 95

    lines = []
    lines.append("=" * 95)
    lines.append(f"PUMA ECOM ALERT  |  {now.strftime('%d-%b-%Y %H:%M')} IST")
    lines.append("=" * 95)

    # ── Section 1 ──
    if new_s1 or old_s1:
        lines.append("")
        lines.append("SECTION 1 — ORDER SPIKE / DOWN")
        lines.append("")

        if new_s1:
            lines.append("  NEW ISSUES")
            lines.append("  " + SEP)
            lines.append("  " + HDR)
            lines.append("  " + SEP)
            for r in sorted(new_s1.values(), key=lambda x: (x["platform"], x["slot"])):
                lines.append("  " + s1_row_line(r, W))
            lines.append("")

        if old_s1:
            lines.append("  PAST ISSUES  (flagged earlier today)")
            lines.append("  " + SEP)
            lines.append("  " + HDR)
            lines.append("  " + SEP)
            for r in sorted(old_s1.values(), key=lambda x: (x["platform"], x["slot"])):
                lines.append("  " + s1_row_line(r, W))
            lines.append("")

    # ── Section 2 ──
    if new_disc or old_disc:
        lines.append("=" * 70)
        lines.append("SECTION 2 — HIGH DISCOUNT ARTICLES  (>= 70% discount, >= 5 orders today)")
        lines.append("")

        disc_hdr = (f"  {'Platform':<13} {'Article':<13} "
                    f"{'Orders':>10} {'Discount%':>10}  {'First Flagged'}")
        disc_sep = "  " + "-" * 65

        if new_disc:
            lines.append("  NEW FLAGS")
            lines.append(disc_sep)
            lines.append(disc_hdr)
            lines.append(disc_sep)
            for r in sorted(new_disc.values(), key=lambda x: (x["platform"], -x["orders"])):
                lines.append(
                    f"  {r['platform']:<13} {r['article']:<13} "
                    f"{int(r['orders']):>10,} {r['discount']*100:>9.1f}%  "
                    f"{r['first_flagged']}"
                )
            lines.append("")

        if old_disc:
            lines.append("  PAST FLAGS  (flagged earlier today — current orders shown)")
            lines.append(disc_sep)
            lines.append(
                f"  {'Platform':<13} {'Article':<13} "
                f"{'Curr Orders':>11} {'Discount%':>10}  {'First Flagged'}"
            )
            lines.append(disc_sep)
            for r in sorted(old_disc.values(), key=lambda x: (x["platform"], -x.get("current_orders", 0))):
                lines.append(
                    f"  {r['platform']:<13} {r['article']:<13} "
                    f"{int(r.get('current_orders', r.get('first_orders', 0))):>11,} "
                    f"{r['discount']*100:>9.1f}%  "
                    f"{r['first_flagged']}"
                )
            lines.append("")

    lines.append("=" * 95)
    report = "\n".join(lines)
    print(report)

    with open("surge_report.txt", "w") as f:
        f.write(report)

    print(f"\nNew issues: {len(new_s1)} spike/down  |  {len(new_disc)} high discount")
    return True, report

# ─────────────────────────────────────────────
if __name__ == "__main__":
    found, report = run()
    if found:
        print("\nIssues detected — email will be sent here once configured.")
    else:
        print("\nAll clear — no email sent.")
