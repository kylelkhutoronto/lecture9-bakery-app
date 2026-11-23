"""Home Bakery: standard-library-only local order tracker."""
import csv
import html
import io
import re
import sqlite3
from datetime import date, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'bakery.db'
START = date(2026, 11, 23)
PRODUCTS = {'cupcakes': (40, 30), 'cake': (120, 120)}


class Messages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.parts = []
        self.messages = []

    def handle_starttag(self, tag, attrs):
        if tag == 'p' and dict(attrs).get('class') == 'message':
            self.capture = True
            self.parts = []

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'p' and self.capture:
            self.messages.append(''.join(self.parts).strip())
            self.capture = False


def parse_pickup(text):
    match = re.search(r'(Nov|Dec)\s+(\d+)', text)
    if not match:
        match = re.search(r'(\d+)\s+(Nov|Dec)', text)
        if not match:
            raise ValueError('Unrecognized pickup date: ' + text)
        day, month = match.groups()
    else:
        month, day = match.groups()
    return date(2026, {'Nov': 11, 'Dec': 12}[month], int(day)).isoformat()


def parse_item(text):
    match = re.fullmatch(r'(\d+)\s+(dozen cupcakes|cakes?)', text.strip(), re.I)
    if not match:
        raise ValueError('Use a whole number of cupcake dozens or cakes.')
    return ('cupcakes' if 'cupcakes' in match[2].lower() else 'cake', int(match[1]))


