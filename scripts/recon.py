#!/usr/bin/env python3
"""
LCS vs CRM Training_Modules reconciliation.

Reads from environment variables, fetches live data via REST APIs,
and prints a report to stdout.

Matching priority:
  1. S-number + module_type  (e.g. S23 + srfnd)
  2. module_type + start_date ±2 days
"""
import json, re, os, html, argparse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import requests

# ── Config ──────────────────────────────────────────────────────────────────────
SPREADSHEET_ID = '109j8qpzauXZUJ32vm8FA7y6PZlLRkzHIjl2wV-TYVRM'
CUTOFF = datetime(2026, 1, 1)

CRM_ORG     = '688920719'                 # Zoho CRM org id (for record links)
TC_TAB      = 'CustomModule4'             # Training_Course tab segment in CRM URLs
REPORT_HTML = os.environ.get('REPORT_HTML', 'report.html')

HERENOW_API  = 'https://here.now/api/v1'
HERENOW_CRED = os.path.expanduser('~/.herenow/credentials')
HERENOW_SLUG = os.environ.get('HERENOW_SLUG_FILE', '.herenow-slug')  # remembers the fixed site

COL_MODULE   = 2
COL_DATE     = 3
COL_HOST     = 4
COL_STATUS   = 10
COL_HOST_REP = 16
COL_CRM_LINK = 19
COL_TC       = 21

ADMIN_TYPES = {
    'ar','exam','workshop','spinefitter','gyrotonic','gyrokinesis',
    'infinity','japan','cec','examresit','exams',
    'matcom1','matcom2','matadv','matint',
    'adfee','eit','gw',
    'sexam1','sexam2','sexam3','sexam4','sexam5','sexam6',
    'sexam1resit','sexam2resit','sexam3resit','sexam4resit','sexam5resit','sexam6resit',
    'matexam1','matexam2','matexam3','matexam4',
    'matexam1resit','matexam2resit','matexam3resit','matexam4resit',
    'rehabexam1','rehabexam2','rehabexam3','rehabexam4','rehabexam5','rehabexam6',
    'rehabexam1resit','rehabexam2resit','rehabexam3resit','rehabexam4resit','rehabexam5resit','rehabexam6resit',
    'refexam1','refexam2','refexam3','refexam4',
    'refexam1resit','refexam2resit','refexam3resit','refexam4resit',
    'sr1','sr2','sr3','sr4','sr5','sr6',
    'cec2','cec3','cec8','aonlsa','ref3','refcom1','refcom2',
    'pspine','pspineeq','cts1','cts2',
    'pponl',   # deferred
}

ALIASES = {
    'sint':     'srint',
    'sadv1':    'sradv1',
    'sadv2':    'sradv2',
    'sfnd':     'srfnd',
    'gonline':  'gonl',
    'pponline': 'pponl',
    'pp':       'pponl',
}

STATUS_SKIP   = ('cancel', 'cxl', 'postpone', 'postponed')
SKIP_PREFIXES = {'gyrotonic', 'gyrokinesis', 'infinity', 'eit'}


# ── Module type helpers ─────────────────────────────────────────────────────────
def lcs_module_type(module_name):
    raw = module_name.strip().split('-')[0].lower()
    raw = re.sub(r'(\d+)[ab]$', r'\1', raw)
    raw = re.sub(r'(0[1-9]|1[0-2])\d{2}$', '', raw)
    return ALIASES.get(raw) or raw


def should_skip_type(mtype, module_name):
    if mtype in ADMIN_TYPES:
        return True
    # Multi-word admin names like "CEC Workshop" / "CEC Pre and Post natal"
    # normalize to "cec workshop" etc.; skip when the leading word is admin.
    if mtype.split() and mtype.split()[0] in ADMIN_TYPES:
        return True
    mname_lower = module_name.lower()
    for prefix in SKIP_PREFIXES:
        if mtype.startswith(prefix) or mname_lower.startswith(prefix):
            return True
    return False


def lcs_s_number(module_name):
    for part in reversed(module_name.strip().split('-')):
        if re.match(r'^S\d+[A-Z]?$', part, re.IGNORECASE):
            return part.upper()
    return None


