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
        self.assertEqual(self.db.execute('SELECT count(*) FROM orders').fetchone()[0],13)
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
        self.assertEqual(self.request('Okafor')['status'],'pending')
        with self.assertRaises(ValueError):
            app.act(self.db,{'action':'advance'})

    def test_helper_capacity_and_duplicate(self):
        self.day('2026-11-26')
        app.act(self.db, {'action':'helper','day':'2026-11-28'})
        self.assertEqual(app.capacity(self.db,'2026-11-28'),720)
        with self.assertRaises(ValueError):
            app.act(self.db, {'action':'helper','day':'2026-11-28'})
        with self.assertRaises(ValueError):
            app.act(self.db, {'action':'helper','day':'2026-11-27'})
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
        self.decide('Brown','cancel')
        self.assertEqual(app.usage(self.db,'2026-11-26'),300)
        self.decide('Reyes')
        self.assertEqual(app.usage(self.db,'2026-11-26'),480)
        self.day('2026-11-25')
        self.decide('Moreau','change')
        self.assertEqual(app.usage(self.db,'2026-11-28'),300)
        self.decide('Lam')
        self.day('2026-11-26')
        with self.assertRaises(ValueError):
            self.decide('Lam','change')
        self.assertEqual(self.db.execute("SELECT quantity FROM orders WHERE customer='Lam'").fetchone()[0],2)

    def test_fulfillment_and_escaping(self):
        app.act(self.db,{'action':'status','id':'101'})
        app.act(self.db,{'action':'status','id':'101'})
        self.assertEqual(app.usage(self.db,'2026-11-23'),240)
        with self.assertRaises(ValueError):
            app.act(self.db,{'action':'status','id':'103'})
        app.act(self.db,{'action':'request','customer':'<script>hello</script>','kind':'new','product':'cake','quantity':'1','pickup':'2026-11-25'})
        page = app.render(self.db,{})
        self.assertNotIn('<script>hello</script>',page)
        self.assertIn('&lt;script&gt;hello&lt;/script&gt;',page)

    def test_full_week(self):
        rejected = []
        while app.today(self.db).isoformat() <= '2026-11-29':
            if app.today(self.db).isoformat() == '2026-11-24':
                app.act(self.db,{'action':'helper','day':'2026-11-27'})
            if app.today(self.db).isoformat() == '2026-11-25':
                app.act(self.db,{'action':'helper','day':'2026-11-28'})
            rows = self.db.execute("SELECT * FROM requests WHERE status='pending' AND received<=? ORDER BY received,id", (app.today(self.db).isoformat(),)).fetchall()
            for r in rows:
                ok, reason, target = app.assess(self.db,r)
                if not ok:
                    rejected.append(r['customer'])
                app.act(self.db,{'action':'accept' if ok else 'reject','id':str(r['id'])})
            for row in self.db.execute('SELECT DISTINCT pickup FROM orders'):
                self.assertLessEqual(app.usage(self.db,row[0]),app.capacity(self.db,row[0]))
            app.act(self.db,{'action':'advance'})
        self.assertEqual(rejected,['Okafor','Lam','Novak','Bell','Adams','Zhou','Lee'])
        self.assertEqual(app.usage(self.db,'2026-11-28'),720)
        self.assertEqual(app.usage(self.db,'2026-11-29'),300)
        self.assertEqual(app.usage(self.db,'2026-11-30'),240)
        self.assertEqual(app.usage(self.db,'2026-12-01'),300)
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
                self.assertIn('Customer',urllib.request.urlopen(base+'/orders.csv').read().decode())
                with self.assertRaises(urllib.error.HTTPError) as hidden:
                    urllib.request.urlopen(base+'/bakery.db')
                self.assertEqual(hidden.exception.code,404)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


if __name__ == '__main__':
    unittest.main()
