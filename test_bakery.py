"""Run on temporary database backups only; never mutate bakery.db."""
import sqlite3
import tempfile
import unittest
import threading
import urllib.request
import urllib.parse
import urllib.error
from unittest.mock import patch
from pathlib import Path
import app


class BakeryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = app.connect(Path(self.temp.name) / 'bakery-copy.db')
        source = sqlite3.connect(app.DB.as_uri() + '?mode=ro', uri=True)
        source.backup(self.db)
        source.close()
        # Reset only the temporary copy so tests also work after the baker uses the app.
        self.db.execute('PRAGMA foreign_keys=OFF')
        for obj in self.db.execute("SELECT name,type FROM sqlite_master WHERE type IN ('table','view','trigger') AND name NOT LIKE 'sqlite_%'").fetchall():
            self.db.execute('DROP '+obj['type']+' IF EXISTS "'+obj['name']+'"')
        self.db.commit()
        self.db.execute('PRAGMA foreign_keys=ON')
        app.initialize(self.db)
        app.act(self.db, {'action':'fetch'})

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def request(self, name, kind='new'):
        return self.db.execute('SELECT * FROM requests WHERE customer=? AND kind=?', (name,kind)).fetchone()

    def decide(self, name, kind='new', action='accept'):
        app.act(self.db, {'action':action, 'id':str(self.request(name,kind)['id'])})

    def day(self, value):
        with self.db:
            self.db.execute("UPDATE settings SET value=? WHERE key='day'", (value,))

    def test_import_and_restart(self):
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0],16)
        self.assertEqual(self.db.execute('SELECT count(*) FROM requests').fetchone()[0],25)
        app.initialize(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM requests').fetchone()[0],25)
        self.assertEqual(app.usage(self.db,'2026-11-26'),360)
        self.assertEqual(app.usage(self.db,'2026-11-28'),240)

    def test_notice_and_future_messages(self):
        self.assertFalse(app.assess(self.db,self.request('Okafor'))[0])
        self.assertTrue(app.assess(self.db,self.request('Jasmine'))[0])
        self.assertFalse(app.assess(self.db,self.request('Reyes'))[0])
        with self.assertRaises(ValueError):
            self.decide('Okafor')
        self.assertEqual(self.request('Okafor')['status'],'rejected')
        self.assertIn('Automatically rejected', self.request('Okafor')['decision'])

    def test_helper_capacity_and_duplicate(self):
        self.day('2026-11-26')
        app.act(self.db, {'action':'helper','day':'2026-11-28'})
        self.assertEqual(app.capacity(self.db,'2026-11-28'),720)
        with self.assertRaises(ValueError):
            app.act(self.db, {'action':'helper','day':'2026-11-28'})
        with self.assertRaises(ValueError):
            app.act(self.db, {'action':'helper','day':'2026-11-27'})
        app.act(self.db,{'action':'fetch'})
        with self.db:
            self.db.execute("UPDATE orders SET quantity=12 WHERE customer='Moreau'")
        self.assertEqual(app.usage(self.db,'2026-11-28'),480)
        self.decide('Wilson')
        self.decide('Novak')
        self.assertEqual(app.usage(self.db,'2026-11-28'),720)
        with self.assertRaises(ValueError):
            self.decide('Bell')

    def test_changes_and_cancellations(self):
        self.day('2026-11-24')
        with self.db:
            app.fetch_arrivals(self.db)
        self.assertEqual(app.usage(self.db,'2026-11-26'),300)
        self.decide('Reyes')
        self.assertEqual(app.usage(self.db,'2026-11-26'),480)
        self.day('2026-11-25')
        with self.db:
            app.fetch_arrivals(self.db)
        self.assertEqual(app.usage(self.db,'2026-11-28'),300)
        self.decide('Lam')
        self.day('2026-11-26')
        with self.assertRaises(ValueError):
            self.decide('Lam','change')
        self.assertEqual(self.db.execute("SELECT quantity FROM orders WHERE customer='Lam'").fetchone()[0],2)

    def test_fulfillment_and_escaping(self):
        app.act(self.db,{'action':'request','customer':'<script>hello</script>','kind':'new','product':'cake','quantity':'1','pickup':'2026-11-25'})
        page = app.render(self.db,{})
        self.assertNotIn('<script>hello</script>',page)
        self.assertIn('&lt;script&gt;hello&lt;/script&gt;',page)
        app.act(self.db,{'action':'advance'})
        self.assertEqual(self.db.execute('SELECT status FROM orders WHERE id=101').fetchone()[0],'collected')
        self.assertEqual(app.usage(self.db,'2026-11-23'),240)

    def test_full_week(self):
        # Book before fetching the busy days' inboxes.
        app.act(self.db,{'action':'helper','day':'2026-11-27'})
        app.act(self.db,{'action':'helper','day':'2026-11-28'})
        while app.today(self.db).isoformat() <= '2026-11-29':
            app.act(self.db,{'action':'fetch'})
            app.act(self.db,{'action':'advance'})
            for row in self.db.execute('SELECT * FROM day_summary'):
                self.assertLessEqual(row['baking_minutes'],row['capacity_minutes'])
        rejected = [r[0] for r in self.db.execute("SELECT customer FROM requests WHERE status='rejected' ORDER BY id")]
        self.assertEqual(rejected,['Okafor','Lam','Novak','Bell','Adams','Zhou','Lee'])
        self.assertEqual(app.usage(self.db,'2026-11-28'),720)
        self.assertEqual(app.usage(self.db,'2026-11-29'),300)
        self.assertEqual(app.usage(self.db,'2026-11-30'),240)
        self.assertEqual(app.usage(self.db,'2026-12-01'),300)
        self.assertEqual(self.db.execute("SELECT count(*) FROM requests WHERE status='pending'").fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM orders WHERE pickup<'2026-11-30' AND status='accepted'").fetchone()[0],0)

    def test_bulk_rechecks_capacity_and_baker_reject(self):
        self.decide('Patel',action='reject')
        for name in ['One','Two']:
            app.act(self.db,{'action':'request','customer':name,'kind':'new','product':'cake','quantity':'2','pickup':'2026-11-25'})
        app.act(self.db,{'action':'advance'})
        self.assertEqual(self.request('One')['status'],'approved')
        self.assertEqual(self.request('Two')['status'],'rejected')
        self.assertEqual(self.request('Patel')['status'],'rejected')
        self.assertLessEqual(app.usage(self.db,'2026-11-25'),480)

    def test_automatic_changes_and_cancellation(self):
        app.act(self.db,{'action':'advance'})
        app.act(self.db,{'action':'fetch'})
        self.assertEqual(self.request('Brown','cancel')['status'],'approved')
        self.assertEqual(app.usage(self.db,'2026-11-26'),300)
        app.act(self.db,{'action':'advance'})
        app.act(self.db,{'action':'fetch'})
        self.assertEqual(self.request('Moreau','change')['status'],'approved')
        app.act(self.db,{'action':'advance'})
        app.act(self.db,{'action':'fetch'})
        self.assertEqual(self.request('Lam','change')['status'],'rejected')
        self.assertEqual(self.db.execute("SELECT quantity FROM orders WHERE customer='Lam'").fetchone()[0],2)

    def test_derived_day_totals(self):
        row = self.db.execute("SELECT * FROM day_summary WHERE date='2026-11-26'").fetchone()
        self.assertEqual((row['order_count'],row['baking_minutes'],row['sales']),(3,360,440))
        app.act(self.db,{'action':'helper','day':'2026-11-26'})
        self.assertEqual(self.db.execute("SELECT capacity_minutes FROM day_summary WHERE date='2026-11-26'").fetchone()[0],720)
        with self.assertRaises(ValueError):
            app.act(self.db,{'action':'request','customer':'Bad','kind':'new','product':'cake','quantity':'0','pickup':'2026-11-26'})
        self.assertIsNone(self.request('Bad'))

    def test_fetch_is_explicit_and_idempotent(self):
        app.act(self.db,{'action':'advance'})
        before = self.db.execute('SELECT count(*) FROM orders').fetchone()[0]
        self.assertIsNone(self.request('Reyes')['order_id'])
        with self.assertRaisesRegex(ValueError,'Fetch'):
            app.act(self.db,{'action':'advance'})
        self.assertEqual(app.today(self.db).isoformat(),'2026-11-24')
        app.act(self.db,{'action':'fetch'})
        self.assertGreater(self.db.execute('SELECT count(*) FROM orders').fetchone()[0],before)
        snapshot = [tuple(r) for r in self.db.execute('SELECT * FROM orders ORDER BY id')]
        audits = self.db.execute('SELECT count(*) FROM audit').fetchone()[0]
        app.act(self.db,{'action':'fetch'})
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM orders ORDER BY id')],snapshot)
        self.assertEqual(self.db.execute('SELECT count(*) FROM audit').fetchone()[0],audits)

    def test_manual_request_does_not_fetch_other_messages(self):
        app.act(self.db,{'action':'advance'})
        self.assertIsNone(self.request('Reyes')['decision'])
        app.act(self.db,{'action':'request','customer':'Manual','kind':'new','product':'cake','quantity':'1','pickup':'2026-11-26'})
        self.assertIsNotNone(self.request('Manual')['decision'])
        self.assertIsNone(self.request('Reyes')['decision'])
        self.assertIsNone(self.request('Brown','cancel')['decision'])
        with self.assertRaisesRegex(ValueError,'Fetch'):
            app.act(self.db,{'action':'advance'})

    def test_order_lifecycle_and_noncommitted_totals(self):
        for name,status in [('Jasmine','requested'),('Okafor','rejected')]:
            r = self.request(name)
            self.assertEqual(self.db.execute('SELECT status FROM orders WHERE id=?',(r['order_id'],)).fetchone()[0],status)
        self.assertEqual(app.usage(self.db,'2026-11-25'),120)
        self.decide('Jasmine')
        self.assertEqual(app.usage(self.db,'2026-11-25'),210)
        self.decide('Patel',action='reject')
        self.assertEqual(self.db.execute('SELECT status FROM orders WHERE id=?',(self.request('Patel')['order_id'],)).fetchone()[0],'rejected')
        self.assertEqual(app.usage(self.db,'2026-11-27'),300)
        page = app.render(self.db,{})
        for label in ['Fetch messages','Incompatible orders','Changes and cancellations','Pickup 5 PM','Kim','+4 hr · $100','day']:
            self.assertIn(label,page)
        # Requested/rejected orders have no committed sales in the week summary.
        sales = self.db.execute("SELECT sum(sales) FROM day_summary WHERE date BETWEEN '2026-11-23' AND '2026-11-29'").fetchone()[0]
        self.assertIn(f'${sales:,}',page)

    def test_migration_preserves_saved_choices(self):
        self.decide('Jasmine')
        accepted = tuple(self.db.execute('SELECT * FROM orders WHERE id=?',(self.request('Jasmine')['order_id'],)).fetchone())
        app.act(self.db,{'action':'helper','day':'2026-11-28'})
        # Simulate the previous build, which had no Orders rows for pending/rejected messages.
        with self.db:
            ids = [r[0] for r in self.db.execute("SELECT id FROM orders WHERE status IN ('requested','rejected')")]
            for order_id in ids:
                self.db.execute('UPDATE requests SET order_id=NULL WHERE order_id=?',(order_id,))
                self.db.execute('DELETE FROM orders WHERE id=?',(order_id,))
        decisions = [tuple(r) for r in self.db.execute('SELECT id,status,decision FROM requests ORDER BY id')]
        app.initialize(self.db)
        self.assertEqual(tuple(self.db.execute('SELECT * FROM orders WHERE id=?',(accepted[0],)).fetchone()),accepted)
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT id,status,decision FROM requests ORDER BY id')],decisions)
        self.assertEqual(app.capacity(self.db,'2026-11-28'),720)
        self.assertEqual(app.today(self.db).isoformat(),'2026-11-23')
        self.assertIsNotNone(self.request('Patel')['order_id'])
        snapshot = self.db.execute('SELECT count(*) FROM orders').fetchone()[0]
        app.initialize(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0],snapshot)

    def test_accept_all_each_day_without_helpers(self):
        while app.today(self.db).isoformat() <= '2026-11-29':
            app.act(self.db,{'action':'fetch'})
            app.act(self.db,{'action':'advance'})
            for row in self.db.execute('SELECT * FROM day_summary'):
                self.assertLessEqual(row['baking_minutes'],480)
        rejected = [r[0] for r in self.db.execute("SELECT customer FROM requests WHERE status='rejected' ORDER BY id")]
        self.assertEqual(rejected,['Okafor','Park','Singh','Lam','Wilson','Bell','Adams','Zhou','Lee'])
        self.assertEqual(app.usage(self.db,'2026-11-28'),480)
        self.assertEqual(app.usage(self.db,'2026-11-27'),480)
        self.assertEqual(self.db.execute("SELECT count(*) FROM requests WHERE status='pending'").fetchone()[0],0)

    def test_http_forms_and_saving(self):
        copy_path = Path(self.temp.name) / 'bakery-copy.db'
        original_connect = app.connect
        with patch.object(app,'connect',side_effect=lambda: original_connect(copy_path)):
            server = app.HTTPServer(('127.0.0.1',0),app.Handler)
            worker = threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                self.assertIn('Jasmine',urllib.request.urlopen(base).read().decode())
                form = urllib.parse.urlencode({'action':'accept','id':self.request('Jasmine')['id']}).encode()
                response = urllib.request.urlopen(urllib.request.Request(base+'/',data=form))
                self.assertEqual(response.status,200)
                self.assertEqual(self.request('Jasmine')['status'],'approved')
                with self.assertRaises(urllib.error.HTTPError) as repeat:
                    urllib.request.urlopen(urllib.request.Request(base+'/',data=form))
                self.assertEqual(repeat.exception.code,400)
                malicious = urllib.request.Request(base+'/',data=urllib.parse.urlencode({'action':'helper','day':'2026-11-28'}).encode(),headers={'Origin':'https://other.example'})
                with self.assertRaises(urllib.error.HTTPError) as blocked:
                    urllib.request.urlopen(malicious)
                self.assertEqual(blocked.exception.code,403)
                import csv, io
                exported = list(csv.DictReader(io.StringIO(urllib.request.urlopen(base+'/orders.csv').read().decode())))
                for row in exported:
                    if row['Status'] in ('requested','rejected','cancelled'):
                        self.assertEqual((row['Sales'],row['Baking minutes']),('0','0'))
                self.assertIn('Okafor',[r['Customer'] for r in exported])
                with self.assertRaises(urllib.error.HTTPError) as hidden:
                    urllib.request.urlopen(base+'/bakery.db')
                self.assertEqual(hidden.exception.code,404)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


if __name__ == '__main__':
    unittest.main()
