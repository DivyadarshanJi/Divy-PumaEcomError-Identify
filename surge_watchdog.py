import os
import pyodbc
from datetime import date, datetime, timedelta

# ─────────────────────────────────────────────
# CONSTANTS  (same as your VBA)
# ─────────────────────────────────────────────
SPIKE_SURGE_RATIO  = 1.5
SPIKE_MIN_ORDERS   = 100
SPIKE_WATCH_RATIO  = 1.5
SPIKE_WATCH_MIN    = 50
DROP_ALERT_RATIO   = 0.5
DROP_WATCH_RATIO   = 0.75
ART_MIN_BASELINE   = 5
ART_MIN_TODAY_ORD  = 50
MIN_WEEKS_CONF     = 3
TOP_ARTICLES       = 5

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

ALL_CHANNELS = list(CHANNEL_MAP.keys())

CHANNEL_FILTER = "p.sales_channel IN ({})".format(
    ",".join(f"'{c}'" for c in ALL_CHANNELS)
)

DISPLAY_ORDER = [
    "Myntra","Amazon","Flipkart","Nykaa","Ajio",
    "TataCliq","TataCliqLux","Puma.com","RCB",
    "Magicpin","Cred","GoFynd","Firstcry"
]

# ─────────────────────────────────────────────
# DB CONNECTION  (credentials from env/secrets)
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
# HELPERS
# ─────────────────────────────────────────────
def hour_label(hr):
    def fmt(h):
        if h == 0:   return "12AM"
        if h < 12:   return f"{h}AM"
        if h == 12:  return "12PM"
        return f"{h-12}PM"
    return f"{fmt(hr)} - {fmt((hr+1) % 24)}"

def smart_baseline(vals):
    """Remove highest + lowest, average the rest. Same logic as VBA."""
    non_zero = sum(1 for v in vals if v > 0)
    if non_zero < 2:
        return sum(vals) / len(vals), non_zero
    min_i = vals.index(min(vals))
    max_i = vals.index(max(vals))
    filtered = [v for i, v in enumerate(vals) if i != min_i and i != max_i]
    avg = sum(filtered) / len(filtered) if filtered else (min(vals) + max(vals)) / 2
    return avg, non_zero

def get_status(today_ord, base_ord, wks):
    if wks < MIN_WEEKS_CONF and base_ord < 5:
        return 0
    if base_ord == 0:
        return 1 if today_ord >= SPIKE_MIN_ORDERS else 0
    r = today_ord / base_ord
    if r >= SPIKE_SURGE_RATIO:
        return 3 if today_ord >= SPIKE_MIN_ORDERS else 1
    if r >= SPIKE_WATCH_RATIO:
        return 1 if today_ord >= SPIKE_WATCH_MIN else 0
    if r <= DROP_ALERT_RATIO:
        return 2
    if r <= DROP_WATCH_RATIO:
        return 1
    return 0

