#!/usr/bin/env python3
"""
LCS vs CRM Training_Modules reconciliation.

Reads from environment variables, fetches live data via REST APIs,
prints a report to stdout, and emails via SendGrid if SENDGRID_API_KEY is set.

Matching priority:
  1. S-number + module_type  (e.g. S23 + srfnd)
  2. module_type + start_date ±2 days
"""
import json, re, os, sys
from datetime import datetime, timedelta
from collections import defaultdict
import requests

# ── Config ──────────────────────────────────────────────────────────────────────
SPREADSHEET_ID = '109j8qpzauXZUJ32vm8FA7y6PZlLRkzHIjl2wV-TYVRM'
CUTOFF = datetime(2026, 1, 1)

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


def find_in_crm(lcs_module_name, lcs_date_str, by_series, by_date):
    mtype = lcs_module_type(lcs_module_name)
    s_num = lcs_s_number(lcs_module_name)
    if s_num:
        hits = by_series.get((mtype, s_num), [])
        if hits:
            return hits, f'series:{mtype}/{s_num}'
    hits = by_date.get((mtype, lcs_date_str), [])
    if hits:
        return hits, f'date:{mtype}/{lcs_date_str}'
    return [], None


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

        tms, method = find_in_crm(module_name, date_str, by_series, by_date)
        if not tms:
            missing.append({
                'row': i + 1, 'module': module_name, 'type': mtype,
                'date': date_str, 'date_raw': date_raw,
                'host': host, 'host_rep': host_rep,
                'status': status, 'crm_link': crm_link, 'tc': tc_col,
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

    return checked, missing, tc_mismatch


# ── Report ──────────────────────────────────────────────────────────────────────
def build_report_html(checked, missing, tc_mismatch, run_date):
    td = 'style="padding:4px 8px;border:1px solid #ccc"'
    th = 'style="padding:4px 8px;border:1px solid #ccc;background:#f0f0f0"'

    parts = [
        f'<h2>LCS Reconciliation Report — {run_date}</h2>',
        f'<p>LCS rows checked (2026+, active, non-admin): <strong>{checked}</strong></p>',
        f'<h3>Missing from CRM ({len(missing)} entries)</h3>',
    ]

    if missing:
        parts.append(f'<table style="border-collapse:collapse;font-family:monospace;font-size:12px">')
        parts.append(f'<tr><th {th}>Row</th><th {th}>Module</th><th {th}>Date</th>'
                     f'<th {th}>Host</th><th {th}>Host Rep</th><th {th}>LCS CRM Link</th></tr>')
        for m in sorted(missing, key=lambda x: x['date']):
            link = f'<a href="{m["crm_link"]}">{m["crm_link"][:60]}</a>' if m['crm_link'] else ''
            parts.append(f'<tr><td {td}>{m["row"]}</td><td {td}>{m["module"]}</td>'
                         f'<td {td}>{m["date"]}</td><td {td}>{m["host"]}</td>'
                         f'<td {td}>{m["host_rep"]}</td><td {td}>{link}</td></tr>')
        parts.append('</table>')
    else:
        parts.append('<p style="color:green"><strong>All LCS entries found in CRM.</strong></p>')

    if tc_mismatch:
        parts.append(f'<h3>TC Link Mismatches ({len(tc_mismatch)} entries — TM found but TC link differs)</h3>')
        parts.append(f'<table style="border-collapse:collapse;font-family:monospace;font-size:12px">')
        parts.append(f'<tr><th {th}>Row</th><th {th}>Module</th><th {th}>Date</th>'
                     f'<th {th}>Host</th><th {th}>Match method</th>'
                     f'<th {th}>LCS TC ID</th><th {th}>CRM TC ID(s)</th></tr>')
        for m in sorted(tc_mismatch, key=lambda x: x['date']):
            parts.append(f'<tr><td {td}>{m["row"]}</td><td {td}>{m["module"]}</td>'
                         f'<td {td}>{m["date"]}</td><td {td}>{m["host"]}</td>'
                         f'<td {td}>{m["method"]}</td><td {td}>{m["lcs_tc_id"]}</td>'
                         f'<td {td}>{", ".join(m["crm_tc_ids"])}</td></tr>')
        parts.append('</table>')

    return '\n'.join(parts)


def print_report(checked, missing, tc_mismatch):
    print(f"LCS rows checked (2026+, active, non-admin): {checked}")
    print(f"\n{'='*70}")
    print(f"MISSING FROM CRM ({len(missing)} entries)")
    print(f"{'='*70}")
    for m in sorted(missing, key=lambda x: x['date']):
        rep  = f'  [{m["host_rep"]}]' if m['host_rep'] else ''
        link = f'  [CRM link: {m["crm_link"][:60]}]' if m['crm_link'] else ''
        print(f"  Row {m['row']:<5} {m['module']:<25} {m['date']}  {m['host']}{rep}{link}")
    if tc_mismatch:
        print(f"\n{'='*70}")
        print(f"TC LINK MISMATCHES ({len(tc_mismatch)} entries)")
        print(f"{'='*70}")
        for m in sorted(tc_mismatch, key=lambda x: x['date']):
            print(f"  Row {m['row']:<5} {m['module']:<25} {m['date']}  {m['host']}")
            print(f"         Match method : {m['method']}")
            print(f"         LCS TC ID    : {m['lcs_tc_id']}")
            print(f"         CRM TM TC(s) : {', '.join(m['crm_tc_ids'])}")
            print(f"         CRM TM name  : {'; '.join(m['crm_tms'])}")


def send_email(subject, html_body):
    api_key  = os.environ.get('SENDGRID_API_KEY', '')
    to_email = os.environ.get('REPORT_EMAIL', 'chris@imaa.world')
    if not api_key:
        print("\n(No SENDGRID_API_KEY set — report logged above, no email sent.)")
        return
    resp = requests.post(
        'https://api.sendgrid.com/v3/mail/send',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={
            'personalizations': [{'to': [{'email': to_email}]}],
            'from': {'email': 'noreply@imaa.world', 'name': 'LCS Recon'},
            'subject': subject,
            'content': [{'type': 'text/html', 'value': html_body}],
        }
    )
    if resp.status_code == 202:
        print(f"Report emailed to {to_email}")
    else:
        print(f"SendGrid error {resp.status_code}: {resp.text}", file=sys.stderr)


# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    run_date = datetime.utcnow().strftime('%Y-%m-%d')
    print(f"=== LCS Reconciliation {run_date} ===\n")

    print("Fetching Zoho access token...")
    zoho_token = get_zoho_token()

    print("Fetching CRM Training_Modules (2026+)...")
    crm_records = fetch_crm_training_modules(zoho_token)
    print(f"  Loaded {len(crm_records)} records")

    print("Fetching LCS Google Sheet...")
    google_token = get_google_token()
    lcs_rows = fetch_lcs_sheet(google_token)
    print(f"  Loaded {len(lcs_rows)} rows\n")

    checked, missing, tc_mismatch = reconcile(lcs_rows, crm_records)
    print_report(checked, missing, tc_mismatch)

    n = len(missing)
    subject = f"LCS Recon {run_date} — {n} missing TM{'s' if n != 1 else ''}"
    html    = build_report_html(checked, missing, tc_mismatch, run_date)
    send_email(subject, html)


if __name__ == '__main__':
    main()