def crm_s_number(tm_name):
    m = re.search(r'/\s*(S\d+[A-Z]?)\s*/', tm_name, re.IGNORECASE)
    return m.group(1).upper() if m else None


def parse_date(s):
    s = s.strip()
    if not s or s.lower() in ('na', 'tbc', 'n/a', '-', ''):
        return None
    s2 = re.sub(r'^\d+/', '', s)
    s3 = re.sub(r'^\d+-', '', s)
    for candidate in [s, s2, s3]:
        candidate = candidate.strip()
        for fmt in ('%d %b %y', '%d %b %Y', '%d/%m/%Y', '%d/%m/%y',
                    '%Y-%m-%d', '%d %B %Y', '%d %B %y'):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                pass
    return None


def extract_tc_id(url):
    if not url:
        return None
    m = re.search(r'/(\d{15,20})/?$', url.strip())
    return m.group(1) if m else None


# ── CRM lookup builders ─────────────────────────────────────────────────────────
def build_crm_lookups(crm_records):
    by_series = defaultdict(list)
    by_date   = defaultdict(list)
    for tm in crm_records:
        mtype  = tm['module_name'].lower().strip()
        s_num  = crm_s_number(tm['name'])
        start  = tm['start_date']
        if s_num:
            by_series[(mtype, s_num)].append(tm)
        if start:
            try:
                d = datetime.strptime(start, '%Y-%m-%d')
                for delta in range(-2, 3):
                    key = (mtype, (d + timedelta(days=delta)).strftime('%Y-%m-%d'))
                    by_date[key].append(tm)
            except ValueError:
                pass
    return by_series, by_date


DATE_WINDOW = 2  # ± days a CRM run may differ from the LCS date and still match


def tm_date(tm):
    """Parsed start date for a CRM TM, cached on the record."""
    if 'parsed_date' not in tm:
        try:
            tm['parsed_date'] = datetime.strptime(tm['start_date'], '%Y-%m-%d')
        except (ValueError, KeyError):
            tm['parsed_date'] = None
    return tm['parsed_date']


def find_in_crm(lcs_module_name, lcs_date, by_series, by_date):
    """Resolve an LCS row to CRM TM(s).

    Returns (tms, method). Methods, strongest first:
      series+date  exact series, run within ±DATE_WINDOW days  → trust for TC compare
      date         no S-number, matched by module type + date
      series-only  series exists in CRM but no run near this date (schedule differs)
    """
    mtype = lcs_module_type(lcs_module_name)
    s_num = lcs_s_number(lcs_module_name)
    lcs_date_str = lcs_date.strftime('%Y-%m-%d')

    if s_num:
        series_hits = by_series.get((mtype, s_num), [])
        if series_hits:
            near = [tm for tm in series_hits
                    if tm_date(tm) and abs((tm_date(tm) - lcs_date).days) <= DATE_WINDOW]
            if near:
                return near, f'series+date:{mtype}/{s_num}'
            return series_hits, f'series-only:{mtype}/{s_num}'

    hits = by_date.get((mtype, lcs_date_str), [])
    if hits:
        return hits, f'date:{mtype}/{lcs_date_str}'
    return [], None


def find_duplicate_tms(crm_records):
    """CRM TMs sharing a TM-number — a data-integrity problem in CRM itself.

    Only records carrying a real TM-#### identifier are candidates. Records
    without one (e.g. CRAF) are each legitimately tied to their own Training_Course,
    so identical CRAF names across different TCs are not duplicates.
    """
    by_number = defaultdict(list)
    for tm in crm_records:
        m = re.match(r'\s*(TM-\d+)', tm['name'])
        if not m:
            continue
        by_number[m.group(1)].append(tm)
    dups = []
    for num, recs in by_number.items():
        if len(recs) > 1:
            dups.append({
                'tm_number': num,
                'count':     len(recs),
                'names':     [r['name'] for r in recs],
                'tc_ids':    sorted({r['tc_id'] for r in recs if r.get('tc_id')}),
                'records':   [{'id': r['id'], 'name': r['name'],
                               'tc_id': r.get('tc_id', ''), 'date': r.get('start_date', '')}
                              for r in recs],
            })
    return sorted(dups, key=lambda x: x['tm_number'])