STATUS_LABEL = {3: "SURGE", 2: "ALERT", 1: "WATCH", 0: "OK"}

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run():
    today      = date.today()
    today_str  = today.strftime("%Y-%m-%d")
    current_hr = datetime.now().hour

    # Build base week dates (8 same weekdays going back)
    base_weeks = [
        (today - timedelta(weeks=i+1)).strftime("%Y-%m-%d")
        for i in range(8)
    ]
    base_in    = ",".join(f"'{d}'" for d in base_weeks)

    week_case = "CASE CAST(p.channel_order_time AS DATE) " + \
                " ".join(f"WHEN '{d}' THEN {i+1}" for i, d in enumerate(base_weeks)) + \
                " END"

    base_cond = (
        "p.order_status NOT IN ('cancelled','unfulfillable') "
        "AND p.order_type='SALES' "
        "AND (p.order_qty - p.cancelled_qty) > 0 "
        f"AND {CHANNEL_FILTER}"
    )

    q1 = f"""
        SELECT p.sales_channel,
               DATEPART(HOUR, p.channel_order_time) AS hr,
               SUM(p.order_qty - p.cancelled_qty) AS orders
        FROM PUMA_ECOM.dbo.PUMA_Discount_ALert p
        WHERE {base_cond}
          AND CAST(p.channel_order_time AS DATE) = '{today_str}'
        GROUP BY p.sales_channel, DATEPART(HOUR, p.channel_order_time)
    """

    q2 = f"""
        SELECT p.sales_channel,
               DATEPART(HOUR, p.channel_order_time) AS hr,
               {week_case} AS week_num,
               SUM(p.order_qty - p.cancelled_qty) AS orders
        FROM PUMA_ECOM.dbo.PUMA_Discount_ALert p
        WHERE {base_cond}
          AND CAST(p.channel_order_time AS DATE) IN ({base_in})
        GROUP BY p.sales_channel,
                 DATEPART(HOUR, p.channel_order_time),
                 {week_case}
    """

    q4 = f"""
        SELECT p.sales_channel,
               DATEPART(HOUR, p.channel_order_time) AS hr,
               TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT) AS article,
               SUM(p.order_qty - p.cancelled_qty) AS orders
        FROM PUMA_ECOM.dbo.PUMA_Discount_ALert p
        WHERE {base_cond}
          AND CAST(p.channel_order_time AS DATE) = '{today_str}'
          AND TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT) IS NOT NULL
        GROUP BY p.sales_channel,
                 DATEPART(HOUR, p.channel_order_time),
                 TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT)
    """

    q5 = f"""
        SELECT p.sales_channel,
               DATEPART(HOUR, p.channel_order_time) AS hr,
               TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT) AS article,
               {week_case} AS week_num,
               SUM(p.order_qty - p.cancelled_qty) AS orders
        FROM PUMA_ECOM.dbo.PUMA_Discount_ALert p
        WHERE {base_cond}
          AND CAST(p.channel_order_time AS DATE) IN ({base_in})
          AND TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT) IS NOT NULL
        GROUP BY p.sales_channel,
                 DATEPART(HOUR, p.channel_order_time),
                 TRY_CAST(LTRIM(REPLACE(UPPER(p.Style),'IN','')) AS BIGINT),
                 {week_case}
    """

    # ── Fetch data ──
    print("Connecting to database...")
    conn = get_connection()
    cur  = conn.cursor()

    print("Fetching today orders...")
    cur.execute(q1)
    d_today = {}
    for row in cur.fetchall():
        d_today[f"{row[0]}|{row[1]}"] = float(row[2])

    print("Fetching baseline volumes...")
    cur.execute(q2)
    d_base = {}
    for row in cur.fetchall():
        d_base[f"{row[0]}|{row[1]}|{row[2]}"] = float(row[3])

    print("Fetching article today orders...")
    cur.execute(q4)
    d_art_today = {}
    for row in cur.fetchall():
        d_art_today[f"{row[0]}|{row[1]}|{row[2]}"] = float(row[3])

    print("Fetching article baseline...")
    cur.execute(q5)
    d_art_base = {}
    for row in cur.fetchall():
        d_art_base[f"{row[0]}|{row[1]}|{row[2]}|{row[3]}"] = float(row[4])

    conn.close()
    print("Data fetched. Computing surges...")

    # ── Compute ──
    plat_status  = {p: 0   for p in DISPLAY_ORDER}
    plat_vol     = {p: 0.0 for p in DISPLAY_ORDER}
    flagged_rows = []   # (platform, hr, label, today_ord, base_ord, ratio, status)
    art_rows     = []   # (platform, hr, article, today_ord, base_ord, ratio)

    for ch, pname in CHANNEL_MAP.items():
        if pname not in plat_status:
            plat_status[pname] = 0
            plat_vol[pname]    = 0.0

        for hr in range(current_hr + 1):
            today_ord = d_today.get(f"{ch}|{hr}", 0.0)
            vol_vals  = [d_base.get(f"{ch}|{hr}|{w+1}", 0.0) for w in range(8)]
            base_ord, wks = smart_baseline(vol_vals)

            sc = get_status(today_ord, base_ord, wks)
            ratio = (today_ord / base_ord) if base_ord > 0 else (99 if today_ord > 0 else 1.0)

            if sc > plat_status[pname]:
                plat_status[pname] = sc
            plat_vol[pname] += today_ord

            if sc >= 1:
                flagged_rows.append((pname, hr, hour_label(hr), today_ord, base_ord, ratio, sc))

                # Article drilldown for SURGE hours
                if sc >= 3:
                    for key, art_tod in d_art_today.items():
                        parts = key.split("|")
                        if parts[0] == ch and int(parts[1]) == hr:
                            art_num = parts[2]
                            if art_tod < ART_MIN_TODAY_ORD:
                                continue
                            a_vals = [d_art_base.get(f"{ch}|{hr}|{art_num}|{w+1}", 0.0) for w in range(8)]
                            a_base, _ = smart_baseline(a_vals)
                            if a_base > 0:
                                a_ratio = art_tod / a_base
                            elif art_tod > 0:
                                a_ratio = 99
                            else:
                                continue
                            art_rows.append((pname, hr, art_num, art_tod, a_base, a_ratio))

    # ── Sort platforms by worst status then volume ──
    def plat_sort_key(p):
        st = plat_status.get(p, 0)
        vol = plat_vol.get(p, 0)
        return (-st, -vol)

    sorted_plats = sorted(plat_status.keys(), key=plat_sort_key)

    # ── Build report ──
    surge_found = any(r[6] >= 2 for r in flagged_rows)

    report_lines = []
    report_lines.append("=" * 60)
    report_lines.append(f"SURGE MONITOR  |  {datetime.now().strftime('%d-%b-%Y %H:%M')}")
    report_lines.append(f"Baseline: 8 weeks same day, drop highest+lowest")
    report_lines.append("=" * 60)

    if surge_found:
        report_lines.append("")
        report_lines.append("!! SURGE / ALERT SUMMARY !!")
        report_lines.append("-" * 60)
        report_lines.append(f"{'Status':<8} {'Platform':<14} {'Hour':<14} {'Orders':>7} {'Baseline':>9} {'Ratio':>6}")
        report_lines.append("-" * 60)
        for pname in sorted_plats:
            for r in flagged_rows:
                if r[0] == pname and r[6] >= 2:
                    report_lines.append(
                        f"{STATUS_LABEL[r[6]]:<8} {r[0]:<14} {r[2]:<14} "
                        f"{int(r[3]):>7,} {int(r[4]):>9,} {r[5]:>5.1f}x"
                    )

    report_lines.append("")
    report_lines.append("-" * 60)
    report_lines.append(f"{'Platform':<14} {'Hour':<14} {'Status':<7} {'Orders':>7} {'Baseline':>9} {'Ratio':>6}")
    report_lines.append("-" * 60)

    for pname in sorted_plats:
        plat_rows = [r for r in flagged_rows if r[0] == pname]
        if not plat_rows:
            continue
        report_lines.append(f"  {pname}")
        for r in sorted(plat_rows, key=lambda x: x[1]):
            report_lines.append(
                f"  {'':12} {r[2]:<14} {STATUS_LABEL[r[6]]:<7} "
                f"{int(r[3]):>7,} {int(r[4]):>9,} {r[5]:>5.1f}x"
            )
            # Top articles for SURGE
            top_arts = sorted(
                [a for a in art_rows if a[0] == pname and a[1] == r[1]],
                key=lambda x: -x[3]
            )[:TOP_ARTICLES]
            for a in top_arts:
                ratio_str = "New" if a[5] >= 99 else f"{a[5]:.1f}x"
                report_lines.append(
                    f"    Article {a[2]:<10} Orders:{int(a[3]):>6,}  "
                    f"Base:{int(a[4]):>6,}  Ratio:{ratio_str}"
                )
        report_lines.append("")

    report_lines.append("=" * 60)
    report_lines.append("LEGEND:")
    report_lines.append("SURGE = orders >= 1.5x baseline AND >= 100 orders")
    report_lines.append("ALERT = orders <= 50% of baseline")
    report_lines.append("WATCH = orders between 50%-150% of baseline boundaries")
    report_lines.append("OK    = orders within normal range")
    report_lines.append("=" * 60)

    report = "\n".join(report_lines)
    print(report)

    # ── Save report to file (GitHub Actions will show in logs) ──
    with open("surge_report.txt", "w") as f:
        f.write(report)

    print("\nDone.")
    return surge_found, report

# ─────────────────────────────────────────────
if __name__ == "__main__":
    run()
