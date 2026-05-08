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
TOP_ARTICLES = 5
FLAGS_FILE   = "flagged_today.json"
CONFIG_FILE  = "config.json"

DEFAULT_THRESHOLDS = {
    "spike_ratio":         1.5,
    "spike_min_orders":    100,
    "down_ratio":          0.5,
    "discount_min_pct":    70,
    "discount_min_orders": 5,
}

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
# LOAD CONFIG  (local file)
# ─────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        print("WARNING: config.json not found. Using defaults.")
        return {
            "cc_all":      [],
            "spoc_emails": {},
            "thresholds":  {"_default": DEFAULT_THRESHOLDS.copy()}
        }
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
    print("Config loaded from config.json")
    return cfg

# ─────────────────────────────────────────────
# CONFIG HELPERS
# ─────────────────────────────────────────────
def to_list(val):
    """Converts single email string or list to a clean list."""
    if not val:
        return []
    if isinstance(val, list):
        return [e.strip() for e in val if e.strip()]
    return [val.strip()] if val.strip() else []

def get_cc_emails(cfg):
    """Returns cc_all as a list."""
    return to_list(cfg.get("cc_all", []))

def get_platform_emails(cfg, platform):
    """
    Returns (to_list, cc_list) for a platform.
    If platform has no SPOC → to_list is empty, falls back to cc_all.
    """
    spoc = to_list(cfg.get("spoc_emails", {}).get(platform, ""))
    cc   = get_cc_emails(cfg)
    if not spoc:
        # No SPOC → send to cc_all only
        return cc, []
    return spoc, cc

def get_platform_thresholds(cfg, platform):
    """
    Returns thresholds for a platform.
    use_global=true → use _default values
    use_global=false → use platform specific values
    """
    default = cfg.get("thresholds", {}).get("_default", DEFAULT_THRESHOLDS)
    plat    = cfg.get("thresholds", {}).get(platform, {})

    if plat.get("use_global", True):
        t = default
    else:
        t = plat

    return {
        "spike_ratio":         float(t.get("spike_ratio",         DEFAULT_THRESHOLDS["spike_ratio"])),
        "spike_min_orders":    int(t.get("spike_min_orders",       DEFAULT_THRESHOLDS["spike_min_orders"])),
        "down_ratio":          float(t.get("down_ratio",           DEFAULT_THRESHOLDS["down_ratio"])),
        "discount_min_pct":    float(t.get("discount_min_pct",     DEFAULT_THRESHOLDS["discount_min_pct"])) / 100,
        "discount_min_orders": int(t.get("discount_min_orders",    DEFAULT_THRESHOLDS["discount_min_orders"])),
    }

# ─────────────────────────────────────────────
# DB CONNECTION
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
# TIME HELPERS
# ─────────────────────────────────────────────
def now_ist():
    return datetime.now(IST)

def today_ist():
    return now_ist().date()

def get_check_slots():
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
    return dt.strftime("%I:%M%p").lstrip("0")

def get_baseline_dates():
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
# ─────────────────────────────────────────────
def smart_baseline(vals, min_weeks=3):
    non_zero = sum(1 for v in vals if v > 0)
    if non_zero < min_weeks:
        return 0.0, non_zero
    trimmed = sorted(vals)[1:-1]
    return (sum(trimmed) / len(trimmed) if trimmed else 0.0), non_zero

# ─────────────────────────────────────────────
# FLAGS FILE
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
# REPORT HELPERS
# ─────────────────────────────────────────────
def art_str(articles):
    if not articles: return "-"
    parts = []
    for a in articles:
        d = f"{a['discount']*100:.0f}%"
        parts.append(f"{a['article']} ({int(a['orders'])} orders, {d} disc)")
    return ",  ".join(parts)

def s1_row_line(r, w):
    return (
        f"{r['platform']:<{w[0]}} {r['slot']:<{w[1]}} "
        f"{r['issue']:<{w[2]}} {int(r['orders']):>{w[3]},} "
        f"{int(r['baseline']):>{w[4]},} {r['ratio']:>{w[5]}.2f}x  "
        f"{art_str(r['articles'])}"
    )

