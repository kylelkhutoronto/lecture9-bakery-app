# Home Bakery Order Desk

A local bakery order tracker built for MGOC50 Lecture 9. It manages cupcake and cake orders, changes, cancellations, baking capacity, helper bookings, and pickup status using fixed rules.

The app uses **Python's standard library and one SQLite database**. No packages, Node.js, or AI services are required. The interface follows the lecture's wireframe, data model, and order lifecycle.

## Start the app

1. Download or clone this repository and open its folder.
2. Open a terminal in that folder. On Windows, type `powershell` in File Explorer's address bar and press Enter.
3. Run:

   ```powershell
   python app.py
   ```

4. Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

Keep the terminal open while using the app. Press **Ctrl+C** to stop it. If Windows does not recognize `python`, try `py app.py`. If port 8000 is occupied, stop the other server using that port first.

Decisions save automatically in `bakery.db`. Closing or restarting the app does not reset them. **The database included in this repository contains saved classroom decisions**, so a downloaded copy resumes that saved state. Only a fresh database starts with the original Monday dataset.

For the HTML version of this guide, open [README.html](README.html) directly or use **Help** in the app.

## Daily workflow

The app uses a saved classroom date rather than your computer clock. A fresh database begins on **Monday, November 23, 2026**, with 13 existing accepted orders and 25 supplied Instagram messages across Monday–Sunday.

1. **Review the calendar.** Each day shows booked/free hours, capacity, committed customer orders, quantities, pickup status, and helper bookings. Weekly summary cards show committed sales, helper costs, and sales less helpers.
2. **Book help if needed.** The **+4 hr · $100** button books one helper for that date. A separate date form supports other dates. Book at least two calendar days ahead, before fetching messages that would otherwise fail capacity checks.
3. **Fetch messages.** Click **Fetch messages** to process messages received up to the displayed date. Future messages remain unavailable. Repeated fetching does not duplicate orders.
4. **Review requests.** Eligible new orders wait for your decision. Accept individually or reject, optionally recording a reason. Incompatible requests are automatically rejected. Valid changes and cancellations apply automatically when fetched.
5. **Accept all and go to next day.** After fetching all arrived messages, reject anything you do not want, then use this button. The app checks remaining requests sequentially, accepts each that fits, rejects those that no longer fit, closes the displayed day at simulated 5 PM, and advances one day.
6. **Fetch again on the next day.** Advancing does not automatically fetch the next inbox, giving you time to review capacity and book help first.

Pending requests reserve no capacity. Rejected requests stay rejected after help is booked. Helper bookings are final in the interface. There is no reset or undo-day button.

### Changes, cancellations, and extra requests

Use **Record a request** to enter another new order, change, or cancellation received on the displayed day. This entry is checked immediately without fetching other imported messages.

For changes and cancellations, enter the original customer name and pickup date. Matching ignores letter case and must find exactly one accepted order. Changes replace product and quantity on the same order; a rejected change leaves the original intact. To move a pickup date, cancel the original and enter a new request, both with sufficient notice.

Cupcake quantities mean **dozens**; cake quantities mean individual cakes. Use positive whole numbers. For a cancellation, product/quantity do not change the order, but valid values are still required in those form fields.

### Review and export

- The **order book** includes every fetched new order and supports customer/order-number, pickup-date, and status filters.
- **Incompatible orders** and **Changes and cancellations** show processed messages received on the displayed day.
- **Decision history** preserves original messages, outcomes, reasons, source identifiers, and an activity log.
- **Export all orders (CSV)** exports the full order book regardless of filters. Requested, rejected, and cancelled orders have zero committed sales and baking minutes in the export.

The order book's value and time-needed columns describe each order itself, including requests. They are separate from committed weekly totals.

## Business rules

| Product unit | Price | Baking time |
| --- | ---: | ---: |
| One dozen cupcakes | $40 | 30 minutes |
| One cake | $120 | 2 hours |

- Each order contains one product. All baking happens on its pickup date.
- Daily capacity is **8 hours**, or **12 hours** with one helper costing **$100**.
- Orders, changes, cancellations, and helper bookings require **two calendar days of notice**. Monday for Wednesday qualifies; Monday for Tuesday does not.
- Message notice uses its received date. Helper notice uses the displayed booking date.
- Acceptance rechecks capacity one request at a time, so accepting one can consume the space another needs.
- Changes recalculate capacity without counting the original order twice. Cancellations release its hours and remove its sales from totals.
- Only exactly one matching accepted order can be changed or cancelled. Picked-up, cancelled, and legacy Ready orders cannot be changed.
- Closing the pickup day marks accepted orders **Picked up** at simulated 5 PM; it does not wait for the real clock. Picked-up work still counts toward its day's used capacity.

Weekly totals cover the displayed Monday–Sunday week by pickup date. **Committed sales** excludes requested, rejected, and cancelled orders. **Sales less helpers is not profit**: ingredients, taxes, payment collection, and other expenses are not recorded.

## Files and implementation

