# EventLink Zambia

EventLink Zambia is a web application for discovering events, booking event planners, and buying tickets. Prices are shown in Zambian Kwacha (ZMW).

Tagline: Discover Events. Book Planners. Get Your Tickets.

## Features

- Public catalogue of approved events, with search, filters, and planner profiles.
- Client accounts for ticket purchase, digital tickets, payment history, and planner requests.
- Planner accounts that stay pending until an administrator approves them.
- Event submissions that cannot be published by the planner directly.
- Ticket stock reservation, server-side totals, and issuance only after a verified payment notification.
- A clearly labelled payment simulation when no live provider is configured. Simulated payments are stored separately from live payments.
- Administrator dashboards, user and event review, refund records, Chart.js reports, CSV export, and an audit log.
- Role checks on protected routes, CSRF protection, and password hashing.

## Technologies

- Python and Flask
- HTML, CSS, Bootstrap 5, and JavaScript
- SQLite through Flask-SQLAlchemy
- Flask-WTF for forms and CSRF protection
- Werkzeug password hashing
- Chart.js for administrator reports
- QR codes for ticket verification

Bootstrap, Bootstrap Icons, and Chart.js are included in `app/static/vendor` so the interface does not depend on a CDN while you are presenting the project.

## Prerequisites

- Python 3.11 or newer
- PowerShell or another terminal
- A modern browser

## Setup

From the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Open `.env` and replace the placeholders. `SECRET_KEY` and `SIMULATION_WEBHOOK_SECRET` must each be a unique random string of at least 16 characters. Do not use a password you use anywhere else, and do not commit `.env`.

Example values can be created with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Leave `PAYMENT_MODE=simulation` unless a live provider is configured. Leave `FLASK_DEBUG=0` unless you are developing on your own computer. Set `SESSION_COOKIE_SECURE=1` only when the site is served over HTTPS.

## Database

The application creates `instance/eventlink.db` on startup if it does not exist, enables SQLite foreign keys, and seeds the event categories.

You can also initialise it explicitly:

```powershell
$env:FLASK_APP = "run.py"
flask init-db
```

Money is stored as integer ngwee so totals do not use floating-point arithmetic. 1 ZMW = 100 ngwee.

The database file is not meant to be committed.

## Administrator account

There is no public administrator registration page. Create the first administrator with:

```powershell
flask create-admin
```

The command asks for a name, email, Zambian phone number, and a strong password. The password is stored only as a hash.

Stored passwords cannot be read back. If an administrator (or any user) forgets their password, set a new one with:

```powershell
flask reset-password
```

The command asks for the account email and the new password twice; the password is hidden while you type and the reset is recorded in the audit log.

## Run the application

```powershell
$env:FLASK_DEBUG = "1"
python run.py
```

Open http://127.0.0.1:5000

`FLASK_DEBUG=1` is for local development only. Leave it unset, or set it to `0`, for any shared or production run.

## Ticket scanning on a local network

Planners create gate attendant accounts under **Gate Attendants** in the planner dashboard and assign them to events. Attendants log in on a phone, open **Scan Ticket**, and point the camera at the ticket's QR code. The server decides whether each ticket is valid, already used or invalid, and a ticket can be admitted only once.

To run the gate from one laptop with phones on the same Wi-Fi (no internet needed; the scanner library is served from `app/static/vendor`):

1. Find the laptop's Wi-Fi address with `ipconfig` (for example `192.168.1.20`).
2. Phone browsers only allow the camera on HTTPS pages. Create a certificate for that address with [mkcert](https://github.com/FiloSottile/mkcert):

   ```powershell
   mkcert -install
   mkdir instance\certs
   mkcert -cert-file instance\certs\eventlink.pem -key-file instance\certs\eventlink-key.pem 192.168.1.20 localhost 127.0.0.1
   ```

   Install mkcert's root certificate (`mkcert -CAROOT` shows where it is) on each phone so the browser trusts it. On iPhone, also turn it on under Settings, General, About, Certificate Trust Settings.
3. In `.env` set `HOST=0.0.0.0`, `SSL_CERT_FILE=instance/certs/eventlink.pem`, `SSL_KEY_FILE=instance/certs/eventlink-key.pem` and `SESSION_COOKIE_SECURE=1`, keep `FLASK_DEBUG=0`, then start `python run.py`.
4. Allow Python through Windows Defender Firewall on **private** networks when Windows asks (or add an inbound rule for TCP port 5000).
5. On each phone open `https://192.168.1.20:5000`, log in as the gate attendant and allow camera access when asked.

If HTTPS is not set up, attendants can still type the ticket reference printed under the QR code; the scan page explains why the camera is unavailable.

## Tests

```powershell
python -m pytest
```

The tests cover registration, duplicate accounts, planner approval, event approval, ticket totals, inventory limits, payment verification, duplicate payment notifications, ticket redemption, booking conflicts, ownership checks, administrator protection, CSRF, and foreign keys.

## Payment provider

`PAYMENT_MODE=simulation` is the default. Checkout states that no real money is transferred. A simulated result is accepted only when the server verifies a signed provider notification. The checkout button does not mark a payment as successful by itself.

For a live provider, set:

```text
PAYMENT_MODE=live
PAYMENT_WEBHOOK_SECRET=a-long-random-secret
```

The provider should send an HTTP POST to `/payments/webhook/live` with the raw JSON body and the header `X-EventLink-Signature` set to the hex HMAC-SHA256 of that body using `PAYMENT_WEBHOOK_SECRET`.

The JSON body must contain:

- `idempotency_key`
- `payment_reference`
- `provider_reference`
- `amount` as an integer number of ngwee
- `currency` set to `ZMW`
- `status` set to `successful` or `failed`

The amount must match the order. Repeated notifications with the same idempotency key are ignored. Simulated notifications cannot update a live payment, and live notifications cannot update a simulated payment.

EventLink does not collect mobile-money PINs, card PINs, or card security codes.

## Refunds

If an administrator cancels an event, the client can request a refund from Payment history. A refund is marked completed only after an administrator confirms that it was handled outside EventLink. The application does not claim that it moved the money itself.

## Project layout

- `run.py` starts the application.
- `config.py` reads environment settings.
- `app/auth` handles registration and login.
- `app/client`, `app/planner`, and `app/admin` are the three role areas.
- `app/events`, `app/tickets`, and `app/payments` cover the public catalogue, tickets, and payments.
- `app/models/database.py` defines the database.
- `app/services` holds ticket, payment, booking, and report rules.
- `app/templates` and `app/static` hold the interface.
- `tests` holds the automated tests.