# ─────────────────────────────────────────────
# BUILD EMAIL BODY
# ─────────────────────────────────────────────
def build_platform_email(platform, new_s1, old_s1, new_disc, old_disc, run_time_str):
    W   = [13, 18, 7, 7, 9, 5]
    HDR = (f"{'Platform':<{W[0]}} {'Time Slot':<{W[1]}} {'Issue':<{W[2]}} "
           f"{'Orders':>{W[3]}} {'Baseline':>{W[4]}} {'Ratio':>{W[5]+1}}  "
           f"Top Articles (Orders, Discount%)")
    SEP = "-" * 95

    lines = []
    lines.append("=" * 95)
    lines.append(f"PUMA ECOM ALERT — {platform}  |  {run_time_str} IST")
    lines.append("=" * 95)

    # Section 1
    p_new_s1 = {k: v for k, v in new_s1.items() if v["platform"] == platform}
    p_old_s1 = {k: v for k, v in old_s1.items() if v["platform"] == platform}

    if p_new_s1 or p_old_s1:
        lines.append("")
        lines.append("SECTION 1 — ORDER SPIKE / DOWN")
        lines.append("")
        if p_new_s1:
            lines.append("  NEW ISSUES")
            lines.append("  " + SEP)
            lines.append("  " + HDR)
            lines.append("  " + SEP)
            for r in sorted(p_new_s1.values(), key=lambda x: x["slot"]):
                lines.append("  " + s1_row_line(r, W))
            lines.append("")
        if p_old_s1:
            lines.append("  PAST ISSUES  (flagged earlier today)")
            lines.append("  " + SEP)
            lines.append("  " + HDR)
            lines.append("  " + SEP)
            for r in sorted(p_old_s1.values(), key=lambda x: x["slot"]):
                lines.append("  " + s1_row_line(r, W))
            lines.append("")

    # Section 2
    p_new_disc = {k: v for k, v in new_disc.items() if v["platform"] == platform}
    p_old_disc = {k: v for k, v in old_disc.items() if v["platform"] == platform}

    if p_new_disc or p_old_disc:
        disc_hdr = (f"  {'Platform':<13} {'Article':<13} "
                    f"{'Orders':>10} {'Discount%':>10}  {'First Flagged'}")
        disc_sep = "  " + "-" * 65
        lines.append("=" * 70)
        lines.append("SECTION 2 — HIGH DISCOUNT ARTICLES")
        lines.append("")
        if p_new_disc:
            lines.append("  NEW FLAGS")
            lines.append(disc_sep)
            lines.append(disc_hdr)
            lines.append(disc_sep)
            for r in sorted(p_new_disc.values(), key=lambda x: -x["orders"]):
                lines.append(
                    f"  {r['platform']:<13} {r['article']:<13} "
                    f"{int(r['orders']):>10,} {r['discount']*100:>9.1f}%  "
                    f"{r['first_flagged']}"
                )
            lines.append("")
        if p_old_disc:
            lines.append("  PAST FLAGS  (flagged earlier today — current orders shown)")
            lines.append(disc_sep)
            lines.append(
                f"  {'Platform':<13} {'Article':<13} "
                f"{'Curr Orders':>11} {'Discount%':>10}  {'First Flagged'}"
            )
            lines.append(disc_sep)
            for r in sorted(p_old_disc.values(), key=lambda x: -x.get("current_orders", 0)):
                lines.append(
                    f"  {r['platform']:<13} {r['article']:<13} "
                    f"{int(r.get('current_orders', r.get('first_orders', 0))):>11,} "
                    f"{r['discount']*100:>9.1f}%  "
                    f"{r['first_flagged']}"
                )
            lines.append("")

    lines.append("=" * 95)
    return "\n".join(lines)