| File | Purpose |
| --- | --- |
| `app.py` | Python HTTP server, page HTML/CSS, imports, database setup/migration, and fixed business rules |
| `bakery.db` | Single persistent SQLite database, including saved classroom decisions |
| `existing_orders.csv` | Orders accepted before Monday, November 23 |
| `InstagramOrders_Monday.html` through `InstagramOrders_Sunday.html` | Supplied customer messages |
| `test_bakery.py` | Automated regression tests using temporary database copies |
| `README.md` and `README.html` | Usage and implementation guides |
| `app.previous.py`, `test_bakery.previous.py`, `README.previous.html` | Previous-build reference copies |

Source files are imported once when a fresh database is created. Restarting preserves decisions; editing the sources afterward does not reimport them. Do not run the previous app against the current migrated database.

The page uses server-rendered HTML/CSS and ordinary forms; JavaScript is not required. The server binds only to `127.0.0.1:8000` and serves the app, HTML help, and CSV export rather than arbitrary folder files. There is no live Instagram connection, message sending, payment handling, AI decision-making, or automatic helper hiring.

### Data model

| Table or view | Purpose |
| --- | --- |
| `orders` | Current order facts: number, customer, product, quantity, pickup date, lifecycle status, and source |
| `days` | One row per baking date and its helper booking date, if any; orders associate with days by pickup date |
| `requests` | Original message, received date, source, requested details, decision/reason, and a foreign-key link to the affected order |
| `settings` | Saved classroom date and initialization marker |
| `audit` | Dated activity records |
| `day_summary` (view) | Derived capacity, order count, baking minutes, and sales |

Hours required and order value are calculated from product and quantity. Daily totals are derived rather than stored as duplicate facts. Message records represent what the customer requested at that time; Orders hold the current order facts. Changes and cancellations retain the original order number.

### Backend states and actions

```text
Fetch eligible new order -> Requested -> Accepted -> Picked up
                                  \-> Rejected by baker
Fetch incompatible order -> Incompatible -> Rejected automatically
Accepted + valid fetched cancellation -> Cancelled
```

Database order statuses are `requested`, `incompatible`, `rejected`, `accepted`, `cancelled`, and `collected` (displayed as Picked up). Incompatible is an immediate transition recorded in the audit log. Legacy `ready` records remain supported. Message decisions use `pending`, `approved`, or `rejected`.

- `assess()` checks notice, the target order, and capacity.
- `fetch_arrivals()` processes newly fetched messages.
- `decide_request()` records decisions and updates order states.
- `act()` handles form actions in transactions using `BEGIN IMMEDIATE`.
- `render()` builds the page.

Actions check rules before saving; failures roll back. Successful forms redirect to prevent refresh from repeating an action. SQL values use parameters, displayed user text is escaped, cross-origin forms are rejected, and exported customer names resembling spreadsheet formulas are protected.

## Differences from the first build

| Area | First build | Final build |
| --- | --- | --- |
| UI | Weekly hours, inbox/form columns, and order book | Adds explicit Fetch, customer orders inside calendar days, capacity beside decisions, and visible incompatible/change/cancellation sections |
| Data model | Helper table; Orders rows created on acceptance | Days table and derived daily summaries; Requested and Rejected new orders also have Orders records |
| Request handling | Manual decisions for changes, cancellations, and blocked requests | Fixed rules apply valid changes/cancellations and reject incompatible messages when fetched |
| Advancing | Required individual decisions first | Sequential bulk acceptance, simulated 5 PM pickups, then date advancement |
| Pickup tracking | Manual Ready then Collected buttons | Automatic Picked up transition when the pickup day closes |
| Financial totals | Covered all dates | Cover the displayed calendar week |

Both builds use the same prices, notice rules, capacity limits, helper costs, source data, order filters, history, and CSV export.

## Alignment with lecture slides 13–25

The final build follows the lecture's printed slide numbers:

- **13 and 17:** bakery objective, products, prices, baking times, capacity, notice, and helpers.
- **14–15:** local software with no AI inside the app, plus implementation and usage documentation.
- **18–20:** distinct page/rule/storage functions and an interface centered on Fetch, accept/reject, helpers, capacity, orders, and money.
- **21:** Orders linked to Days, with calculated hours, totals, and counts.
- **22–24:** Requested/Accepted/Rejected/Picked up/Cancelled lifecycle, automatic incompatible rejection, sequential acceptance, and helper capacity changes.
- **25:** staged implementation, regression tests, browser checks, and full-week acceptance tests with and without helpers.

## Tests and backups

Run from the project folder:

```powershell
python -m unittest -v test_bakery.py
```

The current build passed **15 automated tests**. They cover imports/restarts, notice, helpers, capacity, changes/cancellations, pickups, text escaping, sequential bulk acceptance, derived summaries, explicit/idempotent fetching, manual-entry isolation, saved-choice migration, lifecycle records, CSV totals, HTTP forms, and full weeks with and without helpers.

Each test starts with a temporary backup of `bakery.db` and resets **only that copy** to the source dataset. Tests never change the real database. The database and source files must be present. Desktop and phone layouts, day advancement, fetching, and an automatic cancellation were also checked in a browser using a temporary database copy.

To back up, stop the app and copy `bakery.db` somewhere safe. To restore, stop the app and replace it with your backup. Keep `app.py`, `README.html`, and the eight source files together when moving the app.
