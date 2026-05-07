import os
import json
import pyodbc
from datetime import date, datetime, timedelta, timezone

# India Standard Time = UTC + 5:30
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
FLAGS_FILE       = "flagged_discounts.json"

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
    server   = os.environ["DB_HOST"]
    database = os.environ["DB_NAME"]
    username = os.environ["DB_USER"]
    password = os.environ["DB_PASSWORD"]
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
    now      = datetime.now(IST).replace(second=0, microsecond=0, tzinfo=None)
    mins     = 0 if now.minute < 30 else 30
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
        h      = dt.hour
        m      = dt.minute
        suffix = "AM" if h < 12 else "PM"
        h12    = h if h <= 12 else h - 12
        if h12 == 0: h12 = 12
        return f"{h12}:{m:02d}{suffix}"
    return f"{fmt(start)}-{fmt(end)}"

def get_baseline_dates(slot_start):
    today   = datetime.now(IST).date()
    weekday = today.weekday()
    dates   = []
    weeks   = 0
    while len(dates) < 8:
        weeks    += 1
        candidate = today - timedelta(weeks=weeks)
        if candidate.weekday() == weekday:
            dates.append(candidate.strftime("%Y-%m-%d"))
    return dates

# ─────────────────────────────────────────────
# SMART BASELINE
# ─────────────────────────────────────────────
def smart_baseline(vals):
    non_zero = sum(1 for v in vals if v > 0)
    if non_zero < MIN_WEEKS:
        return 0.0, non_zero
    if len(vals) < 3:
        return sum(vals) / len(vals), non_zero
    trimmed = sorted(vals)[1:-1]
    avg     = sum(trimmed) / len(trimmed) if trimmed else 0.0
    return avg, non_zero

# ─────────────────────────────────────────────
# FLAGS FILE — load / save / update
# ─────────────────────────────────────────────
def load_flags():
    """
    Load flagged_discounts.json.
    Returns dict: {date, articles: {(platform,article): {discount, first_flagged, first_orders}}}
    Resets if date has changed.
    """
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    if not os.path.exists(FLAGS_FILE):
        return {"date": today_str, "articles": {}}

    with open(FLAGS_FILE, "r") as f:
        data = json.load(f)

    # Reset if new day
    if data.get("date") != today_str:
        return {"date": today_str, "articles": {}}

    return data

def save_flags(flags):
    # Convert tuple keys to string for JSON
    serializable = {
        "date": flags["date"],
        "articles": {
            f"{k[0]}|||{k[1]}": v
            for k, v in flags["articles"].items()
        }
    }
    with open(FLAGS_FILE, "w") as f:
        json.dump(serializable, f, indent=2)