def connect(path=DB):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,customer TEXT NOT NULL,
      product TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),pickup TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'accepted',source TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,source TEXT UNIQUE NOT NULL,
      received TEXT NOT NULL,customer TEXT NOT NULL,kind TEXT NOT NULL,product TEXT,
      quantity INTEGER,pickup TEXT NOT NULL,raw TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
      decision TEXT,order_id INTEGER REFERENCES orders(id));
    CREATE TABLE IF NOT EXISTS helpers(day TEXT PRIMARY KEY,booked_on TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,day TEXT NOT NULL,detail TEXT NOT NULL);
    ''')
    if db.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
        return
    with db:
        for row in csv.DictReader((ROOT / 'existing_orders.csv').open(encoding='utf-8-sig', newline='')):
            product, qty = parse_item(row['item'])
            db.execute('INSERT INTO orders(id,customer,product,quantity,pickup,source) VALUES(?,?,?,?,?,?)',
                       (int(row['order'].lstrip('#')), row['customer'], product, qty,
                        parse_pickup(row['pickup_day']), 'existing_orders.csv'))
        for offset, weekday in enumerate(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']):
            filename = f'InstagramOrders_{weekday}.html'
            parser = Messages()
            parser.feed((ROOT / filename).read_text(encoding='utf-8-sig'))
            for index, raw in enumerate(parser.messages, 1):
                fields = dict(line.split(':', 1) for line in raw.splitlines() if ':' in line)
                item = fields['Order'].strip()
                kind = 'cancel' if item.lower() == 'cancel' else 'change' if item.lower().startswith('change to ') else 'new'
                product, qty = (None, None) if kind == 'cancel' else parse_item(re.sub(r'^Change to ', '', item, flags=re.I))
                db.execute('''INSERT INTO requests(source,received,customer,kind,product,quantity,pickup,raw)
                  VALUES(?,?,?,?,?,?,?,?)''', (f'{filename} message {index}', (START + timedelta(days=offset)).isoformat(),
                  fields['Name'].strip(), kind, product, qty, parse_pickup(fields['Pickup Date']), raw))
        db.executemany('INSERT INTO settings VALUES(?,?)', [('day', START.isoformat()), ('initialized', '1')])
        log(db, 'Imported 13 accepted orders and 25 Instagram requests. No requests decided automatically.')


def today(db):
    return date.fromisoformat(db.execute("SELECT value FROM settings WHERE key='day'").fetchone()[0])


def log(db, detail):
    db.execute('INSERT INTO audit(day,detail) VALUES(?,?)', (today(db).isoformat(), detail))


def usage(db, day, exclude=None):
    return sum(PRODUCTS[r['product']][1] * r['quantity'] for r in db.execute(
        "SELECT * FROM orders WHERE pickup=? AND status!='cancelled' AND id!=?", (day, exclude or -1)))


def capacity(db, day):
    return 720 if db.execute('SELECT 1 FROM helpers WHERE day=?', (day,)).fetchone() else 480


def assess(db, r):
    if r['received'] > today(db).isoformat():
        return False, 'This message has not arrived yet.', None
    if (date.fromisoformat(r['pickup']) - date.fromisoformat(r['received'])).days < 2:
        return False, 'Too late: the message gives less than 2 calendar days of notice.', None
    if r['pickup'] < today(db).isoformat():
        return False, 'Pickup day has passed.', None
    target = None
    if r['kind'] != 'new':
        matches = db.execute("SELECT * FROM orders WHERE customer=? COLLATE NOCASE AND pickup=? AND status='accepted'",
                             (r['customer'], r['pickup'])).fetchall()
        if len(matches) != 1:
            return False, 'Needs exactly one matching accepted order. Resolve the earlier request first; ready or collected orders cannot change.', None
        target = matches[0]['id']
    if r['kind'] == 'cancel':
        return True, 'Notice met. Applying this cancellation releases its baking time.', target
    needed = usage(db, r['pickup'], target) + r['quantity'] * PRODUCTS[r['product']][1]
    cap = capacity(db, r['pickup'])
    if needed > cap:
        helper_possible = cap == 480 and needed <= 720 and (date.fromisoformat(r['pickup']) - today(db)).days >= 2
        return False, (f'Would use {needed/60:g} of {cap/60:g} hours. ' +
                       ('Book a helper for this day, then reassess.' if helper_possible else 'Not enough daily capacity.')), target
    return True, f'Fits: {needed/60:g} of {cap/60:g} baking hours after approval.', target


def act(db, form):
    action = form.get('action', '')
    # Serialize rule checking and writes so two browser tabs cannot overbook.
    with db:
        db.execute('BEGIN IMMEDIATE')
        current = today(db)
        if action in ('accept', 'reject'):
            r = db.execute('SELECT * FROM requests WHERE id=?', (int(form['id']),)).fetchone()
            if not r or r['status'] != 'pending' or r['received'] > current.isoformat():
                raise ValueError('This request is unavailable or already decided.')
            ok, reason, target = assess(db, r)
            if action == 'accept':
                if not ok:
                    raise ValueError(reason)
                if r['kind'] == 'new':
                    target = db.execute('INSERT INTO orders(customer,product,quantity,pickup,source) VALUES(?,?,?,?,?)',
                        (r['customer'], r['product'], r['quantity'], r['pickup'], r['source'])).lastrowid
                elif r['kind'] == 'cancel':
                    db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (target,))
                else:
                    db.execute('UPDATE orders SET product=?,quantity=? WHERE id=?', (r['product'], r['quantity'], target))
                decision = reason
            else:
                decision = form.get('reason', '').strip()[:500] or ('Baker declined. ' + reason)
            db.execute('UPDATE requests SET status=?,decision=?,order_id=? WHERE id=?',
                       ('approved' if action == 'accept' else 'rejected', decision, target, r['id']))
            log(db, f"Request {r['id']} · {r['customer']} · {r['kind']} · {action}: {decision}")
        elif action == 'helper':
            day = date.fromisoformat(form['day'])
            if (day - current).days < 2:
                raise ValueError('A helper needs at least 2 calendar days of notice.')
            if db.execute('SELECT 1 FROM helpers WHERE day=?', (day.isoformat(),)).fetchone():
                raise ValueError('A helper is already booked for this day.')
            db.execute('INSERT INTO helpers VALUES(?,?)', (day.isoformat(), current.isoformat()))
            log(db, f'Helper booked for {day}: $100, adds 4 hours. Booking is final in this app.')
        elif action == 'advance':
            if db.execute("SELECT 1 FROM requests WHERE status='pending' AND received<=?", (current.isoformat(),)).fetchone():
                raise ValueError('Decide all arrived messages before moving to the next day.')
            db.execute("UPDATE settings SET value=? WHERE key='day'", ((current + timedelta(days=1)).isoformat(),))
            log(db, 'Moved to the next calendar day.')
        elif action == 'request':
            kind = form['kind']
            if kind not in ('new', 'change', 'cancel'):
                raise ValueError('Choose a valid request type.')
            name = form['customer'].strip()
            if not name or len(name) > 100:
                raise ValueError('Enter a customer name of 1 to 100 characters.')
            pickup = date.fromisoformat(form['pickup']).isoformat()
            product = form['product']
            qty = int(form['quantity'])
            if product not in PRODUCTS or not 1 <= qty <= 1000:
                raise ValueError('Choose a product and a whole quantity from 1 to 1000.')
            sequence = db.execute('SELECT coalesce(max(id),0)+1 FROM requests').fetchone()[0]
            raw = f'Name: {name}\nRequest: {kind}\nProduct: {product}\nQuantity: {qty}\nPickup: {pickup}'
            db.execute('INSERT INTO requests(source,received,customer,kind,product,quantity,pickup,raw) VALUES(?,?,?,?,?,?,?,?)',
                       (f'Manual request {sequence}', current.isoformat(), name, kind, product, qty, pickup, raw))
            log(db, f'Manual {kind} request recorded for {name}; awaiting decision.')
        elif action == 'status':
            order = db.execute('SELECT * FROM orders WHERE id=?', (int(form['id']),)).fetchone()
            if not order or order['pickup'] != current.isoformat():
                raise ValueError('Mark orders ready or collected on their pickup day only.')
            transition = {'accepted': 'ready', 'ready': 'collected'}
            if order['status'] not in transition:
                raise ValueError('This order cannot move to the next status.')
            status = transition[order['status']]
            db.execute('UPDATE orders SET status=? WHERE id=?', (status, order['id']))
            log(db, f"Order #{order['id']} · {order['customer']} marked {status}.")
        else:
            raise ValueError('Unknown action.')


def esc(value):
    return html.escape(str(value), quote=True)


def button(action, label, fields=None, disabled=False, secondary=False):
    fields = fields or {}
    return '<form method="post" class="inline"><input type="hidden" name="action" value="' + action + '">' + ''.join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in fields.items()) + f'<button {"disabled" if disabled else ""} class="{"secondary" if secondary else ""}">{esc(label)}</button></form>'


STYLE = '''
:root{--ink:#322923;--muted:#70645a;--line:#e4dbce;--cream:#faf7f0;--green:#315e48;--pink:#f3e3d8}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:15px/1.55 system-ui,sans-serif}header{background:#fffdf9;border-bottom:1px solid var(--line);padding:22px max(24px,calc((100vw - 1280px)/2));display:flex;align-items:center;justify-content:space-between;gap:24px}h1,h2,h3{font-family:Georgia,serif;font-weight:500;line-height:1.2}h1{margin:0;font-size:32px}h2{font-size:27px;margin:0 0 18px}h3{font-size:22px;margin:0 0 10px}p{margin:8px 0}main{max-width:1328px;margin:auto;padding:30px 24px}a{color:var(--green)}nav{display:flex;gap:18px;flex-wrap:wrap}small,.muted{color:var(--muted)}.eyebrow{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--green);font-weight:700}.topline,.sectionhead{display:flex;justify-content:space-between;align-items:center;gap:20px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:24px 0}.stat,.panel,.daycard,.message{background:#fffdf9;border:1px solid var(--line);border-radius:14px;padding:20px}.stat strong{display:block;font:32px Georgia,serif;margin-top:6px}.days{display:grid;grid-template-columns:repeat(7,1fr);gap:10px}.daycard{padding:14px;min-width:0}.daycard.today{border:2px solid var(--green)}.daycard strong{display:block}progress{width:100%;height:9px;accent-color:var(--green)}.columns{display:grid;grid-template-columns:1.5fr 1fr;gap:24px;margin-top:28px}.message{margin-bottom:14px}.message pre{white-space:pre-wrap;font:14px/1.5 system-ui;background:var(--cream);padding:12px;border-radius:8px}.badge{display:inline-block;border-radius:20px;background:#e7efe7;color:#284f39;padding:3px 10px;font-size:12px;font-weight:650}.badge.warn{background:#fae9d5;color:#7c4917}.rule{font-size:14px;margin:12px 0}.inline{display:inline-block;margin:3px 5px 3px 0}button{font:inherit;background:var(--green);color:white;border:1px solid var(--green);border-radius:7px;padding:9px 14px;cursor:pointer}button.secondary{background:white;color:var(--ink);border-color:var(--line)}button:disabled{opacity:.4;cursor:not-allowed}input,select{font:inherit;border:1px solid #bfb3a5;border-radius:6px;padding:9px;background:white;max-width:100%;width:100%}label{display:block;font-size:13px;font-weight:600;margin:12px 0 4px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;padding:12px;border-bottom:1px solid var(--line)}th{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}.tablewrap{overflow:auto}.panel{margin-bottom:22px}.notice{padding:15px;border-radius:8px;background:#fae9d5;border:1px solid #dca767;margin-bottom:20px}.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}summary{cursor:pointer;font-weight:600}section{scroll-margin-top:20px}.empty{padding:25px;text-align:center;color:var(--muted)}footer{padding:20px 0;color:var(--muted);font-size:13px}@media(max-width:1000px){.days{grid-template-columns:repeat(4,1fr)}.columns{grid-template-columns:1fr}}@media(max-width:600px){header,.topline{align-items:flex-start;flex-direction:column}.stats,.days{grid-template-columns:repeat(2,1fr)}h1{font-size:27px}main{padding:20px 14px}.formgrid{grid-template-columns:1fr}}@media print{button,form,nav{display:none}.columns{display:block}.days{grid-template-columns:repeat(7,1fr)}body{background:white}}
'''


def render(db, query, error=''):
    current = today(db)
    orders = db.execute('SELECT * FROM orders ORDER BY pickup,id').fetchall()
    requests = db.execute('SELECT * FROM requests WHERE received<=? ORDER BY received,id', (current.isoformat(),)).fetchall()
    pending = [r for r in requests if r['status'] == 'pending']
    revenue = sum(PRODUCTS[o['product']][0] * o['quantity'] for o in orders if o['status'] != 'cancelled')
    costs = db.execute('SELECT count(*)*100 FROM helpers').fetchone()[0]
    body = f'<div class="topline"><div><div class="eyebrow">Your daily baking desk</div><h2 style="margin:8px 0">{current.strftime("%A, %B %d, %Y")}</h2><span class="muted">Classroom timeline · All pickups at 5 PM</span></div>{button("advance", "Finish day →", disabled=bool(pending))}</div>'
    if error:
        body += f'<div class="notice" role="alert">{esc(error)}</div>'
    body += '<div class="stats">' + ''.join(f'<div class="stat"><small>{label}</small><strong>{value}</strong></div>' for label,value in [
        ('Messages to decide',len(pending)),('Committed sales',f'${revenue:,}'),('Helper bookings',f'${costs:,}'),('Sales less helpers',f'${revenue-costs:,}')]) + '</div>'
    body += '<p class="muted">Totals cover all imported and entered orders. Sales less helpers excludes ingredients and other costs.</p><section id="capacity" class="panel"><div class="sectionhead"><h2>Baking calendar</h2><small>Confirmed work only · 8 hours + 4 with helper</small></div><div class="days">'
    week = current - timedelta(days=current.weekday())
    for offset in range(7):
        day = (week + timedelta(days=offset)).isoformat()
        used, cap = usage(db,day), capacity(db,day)
        body += f'<div class="daycard {"today" if day==current.isoformat() else ""}"><small>{date.fromisoformat(day).strftime("%a")}</small><strong>{date.fromisoformat(day).strftime("%b %d")}</strong><p>{used/60:g} / {cap/60:g} hr</p><progress value="{used}" max="{cap}"></progress><small>{(cap-used)/60:g} hr free</small>'
        if cap == 720:
            body += '<p class="badge">Helper · $100</p>'
        elif (date.fromisoformat(day)-current).days >= 2:
            body += button('helper','+ Helper',{'day':day},secondary=True)
        else:
            body += '<p><small>Helper deadline passed</small></p>'
        body += '</div>'
    body += '</div><details style="margin-top:18px"><summary>Book a helper for another date</summary><form method="post"><input type="hidden" name="action" value="helper"><label for="helperday">Baking day (at least 2 days ahead)</label><input id="helperday" type="date" name="day" required min="'+(current+timedelta(days=2)).isoformat()+'"><p>One helper per day. $100 adds 4 hours; bookings are final.</p><button>Book helper · $100</button></form></details></section>'
    body += '<div class="columns"><section id="inbox"><div class="sectionhead"><h2>Instagram inbox</h2><span class="badge">'+str(len(pending))+' awaiting you</span></div><p class="muted">New messages arrive as you finish each day. Resolve earlier requests before their changes or cancellations.</p>'
    for r in pending:
        ok, reason, target = assess(db,r)
        body += f'<article class="message"><div class="sectionhead"><h3>{esc(r["customer"])}</h3><span class="badge {"" if ok else "warn"}">{esc(r["kind"].title())} · {"Can approve" if ok else "Blocked"}</span></div><small>Received {r["received"]} · Request {r["id"]}</small><pre>{esc(r["raw"])}</pre><small>{esc(r["source"])}</small><p class="rule">{esc(reason)}</p>'
        body += button('accept','Apply cancellation' if r['kind']=='cancel' else 'Approve change' if r['kind']=='change' else 'Accept order',{'id':r['id']},disabled=not ok)
        body += f'<form method="post" class="inline"><input type="hidden" name="action" value="reject"><input type="hidden" name="id" value="{r["id"]}"><input aria-label="Reason to reject request {r["id"]}" name="reason" placeholder="Optional rejection reason" maxlength="500"><button class="secondary">Reject request</button></form></article>'
    if not pending:
        body += '<div class="message empty">All arrived messages are decided. Finish the day to see the next inbox.</div>'
    body += '</section><aside><section class="panel"><h2>Record a request</h2><p class="muted">Received on the displayed day. This creates a pending request for your review.</p><form method="post"><input type="hidden" name="action" value="request"><label for="customer">Customer name</label><input id="customer" name="customer" required maxlength="100"><div class="formgrid"><div><label for="kind">Request</label><select id="kind" name="kind"><option value="new">New order</option><option value="change">Change order</option><option value="cancel">Cancel order</option></select></div><div><label for="pickup">Pickup date</label><input id="pickup" name="pickup" type="date" required></div><div><label for="product">Product</label><select id="product" name="product"><option value="cupcakes">Cupcakes (dozens)</option><option value="cake">Cake (each)</option></select></div><div><label for="quantity">Quantity</label><input id="quantity" name="quantity" type="number" min="1" max="1000" step="1" value="1" required></div></div><p><small>For changes/cancellations, use the exact customer name and existing pickup date. Product and quantity are ignored for cancellations. To move a pickup, cancel the original and record a new order; both need notice.</small></p><button>Add to inbox</button></form></section><section class="panel"><h3>The bakery rules</h3><p>Cupcakes: <b>$40 / dozen · 30 min</b><br>Cakes: <b>$120 each · 2 hours</b></p><p>One product per order. Bake on pickup day. Every request and helper booking needs <b>2 calendar days of notice</b>.</p><p>Base capacity is 8 hours/day. One helper costs $100 and adds 4 hours. You decide every request.</p><small>Cakes usually arrive a week ahead; cupcakes in the final three days. Saturday is usually busiest. These are planning hints, not acceptance rules.</small></section></aside></div>'
    search = query.get('search',[''])[0]
    status_filter = query.get('status',['all'])[0]
    pickup_filter = query.get('pickup',[''])[0]
    body += '<section id="orders" class="panel"><div class="sectionhead"><h2>Order book</h2><a href="/orders.csv">Export all orders (CSV)</a></div><form method="get" action="/#orders" class="formgrid"><div><label for="search">Search name or order number</label><input id="search" name="search" value="'+esc(search)+'"></div><div><label for="filterstatus">Status</label><select id="filterstatus" name="status">'+''.join(f'<option {"selected" if status_filter==s else ""} value="{s}">{s.title()}</option>' for s in ['all','accepted','ready','collected','cancelled'])+'</select></div><div><label for="filterday">Pickup day</label><input id="filterday" type="date" name="pickup" value="'+esc(pickup_filter)+'"></div><div><p><button>Filter orders</button> <a href="/#orders">Clear</a></p></div></form><div class="tablewrap"><table><thead><tr><th>Order / customer</th><th>Pickup · 5 PM</th><th>Product</th><th>Sales / hours</th><th>Status</th><th>Action</th></tr></thead><tbody>'
    for o in orders:
        if search.lower() not in (o['customer']+' #'+str(o['id'])).lower() or status_filter not in ('all',o['status']) or pickup_filter and o['pickup'] != pickup_filter:
            continue
        price, minutes = PRODUCTS[o['product']]
        action = button('status','Mark ready' if o['status']=='accepted' else 'Mark collected',{'id':o['id']},secondary=True) if o['pickup']==current.isoformat() and o['status'] in ('accepted','ready') else '—'
        body += f'<tr><td><b>#{o["id"]} · {esc(o["customer"])}</b></td><td>{o["pickup"]}</td><td>{o["quantity"]} {"dozen cupcakes" if o["product"]=="cupcakes" else "cake(s)"}</td><td>${price*o["quantity"]} / {minutes*o["quantity"]/60:g} hr</td><td><span class="badge">{o["status"]}</span></td><td>{action}</td></tr>'
    body += '</tbody></table></div><p class="muted">Cancelled orders remain in the record; their sales and hours are excluded from totals. Ready and collected orders still count toward their day’s baking capacity.</p></section><section id="history" class="panel"><h2>Decision history</h2><details><summary>View decided requests and original messages</summary>'
    for r in requests:
        if r['status']!='pending':
            body += f'<details class="message"><summary>Request {r["id"]} · {esc(r["customer"])} · {r["status"]}</summary><pre>{esc(r["raw"])}</pre><p>{esc(r["decision"])}</p><small>{esc(r["source"])}</small></details>'
    body += '</details><details><summary>View activity log</summary>' + ''.join(f'<p><small>{r["day"]}</small> · {esc(r["detail"])}</p>' for r in db.execute('SELECT * FROM audit ORDER BY id DESC'))+'</details></section><footer>Home Bakery · Saved locally in bakery.db · <a href="/README.html">Help &amp; implementation details</a></footer>'
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Home Bakery · Order desk</title><style>'+STYLE+'</style></head><body><header><div><div class="eyebrow">Home Bakery</div><h1>A little order, a better bake.</h1></div><nav><a href="#inbox">Inbox</a><a href="#capacity">Calendar</a><a href="#orders">Orders</a><a href="#history">History</a><a href="/README.html">Help</a></nav></header><main>'+body+'</main></body></html>'


class Handler(BaseHTTPRequestHandler):
    def send(self, content, status=200, mime='text/html; charset=utf-8'):
        data = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/README.html':
            return self.send((ROOT/'README.html').read_text(encoding='utf-8'))
        if parsed.path not in ('/', '/orders.csv'):
            return self.send('Not found', 404)
        with connect() as db:
            if parsed.path == '/orders.csv':
                out = io.StringIO(newline='')
                writer = csv.writer(out)
                writer.writerow(['Order','Customer','Product','Quantity','Pickup (5 PM)','Status','Sales','Baking minutes','Source'])
                for o in db.execute('SELECT * FROM orders ORDER BY pickup,id'):
                    price, minutes = PRODUCTS[o['product']]
                    # Keep spreadsheet programs from treating customer text as formulas.
                    name = o['customer']
                    if name.lstrip().startswith(('=', '+', '-', '@')):
                        name = "'"+name
                    writer.writerow([o['id'], name, o['product'], o['quantity'], o['pickup'], o['status'],
                        price*o['quantity'] if o['status']!='cancelled' else 0,
                        minutes*o['quantity'] if o['status']!='cancelled' else 0, o['source']])
                return self.send(out.getvalue(), mime='text/csv; charset=utf-8')
            self.send(render(db,parse_qs(parsed.query)))

    def do_POST(self):
        # Reject cross-origin browser writes, including forms from unrelated websites.
        expected = 'http://' + self.headers.get('Host','')
        if self.path != '/' or self.headers.get('Origin',expected) != expected or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self.send('Request not allowed',403)
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 16384:
                raise ValueError('Request is too large or empty.')
            form = {k:v[0] for k,v in parse_qs(self.rfile.read(length).decode('utf-8'),keep_blank_values=True).items()}
            with connect() as db:
                try:
                    act(db,form)
                except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
                    return self.send(render(db,{},str(exc)),400)
            self.send_response(303)
            self.send_header('Location','/')
            self.end_headers()
        except (ValueError, UnicodeError):
            self.send('Invalid request',400)


if __name__ == '__main__':
    with connect() as database:
        initialize(database)
    print('Home Bakery is ready: http://127.0.0.1:8000',flush=True)
    print('Keep this window open. Press Ctrl+C to stop.',flush=True)
    try:
        HTTPServer(('127.0.0.1',8000),Handler).serve_forever()
    except KeyboardInterrupt:
        print('\nBakery stopped. Your orders are saved.')