# ── Zoho CRM ────────────────────────────────────────────────────────────────────
def get_zoho_token():
    resp = requests.post('https://accounts.zoho.com/oauth/v2/token', data={
        'grant_type':    'refresh_token',
        'client_id':     os.environ['ZOHO_CLIENT_ID'],
        'client_secret': os.environ['ZOHO_CLIENT_SECRET'],
        'refresh_token': os.environ['ZOHO_REFRESH_TOKEN'],
    })
    resp.raise_for_status()
    return resp.json()['access_token']


def fetch_crm_training_modules(token):
    records = []
    page    = 1
    headers = {'Authorization': f'Zoho-oauthtoken {token}'}
    while True:
        resp = requests.get(
            'https://www.zohoapis.com/crm/v3/Training_Modules/search',
            headers=headers,
            params={
                'criteria': '(Module_Start_Date:greater_equal:2026-01-01)',
                'fields':   'Name,Module_Name,Module_Start_Date,Host,Training_Course',
                'page':     page,
                'per_page': 200,
            }
        )
        if resp.status_code == 204:
            break
        resp.raise_for_status()
        data  = resp.json()
        batch = data.get('data', [])
        if not batch:
            break
        for r in batch:
            records.append({
                'id':          r['id'],
                'name':        r.get('Name', ''),
                'module_name': r.get('Module_Name', '') or '',
                'start_date':  r.get('Module_Start_Date', '') or '',
                'host_name':   (r.get('Host') or {}).get('name', ''),
                'tc_id':       (r.get('Training_Course') or {}).get('id', ''),
                'tc_name':     (r.get('Training_Course') or {}).get('name', ''),
            })
        if not data.get('info', {}).get('more_records'):
            break
        page += 1
    return records


# ── Google Sheets ───────────────────────────────────────────────────────────────
def get_google_token():
    from google.oauth2 import service_account
    import google.auth.transport.requests as ga_requests
    sa_info = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'],
    )
    creds.refresh(ga_requests.Request())
    return creds.token


def fetch_lcs_sheet(token):
    resp = requests.get(
        f'https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/Courses',
        headers={'Authorization': f'Bearer {token}'},
        params={'majorDimension': 'ROWS'},
    )
    resp.raise_for_status()
    return resp.json().get('values', [])


# ── Reconciliation ──────────────────────────────────────────────────────────────
def reconcile(lcs_rows, crm_records):
    by_series, by_date = build_crm_lookups(crm_records)
    missing     = []
    date_diff   = []   # series in CRM, but no run within ±DATE_WINDOW of the LCS date
    tc_mismatch = []
    checked     = 0

    for i, row in enumerate(lcs_rows):
        if i < 3:
            continue
        if len(row) <= max(COL_MODULE, COL_DATE, COL_STATUS):
            continue
        module_name = row[COL_MODULE].strip() if len(row) > COL_MODULE else ''
        if not module_name:
            continue
        mtype = lcs_module_type(module_name)
        if should_skip_type(mtype, module_name):
            continue
        status = row[COL_STATUS].strip() if len(row) > COL_STATUS else ''
        if any(x in status.lower() for x in STATUS_SKIP):
            continue
        date_raw = row[COL_DATE].strip() if len(row) > COL_DATE else ''
        date = parse_date(date_raw)
        if date is None or date < CUTOFF:
            continue

        host     = row[COL_HOST].strip()     if len(row) > COL_HOST     else ''
        host_rep = row[COL_HOST_REP].strip() if len(row) > COL_HOST_REP else ''
        crm_link = row[COL_CRM_LINK].strip() if len(row) > COL_CRM_LINK else ''
        tc_col   = row[COL_TC].strip()       if len(row) > COL_TC       else ''
        date_str = date.strftime('%Y-%m-%d')
        checked += 1

        tms, method = find_in_crm(module_name, date, by_series, by_date)
        if not tms:
            missing.append({
                'row': i + 1, 'module': module_name, 'type': mtype,
                'date': date_str, 'date_raw': date_raw,
                'host': host, 'host_rep': host_rep,
                'status': status, 'crm_link': crm_link, 'tc': tc_col,
            })
        elif method.startswith('series-only'):
            # Series exists but no run near this date — likely a schedule
            # difference, not a TC problem. Don't compare TC links here.
            date_diff.append({
                'row': i + 1, 'module': module_name, 'date': date_str,
                'host': host, 'host_rep': host_rep,
                'crm_dates': sorted(tm['start_date'] for tm in tms if tm.get('start_date')),
            })
        else:
            lcs_tc_id = extract_tc_id(crm_link)
            if lcs_tc_id:
                crm_tc_ids = {tm['tc_id'] for tm in tms if tm.get('tc_id')}
                if lcs_tc_id not in crm_tc_ids:
                    tc_mismatch.append({
                        'row': i + 1, 'module': module_name, 'date': date_str,
                        'host': host, 'host_rep': host_rep, 'method': method,
                        'lcs_tc_id': lcs_tc_id, 'crm_tc_ids': list(crm_tc_ids),
                        'crm_tms': [tm['name'] for tm in tms],
                    })

    return checked, missing, date_diff, tc_mismatch