def deserialize_flags(flags):
    """Convert string keys back to tuples after loading."""
    if not flags["articles"]:
        return flags
    deserialized = {}
    for k, v in flags["articles"].items():
        if "|||" in k:
            parts = k.split("|||")
            deserialized[(parts[0], parts[1])] = v
        else:
            deserialized[k] = v
    flags["articles"] = deserialized
    return flags

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run():
    now_ist   = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    time_now  = now_ist.strftime("%I:%M%p").lstrip("0")
    slots     = get_check_slots()

    print(f"Surge Watchdog running at {now_ist.strftime('%d-%b-%Y %H:%M')} IST")
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

    # ─────────────────────────────────────────
    # SECTION 1 — SPIKE / DOWN (per slot)
    # ─────────────────────────────────────────
    plat_slot_data = {}

    for slot_start, slot_end in slots:
        label      = slot_label(slot_start, slot_end)
        time_s     = slot_start.strftime("%H:%M:%S")
        time_e     = slot_end.strftime("%H:%M:%S")
        base_dates = get_baseline_dates(slot_start)
        base_in    = ",".join(f"'{d}'" for d in base_dates)
        week_case  = (
            "CASE CAST(p.channel_order_time AS DATE) " +
            " ".join(f"WHEN '{d}' THEN {i+1}" for i, d in enumerate(base_dates)) +
            " END"
        )

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
            if wk is None:
                continue
            if ch not in base_by_ch:
                base_by_ch[ch] = [0.0] * 8
            base_by_ch[ch][wk] = float(row[2])

        # Article orders + discount for spike
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
            if art is None:
                continue
            pname = CHANNEL_MAP.get(ch, ch)
            if pname not in art_by_plat:
                art_by_plat[pname] = []
            art_by_plat[pname].append({"article": art, "orders": ords, "discount": disc})

        # Aggregate channels → platforms
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

        # Spike / Down check
        for pname in set(CHANNEL_MAP.values()):
            tod       = plat_today.get(pname, 0.0)
            base, wks = smart_baseline(plat_base.get(pname, [0.0] * 8))

            if wks < MIN_WEEKS and base < 5:
                continue
            if tod == 0 and base == 0:
                continue

            issues = set()
            if base > 0 and tod >= SPIKE_MIN_ORDERS and (tod / base) >= SPIKE_RATIO:
                issues.add("Spike")
            if base > 0 and tod > 0 and (tod / base) <= DOWN_RATIO:
                issues.add("Down")

            if not issues:
                continue

            ratio = (tod / base) if base > 0 else 0.0
            key   = (pname, label)

            top_arts = []
            if "Spike" in issues:
                arts      = art_by_plat.get(pname, [])
                top_arts  = sorted(arts, key=lambda x: -x["orders"])[:TOP_ARTICLES]

            if key not in plat_slot_data:
                plat_slot_data[key] = {
                    "platform": pname,
                    "slot":     label,
                    "issues":   set(),
                    "orders":   tod,
                    "baseline": base,
                    "ratio":    ratio,
                    "articles": top_arts
                }
            plat_slot_data[key]["issues"].update(issues)
            if "Spike" in issues and not plat_slot_data[key]["articles"]:
                plat_slot_data[key]["articles"] = top_arts

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

    # All articles crossing threshold today
    all_disc = {}  # (platform, article) → {orders, discount}
    for row in cur.fetchall():
        ch   = row[0]
        art  = str(int(row[1])) if row[1] is not None else None
        ords = float(row[2]) if row[2] else 0.0
        mrp  = float(row[3]) if row[3] else 0.0
        fwd  = float(row[4]) if row[4] else 0.0
        disc = (1.0 - fwd / mrp) if mrp > 0 else 0.0
        if art is None or ords < DISCOUNT_MIN_ORD or disc < DISCOUNT_MIN_PCT:
            continue
        pname = CHANNEL_MAP.get(ch, ch)
        key   = (pname, art)
        if key not in all_disc:
            all_disc[key] = {"platform": pname, "article": art,
                             "orders": 0.0, "discount": 0.0}
        all_disc[key]["orders"]   += ords
        all_disc[key]["discount"]  = max(all_disc[key]["discount"], disc)

    conn.close()

    # ── Load flags file ──
    flags = deserialize_flags(load_flags())
    already_flagged = flags["articles"]  # (platform, article) → {discount, first_flagged, first_orders}

    # ── Split into new vs past ──
    new_flags  = {}
    past_flags = {}

    for key, data in all_disc.items():
        if key in already_flagged:
            # Past flag — update current orders
            past_entry = dict(already_flagged[key])
            past_entry["current_orders"] = data["orders"]
            past_entry["discount"]       = data["discount"]
            past_flags[key] = past_entry
        else:
            # New flag
            new_flags[key] = {
                "platform":     data["platform"],
                "article":      data["article"],
                "orders":       data["orders"],
                "discount":     data["discount"],
                "first_flagged": time_now,
                "first_orders":  data["orders"]
            }

    # ── Decide whether to send email ──
    s1_rows     = list(plat_slot_data.values())
    has_new_disc = len(new_flags) > 0
    should_send  = bool(s1_rows) or has_new_disc

    if not should_send:
        print("No new issues found. No email sent.")
        # Still save updated past flags with current orders
        if past_flags:
            for key, v in past_flags.items():
                already_flagged[key]["current_orders"] = v["current_orders"]
            save_flags(flags)
        return False, ""

    # ── Update flags file ──
    for key, v in new_flags.items():
        already_flagged[key] = {
            "discount":      v["discount"],
            "first_flagged": v["first_flagged"],
            "first_orders":  v["first_orders"]
        }
    for key, v in past_flags.items():
        already_flagged[key]["current_orders"] = v["current_orders"]
    save_flags(flags)

    # ─────────────────────────────────────────
    # BUILD REPORT
    # ─────────────────────────────────────────
    s1_rows.sort(key=lambda r: (r["platform"], r["slot"]))
    new_list  = sorted(new_flags.values(),  key=lambda r: (r["platform"], -r["orders"]))
    past_list = sorted(past_flags.values(), key=lambda r: (r["platform"], -r.get("current_orders", 0)))

    run_time_str = now_ist.strftime("%d-%b-%Y %H:%M")
    lines = []

    # ── Section 1 ──
    if s1_rows:
        lines.append("=" * 95)
        lines.append(f"PUMA ECOM ALERT  |  {run_time_str} IST")
        lines.append("=" * 95)
        lines.append("")
        lines.append("SECTION 1 — ORDER SPIKE / DOWN  (last 2 slots)")
        lines.append("-" * 95)
        lines.append(
            f"{'Platform':<13} {'Time Slot':<18} {'Issue':<7} "
            f"{'Orders':>7} {'Baseline':>9} {'Ratio':>6}  "
            f"Top Articles (Orders, Discount%)"
        )
        lines.append("-" * 95)
        for r in s1_rows:
            issue_str = ", ".join(sorted(r["issues"]))
            ratio_str = f"{r['ratio']:.2f}x"
            if r["articles"]:
                art_parts = []
                for a in r["articles"]:
                    disc_str = f"{a['discount']*100:.0f}%"
                    art_parts.append(f"{a['article']} ({int(a['orders'])} orders, {disc_str} disc)")
                art_str = ",  ".join(art_parts)
            else:
                art_str = "-"
            lines.append(
                f"{r['platform']:<13} {r['slot']:<18} {issue_str:<7} "
                f"{int(r['orders']):>7,} {int(r['baseline']):>9,} {ratio_str:>6}  "
                f"{art_str}"
            )
        lines.append("")

    # ── Section 2 ──
    if new_list or past_list:
        lines.append("=" * 70)
        lines.append("SECTION 2 — HIGH DISCOUNT ARTICLES  (>= 70% discount, >= 5 orders)")
        lines.append("")

        if new_list:
            lines.append("  NEW FLAGS")
            lines.append("  " + "-" * 65)
            lines.append(
                f"  {'Platform':<13} {'Article':<13} "
                f"{'Orders':>8} {'Discount%':>10}  {'First Flagged'}"
            )
            lines.append("  " + "-" * 65)
            for r in new_list:
                lines.append(
                    f"  {r['platform']:<13} {r['article']:<13} "
                    f"{int(r['orders']):>8,} {r['discount']*100:>9.1f}%  "
                    f"{r['first_flagged']}"
                )
            lines.append("")

        if past_list:
            lines.append("  PAST FLAGS  (flagged earlier today)")
            lines.append("  " + "-" * 65)
            lines.append(
                f"  {'Platform':<13} {'Article':<13} "
                f"{'Curr Orders':>11} {'Discount%':>10}  {'First Flagged'}"
            )
            lines.append("  " + "-" * 65)
            for r in past_list:
                lines.append(
                    f"  {r['platform']:<13} {r['article']:<13} "
                    f"{int(r.get('current_orders', r.get('first_orders',0))):>11,} "
                    f"{r['discount']*100:>9.1f}%  "
                    f"{r['first_flagged']}"
                )
            lines.append("")

    lines.append("=" * 95)
    report = "\n".join(lines)
    print(report)

    with open("surge_report.txt", "w") as f:
        f.write(report)

    total = len(s1_rows) + len(new_list)
    print(f"\n{total} new issue(s) found.")
    return True, report

# ─────────────────────────────────────────────
if __name__ == "__main__":
    found, report = run()
    if found:
        print("\nIssues detected — email will be sent here once configured.")
    else:
        print("\nAll clear — no email sent.")