# ─────────────────────────────────────────────
# SEND EMAIL  (placeholder — SMTP/Graph later)
# ─────────────────────────────────────────────
def send_email(to_emails, cc_emails, subject, body):
    """
    to_emails and cc_emails are always lists.
    Placeholder — will be replaced with real email logic.
    """
    print(f"\n--- EMAIL ---")
    print(f"TO:      {', '.join(to_emails)}")
    print(f"CC:      {', '.join(cc_emails)}")
    print(f"SUBJECT: {subject}")
    print(body)
    print("--- END EMAIL ---\n")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run():
    now          = now_ist()
    today_str    = now.strftime("%Y-%m-%d")
    time_now     = time_label(now)
    run_time_str = now.strftime("%d-%b-%Y %H:%M")
    slots        = get_check_slots()
    base_dates   = get_baseline_dates()
    base_in      = ",".join(f"'{d}'" for d in base_dates)
    week_case    = (
        "CASE CAST(p.channel_order_time AS DATE) " +
        " ".join(f"WHEN '{d}' THEN {i+1}" for i, d in enumerate(base_dates)) +
        " END"
    )

    # ── Load config ──
    cfg      = load_config()
    cc_emails = get_cc_emails(cfg)

    print(f"Surge Watchdog running at {run_time_str} IST")
    print(f"CC Emails: {cc_emails or 'not set'}")
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
    # SECTION 1 — SPIKE / DOWN
    # ─────────────────────────────────────────
    current_s1 = {}

    for slot_start, slot_end in slots:
        label  = slot_label(slot_start, slot_end)
        time_s = slot_start.strftime("%H:%M:%S")
        time_e = slot_end.strftime("%H:%M:%S")

        # Today orders
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

        # Baseline
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

        # Articles
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

        # Spike / Down
        for pname in set(CHANNEL_MAP.values()):
            tod       = plat_today.get(pname, 0.0)
            base, wks = smart_baseline(plat_base.get(pname, [0.0] * 8))
            t         = get_platform_thresholds(cfg, pname)

            if wks < 3 and base < 5: continue
            if tod == 0 and base == 0: continue

            issues = set()
            if base > 0 and tod >= t["spike_min_orders"] and (tod / base) >= t["spike_ratio"]:
                issues.add("Spike")
            if base > 0 and tod > 0 and (tod / base) <= t["down_ratio"]:
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
                "platform":      pname,
                "slot":          label,
                "issue":         ", ".join(sorted(issues)),
                "orders":        tod,
                "baseline":      base,
                "ratio":         ratio,
                "articles":      top_arts,
                "first_flagged": time_now,
            }

    # ─────────────────────────────────────────
    # SECTION 2 — HIGH DISCOUNT
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
        if art is None: continue
        pname = CHANNEL_MAP.get(ch, ch)
        t = get_platform_thresholds(cfg, pname)
        if ords < t["discount_min_orders"] or disc < t["discount_min_pct"]: continue
        key = f"{pname}|||{art}"
        if key not in current_disc:
            current_disc[key] = {"platform": pname, "article": art, "orders": 0.0, "discount": 0.0}
        current_disc[key]["orders"]   += ords
        current_disc[key]["discount"]  = max(current_disc[key]["discount"], disc)

    conn.close()

    # ─────────────────────────────────────────
    # FLAGS
    # ─────────────────────────────────────────
    flags     = load_flags()
    past_s1   = flags.get("platform_slots", {})
    past_disc = flags.get("articles", {})

    new_s1, old_s1 = {}, {}
    for key_tuple, row in current_s1.items():
        str_key = f"{key_tuple[0]}|||{key_tuple[1]}"
        if str_key in past_s1:
            entry = dict(past_s1[str_key])
            entry["orders"]   = row["orders"]
            entry["articles"] = row["articles"]
            old_s1[str_key]   = entry
        else:
            new_s1[str_key] = row

    new_disc, old_disc = {}, {}
    for key, data in current_disc.items():
        if key in past_disc:
            entry = dict(past_disc[key])
            entry["current_orders"] = data["orders"]
            entry["discount"]       = data["discount"]
            old_disc[key]           = entry
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
    # SEND OR SKIP
    # ─────────────────────────────────────────
    has_new = bool(new_s1) or bool(new_disc)

    if not has_new:
        print("No new issues. No email sent.")
        for k, v in old_s1.items():
            if k in past_s1:
                past_s1[k]["orders"]   = v["orders"]
                past_s1[k]["articles"] = v["articles"]
        for k, v in old_disc.items():
            if k in past_disc:
                past_disc[k]["current_orders"] = v["current_orders"]
        save_flags(flags)
        return False, {}

    # Save flags
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
    # EMAIL PER PLATFORM
    # ─────────────────────────────────────────
    affected_platforms = set()
    for v in new_s1.values():   affected_platforms.add(v["platform"])
    for v in new_disc.values(): affected_platforms.add(v["platform"])

    emails_sent = {}

    for platform in sorted(affected_platforms):
        to_emails, cc_list = get_platform_emails(cfg, platform)

        if not to_emails:
            print(f"WARNING: No email set for {platform} and cc_all is empty. Skipping.")
            continue

        body    = build_platform_email(
            platform, new_s1, old_s1,
            new_disc, old_disc, run_time_str
        )
        subject = f"PUMA ECOM ALERT — {platform} | {run_time_str} IST"

        send_email(to_emails, cc_list, subject, body)
        emails_sent[platform] = to_emails

        with open(f"surge_report_{platform}.txt", "w") as f:
            f.write(body)

    print(f"\nSummary:")
    print(f"  New spike/down : {len(new_s1)}")
    print(f"  New discounts  : {len(new_disc)}")
    print(f"  Emails sent    : {len(emails_sent)}")
    for p, e in emails_sent.items():
        print(f"    {p} → {', '.join(e)}")

    return True, emails_sent