# ── Report ──────────────────────────────────────────────────────────────────────
def print_report(checked, missing, date_diff, tc_mismatch, duplicates):
    print(f"LCS rows checked (2026+, active, non-admin): {checked}")

    print(f"\n{'='*70}")
    print(f"MISSING FROM CRM ({len(missing)} entries)")
    print(f"{'='*70}")
    for m in sorted(missing, key=lambda x: x['date']):
        rep  = f'  [{m["host_rep"]}]' if m['host_rep'] else ''
        link = f'  [CRM link: {m["crm_link"][:60]}]' if m['crm_link'] else ''
        print(f"  Row {m['row']:<5} {m['module']:<25} {m['date']}  {m['host']}{rep}{link}")

    print(f"\n{'='*70}")
    print(f"DUPLICATE TMs IN CRM ({len(duplicates)} TM numbers with >1 record)")
    print(f"{'='*70}")
    for d in duplicates:
        flag = '  <-- different TC links' if len(d['tc_ids']) > 1 else ''
        print(f"  {d['tm_number']}  x{d['count']}{flag}")
        for n in d['names']:
            print(f"         {n}")
        if len(d['tc_ids']) > 1:
            print(f"         TC ids: {', '.join(d['tc_ids'])}")

    if tc_mismatch:
        print(f"\n{'='*70}")
        print(f"TC LINK MISMATCHES ({len(tc_mismatch)} entries — date-confirmed match, TC differs)")
        print(f"{'='*70}")
        for m in sorted(tc_mismatch, key=lambda x: x['date']):
            print(f"  Row {m['row']:<5} {m['module']:<25} {m['date']}  {m['host']}")
            print(f"         Match method : {m['method']}")
            print(f"         LCS TC ID    : {m['lcs_tc_id']}")
            print(f"         CRM TM TC(s) : {', '.join(m['crm_tc_ids'])}")
            print(f"         CRM TM name  : {'; '.join(m['crm_tms'])}")

    if date_diff:
        print(f"\n{'='*70}")
        print(f"SERIES FOUND, DATE DIFFERS ({len(date_diff)} entries — series in CRM, no run within ±{DATE_WINDOW}d)")
        print(f"{'='*70}")
        for m in sorted(date_diff, key=lambda x: x['date']):
            print(f"  Row {m['row']:<5} {m['module']:<25} {m['date']}  {m['host']}")
            print(f"         CRM run dates: {', '.join(m['crm_dates'])}")


# ── HTML report ───────────────────────────────────────────────────────────────────
def tc_link(tc_id):
    if not tc_id:
        return '<span class="muted">—</span>'
    url = f'https://crm.zoho.com/crm/org{CRM_ORG}/tab/{TC_TAB}/{tc_id}'
    return f'<a href="{url}" target="_blank">{tc_id[-7:]}</a>'