# ─────────────────────────────────────────────
# DAILY SUMMARY — all 30-min slots midnight to now
# ─────────────────────────────────────────────
def print_daily_summary():
    now        = now_ist()
    today_str  = now.strftime("%Y-%m-%d")
    base_dates = get_baseline_dates()
    base_in    = ",".join(f"'{d}'" for d in base_dates)
    week_case  = (
        "CASE CAST(p.channel_order_time AS DATE) " +
        " ".join(f"WHEN '{d}' THEN {i+1}" for i, d in enumerate(base_dates)) +
        " END"
    )
    cfg = load_config()

    # Build all 30-min slots from 00:00 to now
    slots = []
    cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)
    summary_end = now.replace(second=0, microsecond=0)
    summary_end = summary_end.replace(minute=0 if summary_end.minute < 30 else 30)
    while cursor < summary_end:
        s = cursor
        e = cursor + timedelta(minutes=30)
        slots.append((s, e))
        cursor = e

    if not slots:
        print("No complete slots yet today.")
        return

    conn = get_connection()
    cur  = conn.cursor()

    base_cond = (
        "p.order_status NOT IN ('cancelled','unfulfillable') "
        "AND p.order_type='SALES' "
        "AND (p.order_qty - p.cancelled_qty) > 0 "
        f"AND {CHANNEL_FILTER}"
    )

    platforms = sorted(set(CHANNEL_MAP.values()))

    # Collect results: {platform: {slot_label: {orders, baseline, ratio}}}
    results = {p: {} for p in platforms}

    for slot_start, slot_end in slots:
        label  = slot_label(slot_start, slot_end)
        time_s = slot_start.strftime("%H:%M:%S")
        time_e = slot_end.strftime("%H:%M:%S")

        # Today orders
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

        # Baseline
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

        # Aggregate to platforms
        plat_today, plat_base = {}, {}
        for ch, pname in CHANNEL_MAP.items():
            plat_today[pname] = plat_today.get(pname, 0.0) + today_by_ch.get(ch, 0.0)
            b = base_by_ch.get(ch, [0.0] * 8)
            if pname not in plat_base: plat_base[pname] = [0.0] * 8
            for i in range(8): plat_base[pname][i] += b[i]

        for pname in platforms:
            tod         = plat_today.get(pname, 0.0)
            base, wks   = smart_baseline(plat_base.get(pname, [0.0] * 8))
            ratio       = (tod / base) if base > 0 else None
            results[pname][label] = {
                "orders":   tod,
                "baseline": base,
                "ratio":    ratio,
                "wks":      wks,
            }

    conn.close()

    slot_labels = [slot_label(s, e) for s, e in slots]

    # ── Print ──
    print("\n" + "=" * 120)
    print(f"  DAILY SUMMARY — {today_str}  |  All 30-min slots midnight → now  |  Generated {time_label(now)} IST")
    print("=" * 120)

    COL_W = 16  # width per slot column

    for pname in platforms:
        print(f"\n{'─'*120}")
        print(f"  {pname}")
        print(f"{'─'*120}")

        # Header row
        hdr = f"  {'Metric':<12}"
        for lbl in slot_labels:
            hdr += f"  {lbl:>{COL_W}}"
        print(hdr)
        print(f"  {'-'*12}" + (f"  {'-'*COL_W}" * len(slot_labels)))

        # Orders row
        row_o = f"  {'Orders':<12}"
        for lbl in slot_labels:
            v = results[pname][lbl]["orders"]
            row_o += f"  {int(v):>{COL_W},}"
        print(row_o)

        # Baseline row
        row_b = f"  {'Baseline':<12}"
        for lbl in slot_labels:
            v = results[pname][lbl]["baseline"]
            row_b += f"  {int(v):>{COL_W},}"
        print(row_b)

        # Ratio row  (flag spike/down inline)
        row_r = f"  {'Ratio':<12}"
        for lbl in slot_labels:
            d = results[pname][lbl]
            if d["ratio"] is None or d["wks"] < 3:
                cell = "  -"
            else:
                r    = d["ratio"]
                flag = " ▲" if r >= 1.5 else (" ▼" if r <= 0.5 else "  ")
                cell = f"{r:>6.2f}x{flag}"
            row_r += f"  {cell:>{COL_W}}"
        print(row_r)

    print("\n" + "=" * 120)
    print("  ▲ = Spike (ratio ≥ 1.5)   ▼ = Down (ratio ≤ 0.5)   - = insufficient baseline history")
    print("=" * 120 + "\n")

if __name__ == "__main__":
    print_daily_summary()          # ← add this line
    found, emails = run()
    if found:
        print("\nDone — emails dispatched.")
    else:
        print("\nAll clear — no email sent.")