def build_report_html(checked, missing, date_diff, tc_mismatch, duplicates, run_date, generated_at):
    esc = html.escape
    dup_conflict = [d for d in duplicates if len(d['tc_ids']) > 1]

    def card(n, label, cls):
        return f'<div class="card {cls}"><div class="num">{n}</div><div class="lbl">{label}</div></div>'

    out = [f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LCS Reconciliation — {run_date}</title>
<style>
  :root {{ --red:#c0392b; --orange:#d35400; --amber:#b8860b; --blue:#2c6fbb; --line:#e1e4e8; }}
  body {{ font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; color:#24292e; margin:0; background:#fafbfc; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:24px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#586069; margin:0 0 20px; }}
  .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:28px; }}
  .card {{ flex:1; min-width:120px; border:1px solid var(--line); border-radius:8px; padding:14px 16px; background:#fff; border-left:4px solid #ccc; }}
  .card .num {{ font-size:28px; font-weight:700; }}
  .card .lbl {{ color:#586069; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
  .card.red {{ border-left-color:var(--red); }} .card.orange {{ border-left-color:var(--orange); }}
  .card.amber {{ border-left-color:var(--amber); }} .card.blue {{ border-left-color:var(--blue); }}
  section {{ background:#fff; border:1px solid var(--line); border-radius:8px; margin-bottom:24px; overflow:hidden; }}
  section > h2 {{ font-size:15px; margin:0; padding:12px 16px; border-bottom:1px solid var(--line); border-left:4px solid #ccc; }}
  section.red > h2 {{ border-left-color:var(--red); }} section.orange > h2 {{ border-left-color:var(--orange); }}
  section.amber > h2 {{ border-left-color:var(--amber); }} section.blue > h2 {{ border-left-color:var(--blue); }}
  section .desc {{ color:#586069; padding:4px 16px 0; font-size:13px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ text-align:left; padding:7px 16px; border-top:1px solid var(--line); vertical-align:top; }}
  th {{ background:#f6f8fa; font-weight:600; color:#586069; }}
  td.mono, .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }}
  tr.flag {{ background:#fff8f0; }}
  .pill {{ display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px; font-weight:600; }}
  .pill.bad {{ background:#fde8e6; color:var(--red); }}
  .diff {{ color:var(--red); font-weight:600; }}
  .muted {{ color:#959da5; }}
  .empty {{ padding:14px 16px; color:#22863a; }}
  a {{ color:var(--blue); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
</style></head><body><div class="wrap">
<h1>LCS Reconciliation Report</h1>
<p class="sub">LCS &ldquo;Courses&rdquo; sheet vs Zoho CRM Training_Modules (2026+) · {checked} rows checked</p>
<p class="sub"><strong>Last updated:</strong> {generated_at}</p>
<div class="cards">
  {card(len(missing), 'Missing from CRM', 'red')}
  {card(len(dup_conflict), 'Duplicate TMs (TC conflict)', 'orange')}
  {card(len(tc_mismatch), 'TC link mismatches', 'amber')}
  {card(len(date_diff), 'Series found, date differs', 'blue')}
</div>
"""]

    # Missing
    out.append('<section class="red"><h2>Missing from CRM</h2>')
    out.append('<p class="desc">In the LCS sheet but no matching Training_Module in CRM.</p>')
    if missing:
        out.append('<table><tr><th>Row</th><th>Module</th><th>Date</th><th>Host</th><th>Host rep</th></tr>')
        for m in sorted(missing, key=lambda x: x['date']):
            out.append(f'<tr><td>{m["row"]}</td><td class="mono">{esc(m["module"])}</td>'
                       f'<td class="mono">{m["date"]}</td><td>{esc(m["host"])}</td>'
                       f'<td class="muted">{esc(m["host_rep"])}</td></tr>')
        out.append('</table>')
    else:
        out.append('<p class="empty">None — all checked rows found in CRM.</p>')
    out.append('</section>')

    # Duplicate TMs
    out.append('<section class="orange"><h2>Duplicate Training_Modules in CRM</h2>')
    out.append(f'<p class="desc">Same TM identifier on more than one CRM record. '
               f'<strong>{len(dup_conflict)}</strong> of {len(duplicates)} point at <em>different</em> Training_Courses (highlighted) — these corrupt matching.</p>')
    if duplicates:
        out.append('<table><tr><th>TM</th><th>#</th><th>Records (date / host)</th><th>Training_Course link(s)</th></tr>')
        for d in sorted(duplicates, key=lambda x: (len(x['tc_ids']) < 2, x['tm_number'])):
            conflict = len(d['tc_ids']) > 1
            rows = '<br>'.join(esc(r['name']) for r in d['records'])
            tcs  = ' '.join(tc_link(t) for t in d['tc_ids']) or '<span class="muted">—</span>'
            badge = ' <span class="pill bad">conflict</span>' if conflict else ''
            out.append(f'<tr class="{ "flag" if conflict else "" }">'
                       f'<td class="mono">{esc(d["tm_number"])}{badge}</td>'
                       f'<td>{d["count"]}</td><td class="mono">{rows}</td>'
                       f'<td class="mono">{tcs}</td></tr>')
        out.append('</table>')
    else:
        out.append('<p class="empty">No duplicate TMs found.</p>')
    out.append('</section>')

    # TC mismatches
    out.append('<section class="amber"><h2>TC Link Mismatches</h2>')
    out.append('<p class="desc">Date-confirmed match, but the sheet&rsquo;s Training_Course link differs from CRM&rsquo;s. '
               'Note: many reflect a per-module vs per-course granularity difference, not a data error.</p>')
    if tc_mismatch:
        out.append('<table><tr><th>Row</th><th>Module</th><th>Date</th><th>Host</th>'
                   '<th>Sheet TC</th><th>CRM TC</th><th>CRM TM</th></tr>')
        for m in sorted(tc_mismatch, key=lambda x: x['date']):
            crm_tc = ' '.join(tc_link(t) for t in m['crm_tc_ids']) or '<span class="muted">—</span>'
            out.append(f'<tr><td>{m["row"]}</td><td class="mono">{esc(m["module"])}</td>'
                       f'<td class="mono">{m["date"]}</td><td>{esc(m["host"])}</td>'
                       f'<td class="mono diff">{tc_link(m["lcs_tc_id"])}</td>'
                       f'<td class="mono">{crm_tc}</td>'
                       f'<td class="mono muted">{esc("; ".join(m["crm_tms"]))}</td></tr>')
        out.append('</table>')
    else:
        out.append('<p class="empty">No TC mismatches.</p>')
    out.append('</section>')

    # Date differs
    out.append('<section class="blue"><h2>Series Found, Date Differs</h2>')
    out.append(f'<p class="desc">Series exists in CRM but no run within &plusmn;{DATE_WINDOW} days of the sheet date '
               '(often reused S-numbers / schedule drift).</p>')
    if date_diff:
        out.append('<table><tr><th>Row</th><th>Module</th><th>Sheet date</th><th>Host</th><th>CRM run dates</th></tr>')
        for m in sorted(date_diff, key=lambda x: x['date']):
            out.append(f'<tr><td>{m["row"]}</td><td class="mono">{esc(m["module"])}</td>'
                       f'<td class="mono">{m["date"]}</td><td>{esc(m["host"])}</td>'
                       f'<td class="mono muted">{esc(", ".join(m["crm_dates"]))}</td></tr>')
        out.append('</table>')
    else:
        out.append('<p class="empty">None.</p>')
    out.append('</section>')

    out.append('</div></body></html>')
    return '\n'.join(out)


# ── Publish to here.now ───────────────────────────────────────────────────────────
def herenow_api_key():
    """API key from $HERENOW_API_KEY or ~/.herenow/credentials (needed for a password)."""
    key = os.environ.get('HERENOW_API_KEY')
    if key:
        return key.strip()
    try:
        with open(HERENOW_CRED) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def publish_to_herenow(html_path, password=None, ttl_seconds=None):
    """Publish the report to a FIXED here.now URL.

    The site slug is remembered in HERENOW_SLUG; subsequent runs PUT a new
    version to that same slug so the dashboard URL never changes. An API key is
    required for a stable, owned, password-protected site.
    """
    data    = open(html_path, 'rb').read()
    api_key = herenow_api_key()
    auth    = {'Authorization': f'Bearer {api_key}'} if api_key else {}

    body = {'files': [{'path': 'index.html', 'size': len(data), 'contentType': 'text/html'}],
            'viewer': {'title': 'LCS Reconciliation Report',
                       'description': 'LCS sheet vs Zoho CRM Training_Modules'}}
    if ttl_seconds:
        body['ttlSeconds'] = ttl_seconds

    try:
        with open(HERENOW_SLUG) as f:
            slug = f.read().strip() or None
    except FileNotFoundError:
        slug = None

    # Update the existing site in place (PUT) when we have a slug + key; else create one.
    r = None
    if slug and api_key:
        r = requests.put(f'{HERENOW_API}/publish/{slug}',
                         headers={'Content-Type': 'application/json', **auth}, json=body)
        if not r.ok:
            print(f"  (stored slug '{slug}' not updatable: HTTP {r.status_code} — creating a new site)")
            r = None
    if r is None:
        r = requests.post(f'{HERENOW_API}/publish',
                          headers={'Content-Type': 'application/json', **auth}, json=body)
    r.raise_for_status()
    j    = r.json()
    slug = j['slug']
    up   = j['upload']
    u0   = up['uploads'][0]

    requests.put(u0['url'], headers=u0.get('headers') or {}, data=data).raise_for_status()
    fr = requests.post(up['finalizeUrl'], headers={'Content-Type': 'application/json', **auth},
                       json={'versionId': up['versionId']})
    fr.raise_for_status()
    site_url = fr.json().get('siteUrl') or j.get('siteUrl')

    # Remember the slug so the URL stays fixed across runs.
    with open(HERENOW_SLUG, 'w') as f:
        f.write(slug)

    if password:
        if not api_key:
            print("  WARNING: a password needs a here.now API key — published WITHOUT a password.")
        else:
            pr = requests.patch(f'{HERENOW_API}/publish/{slug}/metadata',
                                headers={'Content-Type': 'application/json', **auth},
                                json={'password': password})
            pr.raise_for_status()
            print("  password protection enabled")

    print(f"  Live URL : {site_url}")
    if j.get('anonymous'):
        print(f"  (anonymous — expires {j.get('expiresAt')})")
    return site_url


# ── Main ────────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description='LCS vs Zoho CRM reconciliation.')
    p.add_argument('--publish', action='store_true',
                   help='publish the HTML report to here.now')
    p.add_argument('--password', default=os.environ.get('HERENOW_PASSWORD'),
                   help='password-protect the published site (implies --publish; needs a here.now API key)')
    p.add_argument('--ttl', type=int, default=None,
                   help='here.now site lifetime in seconds (anonymous sites default to 24h)')
    return p.parse_args()


def main():
    args = parse_args()
    run_dt       = datetime.now(timezone.utc)
    run_date     = run_dt.strftime('%Y-%m-%d')
    generated_at = run_dt.strftime('%Y-%m-%d %H:%M:%S UTC')
    print(f"=== LCS Reconciliation {generated_at} ===\n")

    print("Fetching Zoho access token...")
    zoho_token = get_zoho_token()

    print("Fetching CRM Training_Modules (2026+)...")
    crm_records = fetch_crm_training_modules(zoho_token)
    print(f"  Loaded {len(crm_records)} records")

    print("Fetching LCS Google Sheet...")
    google_token = get_google_token()
    lcs_rows = fetch_lcs_sheet(google_token)
    print(f"  Loaded {len(lcs_rows)} rows\n")

    duplicates = find_duplicate_tms(crm_records)
    checked, missing, date_diff, tc_mismatch = reconcile(lcs_rows, crm_records)
    print_report(checked, missing, date_diff, tc_mismatch, duplicates)

    html_doc = build_report_html(checked, missing, date_diff, tc_mismatch, duplicates, run_date, generated_at)
    with open(REPORT_HTML, 'w') as f:
        f.write(html_doc)
    print(f"\nHTML report written to {os.path.abspath(REPORT_HTML)}")

    if args.publish or args.password:
        print("\nPublishing to here.now...")
        publish_to_herenow(REPORT_HTML, password=args.password, ttl_seconds=args.ttl)


if __name__ == '__main__':
    main()
