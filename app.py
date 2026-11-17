#!/usr/bin/env python3
"""
Aplikasi web: Modul HRD (cuti) + Modul Pemesanan Tiket Pesawat  (US-01 s/d US-13)

- Hanya memakai library bawaan Python 3.9+ (tanpa pip install).
- Database: SQLite (file app.db dibuat otomatis, berisi data contoh).
- Jalankan:   python app.py          lalu buka  http://127.0.0.1:8000
- Reset data: python app.py --reset
"""
import hashlib
import hmac
import html
import os
import random
import re
import secrets
import smtplib
import sqlite3
import sys
import traceback
import zlib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from http import cookies
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIServer, make_server

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "app.db")
OUTBOX = os.path.join(BASE, "outbox")

esc = html.escape

KOTA = {
    "MLG": "Malang",
    "CGK": "Jakarta",
    "SUB": "Surabaya",
    "DPS": "Denpasar",
    "YIA": "Yogyakarta",
    "UPG": "Makassar",
}
MASKAPAI = [("Garuda Indonesia", "GA", 1.35), ("Lion Air", "JT", 0.90),
            ("Citilink", "QG", 1.00), ("Batik Air", "ID", 1.10)]
SLOTS = ["06:30", "12:15", "18:45"]
HORIZON_HARI = 60

STATUS_BOOKING = ["Waiting for Payment", "Booked", "Confirmed"]
BLN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]

ROLE_LABEL = {"hrd": "HRD Manajer", "pengguna": "Pengguna", "member": "Member",
              "manajer_travel": "Manajer Travel"}
HRD, PENGGUNA, MEMBER, MANAJER = {"hrd"}, {"pengguna", "member"}, {"member"}, {"manajer_travel"}

MENU = {
    "hrd": [
        ("Pengajuan Cuti per Periode", "/hrd/cuti", "US-01: lihat siapa yang cuti pada rentang tanggal tertentu"),
        ("Riwayat & Sisa Cuti", "/hrd/riwayat", "US-02: riwayat cuti dan sisa jatah per pegawai"),
    ],
    "manajer_travel": [
        ("Semua Booking", "/manajer/booking", "US-10: pantau booking dan filter status"),
        ("Pesan untuk Pengguna", "/manajer/pesan", "US-11: cari tiket dan pesan atas nama Pengguna"),
        ("Otorisasi Booking", "/manajer/otorisasi", "US-13: verifikasi dengan booking reference"),
    ],
}
MENU["pengguna"] = [
    ("Cari Penerbangan", "/penerbangan", "US-03: cari jadwal ke satu/beberapa tujuan"),
    ("Pesanan Saya", "/pesanan", "US-06/07: pesanan aktif dan pembatalan"),
    ("Tiket Saya", "/tiket", "US-12: lihat tiket dan status e-tiket"),
]
MENU["member"] = MENU["pengguna"] + [
    ("Multi-Destinasi", "/multi", "US-04: rencana perjalanan beberapa kota (Member)"),
    ("Bandingkan Penerbangan", "/bandingkan", "US-05: bandingkan harga, jam, maskapai (Member)"),
    ("Profil Saya", "/profil", "US-08: lengkapi data profil (Member)"),
    ("Validasi Tiket", "/validasi", "US-09: validasi tiket dengan data profil (Member)"),
]


# --------------------------------------------------------------------------- util
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"


def cek_pw(pw, stored):
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def rp(n):
    return "Rp " + f"{int(n):,}".replace(",", ".")


def tgl(s):
    d = date.fromisoformat(s)
    return f"{d.day} {BLN[d.month - 1]} {d.year}"


def parse_date(s):
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def parse_time(s):
    try:
        return datetime.strptime(s, "%H:%M").strftime("%H:%M")
    except (ValueError, TypeError):
        return None


def durasi(mulai, selesai):
    return (date.fromisoformat(selesai) - date.fromisoformat(mulai)).days + 1


def hari_di_tahun(mulai, selesai, tahun):
    a = max(date.fromisoformat(mulai), date(tahun, 1, 1))
    b = min(date.fromisoformat(selesai), date(tahun, 12, 31))
    return max(0, (b - a).days + 1)


def kota_label(code):
    return f"{KOTA.get(code, code)} ({code})"


def opsi_kota(sel="", kosong=None):
    out = f'<option value="">{esc(kosong)}</option>' if kosong else ""
    for k, v in KOTA.items():
        out += f'<option value="{k}"{" selected" if k == sel else ""}>{esc(v)} ({k})</option>'
    return out


def badge(status):
    kelas = {"Waiting for Payment": "warn", "Booked": "info", "Confirmed": "ok",
             "Dibatalkan": "bad", "Disetujui": "ok", "Diajukan": "warn", "Ditolak": "bad"}.get(status, "")
    return f'<span class="badge {kelas}">{esc(status)}</span>'


def tabel(headers, rows):
    th = "".join(f"<th>{h}</th>" for h in headers)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'


# --------------------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, nama TEXT, email TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS profil(
  user_id INTEGER PRIMARY KEY, nama_lengkap TEXT DEFAULT '', nik TEXT DEFAULT '', no_hp TEXT DEFAULT '',
  tanggal_lahir TEXT DEFAULT '', alamat TEXT DEFAULT '', lengkap INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pegawai(
  id INTEGER PRIMARY KEY, nama TEXT, jatah_tahunan INTEGER DEFAULT 12);
CREATE TABLE IF NOT EXISTS cuti(
  id INTEGER PRIMARY KEY, pegawai_id INTEGER, mulai TEXT, selesai TEXT, jenis TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS flights(
  id INTEGER PRIMARY KEY, kode TEXT, maskapai TEXT, asal TEXT, tujuan TEXT, tanggal TEXT,
  jam_berangkat TEXT, jam_tiba TEXT, harga INTEGER, kursi INTEGER);
CREATE INDEX IF NOT EXISTS idx_flights ON flights(asal, tujuan, tanggal);
CREATE TABLE IF NOT EXISTS bookings(
  id INTEGER PRIMARY KEY, ref TEXT UNIQUE, user_id INTEGER, flight_id INTEGER, harga INTEGER,
  status TEXT, created_by INTEGER, created_at TEXT,
  validated INTEGER DEFAULT 0, validated_at TEXT, validated_nama TEXT);
CREATE TABLE IF NOT EXISTS email_log(
  id INTEGER PRIMARY KEY, booking_id INTEGER, ke TEXT, status TEXT, keterangan TEXT, waktu TEXT);
"""


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def gen_flights_for_date(db, d):
    ds = d.isoformat()
    rows = []
    for a in KOTA:
        for b in KOTA:
            if a == b:
                continue
            pair = "".join(sorted([a, b]))
            base = 400_000 + (zlib.crc32(pair.encode()) % 900) * 1000
            dur = 60 + (zlib.crc32(pair.encode()) % 120)
            for i, jam in enumerate(SLOTS):
                rng = random.Random(f"{a}{b}{ds}{i}")
                nama, kode, fak = MASKAPAI[rng.randrange(len(MASKAPAI))]
                harga = int(round(base * fak * rng.uniform(0.9, 1.25) / 1000) * 1000)
                tiba = (datetime.strptime(jam, "%H:%M") + timedelta(minutes=dur)).strftime("%H:%M")
                kursi = 0 if rng.random() < 0.04 else rng.randint(3, 40)
                rows.append((f"{kode}{100 + rng.randrange(900)}", nama, a, b, ds, jam, tiba, harga, kursi))
    db.executemany("INSERT INTO flights(kode,maskapai,asal,tujuan,tanggal,jam_berangkat,jam_tiba,harga,kursi)"
                   " VALUES(?,?,?,?,?,?,?,?,?)", rows)


def ensure_flights(db):
    today = date.today()
    for i in range(HORIZON_HARI + 1):
        d = today + timedelta(days=i)
        if not db.execute("SELECT 1 FROM flights WHERE tanggal=? LIMIT 1", (d.isoformat(),)).fetchone():
            gen_flights_for_date(db, d)


def make_ref(db):
    alfabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        ref = "BK" + "".join(secrets.choice(alfabet) for _ in range(6))
        if not db.execute("SELECT 1 FROM bookings WHERE ref=?", (ref,)).fetchone():
            return ref


def create_booking(db, user_id, flight_id, created_by, status="Waiting for Payment"):
    """Return (booking_id, error). error: None | 'tidak_ada' | 'lewat' | 'habis'."""
    f = db.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
    if not f:
        return None, "tidak_ada"
    if f["tanggal"] < date.today().isoformat():
        return None, "lewat"
    cur = db.execute("UPDATE flights SET kursi=kursi-1 WHERE id=? AND kursi>0", (flight_id,))
    if cur.rowcount == 0:
        return None, "habis"
    cur = db.execute(
        "INSERT INTO bookings(ref,user_id,flight_id,harga,status,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
        (make_ref(db), user_id, flight_id, f["harga"], status, created_by,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return cur.lastrowid, None


def seed(db):
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    today = date.today()
    users = [
        ("hrd", "hrd123", "Hana HRD", "hana@contoh.id", "hrd"),
        ("manajer", "manajer123", "Tono Manajer Travel", "tono@contoh.id", "manajer_travel"),
        ("budi", "budi123", "Budi Santoso", "budi@contoh.id", "pengguna"),
        ("sari", "sari123", "Sari Member", "sari@contoh.id", "member"),
        ("andi", "andi123", "Andi Member", "andi@contoh.id", "member"),
    ]
    for u, p, n, e, r in users:
        db.execute("INSERT INTO users(username,password,nama,email,role) VALUES(?,?,?,?,?)",
                   (u, hash_pw(p), n, e, r))
    uid = {r["username"]: r["id"] for r in db.execute("SELECT id, username FROM users")}
    db.execute("INSERT INTO profil(user_id,nama_lengkap,nik,no_hp,tanggal_lahir,alamat,lengkap) VALUES(?,?,?,?,?,?,1)",
               (uid["sari"], "Sari Wulandari", "3573012345670001", "081234567890", "1995-04-12",
                "Jl. Veteran No. 10, Malang"))
    db.execute("INSERT INTO profil(user_id,nama_lengkap,nik,no_hp,tanggal_lahir,alamat,lengkap) VALUES(?,?,?,?,?,?,0)",
               (uid["andi"], "Andi Pratama", "", "", "", ""))

    pegawai = ["Rina Wulandari", "Dedi Kurniawan", "Maya Sari", "Agus Prasetyo", "Lestari Putri", "Bambang Hartono"]
    for n in pegawai:
        db.execute("INSERT INTO pegawai(nama,jatah_tahunan) VALUES(?,12)", (n,))
    pid = {r["nama"]: r["id"] for r in db.execute("SELECT id, nama FROM pegawai")}
    cuti = [
        ("Rina Wulandari", -60, 3, "Tahunan", "Disetujui"), ("Rina Wulandari", 2, 3, "Tahunan", "Disetujui"),
        ("Rina Wulandari", 30, 5, "Tahunan", "Diajukan"), ("Dedi Kurniawan", -20, 2, "Sakit", "Disetujui"),
        ("Dedi Kurniawan", 5, 4, "Tahunan", "Diajukan"), ("Maya Sari", 1, 2, "Penting", "Disetujui"),
        ("Maya Sari", 10, 1, "Tahunan", "Ditolak"), ("Agus Prasetyo", -100, 5, "Tahunan", "Disetujui"),
        ("Lestari Putri", 3, 7, "Tahunan", "Diajukan"),
    ]
    for nama, off, lama, jenis, st in cuti:
        m = today + timedelta(days=off)
        db.execute("INSERT INTO cuti(pegawai_id,mulai,selesai,jenis,status) VALUES(?,?,?,?,?)",
                   (pid[nama], m.isoformat(), (m + timedelta(days=lama - 1)).isoformat(), jenis, st))

    ensure_flights(db)

    def cari(a, b, off):
        r = db.execute("SELECT id FROM flights WHERE asal=? AND tujuan=? AND tanggal=? AND kursi>0 ORDER BY jam_berangkat",
                       (a, b, (today + timedelta(days=off)).isoformat())).fetchone()
        return r["id"]

    mgr = uid["manajer"]
    create_booking(db, uid["budi"], cari("MLG", "CGK", 4), uid["budi"], "Waiting for Payment")
    create_booking(db, uid["sari"], cari("SUB", "DPS", 6), uid["sari"], "Booked")
    create_booking(db, uid["sari"], cari("CGK", "MLG", 9), uid["sari"], "Confirmed")
    create_booking(db, uid["budi"], cari("DPS", "SUB", 12), uid["budi"], "Confirmed")
    create_booking(db, uid["andi"], cari("YIA", "MLG", 8), mgr, "Waiting for Payment")


def init_db():
    db = connect()
    db.executescript(SCHEMA)
    seed(db)
    ensure_flights(db)
    db.commit()
    db.close()


BOOKING_SQL = """
SELECT b.*, f.kode, f.maskapai, f.asal, f.tujuan, f.tanggal, f.jam_berangkat, f.jam_tiba,
       u.nama AS pemesan, u.email AS email_pemesan, c.nama AS pembuat
FROM bookings b
JOIN flights f ON f.id = b.flight_id
JOIN users u ON u.id = b.user_id
JOIN users c ON c.id = b.created_by
"""


def kirim_eticket(db, booking_id):
    b = db.execute(BOOKING_SQL + " WHERE b.id=?", (booking_id,)).fetchone()
    status, ket = "gagal", ""
    try:
        msg = EmailMessage()
        msg["Subject"] = f"E-Tiket {b['ref']} - {b['asal']} ke {b['tujuan']}"
        msg["From"] = os.environ.get("SMTP_FROM", "no-reply@tiket.local")
        msg["To"] = b["email_pemesan"]
        msg.set_content(
            f"Halo {b['pemesan']},\n\nBerikut e-tiket Anda.\n\n"
            f"Booking reference : {b['ref']}\nStatus            : {b['status']}\n"
            f"Penerbangan       : {b['maskapai']} {b['kode']}\n"
            f"Rute              : {kota_label(b['asal'])} -> {kota_label(b['tujuan'])}\n"
            f"Tanggal           : {tgl(b['tanggal'])}\n"
            f"Berangkat / Tiba  : {b['jam_berangkat']} / {b['jam_tiba']}\n"
            f"Harga             : {rp(b['harga'])}\n")
        host = os.environ.get("SMTP_HOST")
        if host:
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=10) as s:
                if os.environ.get("SMTP_TLS", "1") == "1":
                    s.starttls()
                if os.environ.get("SMTP_USER"):
                    s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
                s.send_message(msg)
            status = "terkirim"
        else:
            os.makedirs(OUTBOX, exist_ok=True)
            with open(os.path.join(OUTBOX, f"{b['ref']}.eml"), "wb") as fh:
                fh.write(bytes(msg))
            status, ket = "simulasi", "SMTP belum diatur; e-tiket disimpan sebagai file di folder outbox"
    except Exception as e:  # AC3 US-12: kegagalan dicatat, tiket tetap tampil
        status, ket = "gagal", str(e)[:200]
    db.execute("INSERT INTO email_log(booking_id,ke,status,keterangan,waktu) VALUES(?,?,?,?,?)",
               (booking_id, b["email_pemesan"], status, ket, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return status


def status_email(db, booking_id):
    r = db.execute("SELECT * FROM email_log WHERE booking_id=? ORDER BY id DESC LIMIT 1", (booking_id,)).fetchone()
    if not r:
        return "Belum ada catatan pengiriman"
    if r["status"] == "terkirim":
        return f"Terkirim ke {esc(r['ke'])}"
    if r["status"] == "simulasi":
        return f"Disimulasikan (file di folder outbox, tujuan {esc(r['ke'])})"
    return f'<span class="err">Gagal terkirim: {esc(r["keterangan"] or "")}</span>'


# --------------------------------------------------------------------------- web framework mini
SESSIONS = {}
ROUTES = []


def route(method, pattern, roles=None):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), roles, fn))
        return fn
    return deco


class Req:
    def __init__(self, environ):
        self.method = environ["REQUEST_METHOD"]
        self.path = environ.get("PATH_INFO", "/") or "/"
        self.query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        self.form = {}
        if self.method == "POST":
            n = min(int(environ.get("CONTENT_LENGTH") or 0), 1_000_000)
            self.form = parse_qs(environ["wsgi.input"].read(n).decode("utf-8", "replace"), keep_blank_values=True)
        c = cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
        sid = c["sid"].value if "sid" in c else None
        self.sid = sid if sid in SESSIONS else None
        self.session = SESSIONS.get(self.sid)
        self.db = None
        self.user = None

    def q(self, k, d=""):
        return self.query.get(k, [d])[0].strip()

    def ql(self, k):
        return [v.strip() for v in self.query.get(k, []) if v.strip()]

    def f(self, k, d=""):
        return self.form.get(k, [d])[0].strip()

    def raw(self, k, d=""):
        return self.form.get(k, [d])[0]


def resp(body, status="200 OK", headers=None):
    return status, [("Content-Type", "text/html; charset=utf-8")] + (headers or []), body.encode("utf-8")


def redirect(url, headers=None):
    return "303 See Other", [("Location", url)] + (headers or []), b""


def flash(req, pesan, kind="ok"):
    if req.session is not None:
        req.session["flash"].append((kind, pesan))


def csrf(req):
    return f'<input type="hidden" name="_csrf" value="{req.session["csrf"]}">' if req.session else ""


CSS = """
:root{--bg:#f4f6fa;--card:#fff;--ink:#1f2933;--muted:#667085;--line:#e4e7ec;--brand:#1d4ed8;
--ok:#047857;--okbg:#d1fae5;--bad:#b42318;--badbg:#fee4e2;--warn:#b54708;--warnbg:#fef0c7;--infobg:#dbeafe}
*{box-sizing:border-box}
body{margin:0;font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--brand);color:#fff;padding:.6rem 1.2rem;display:flex;gap:1.2rem;align-items:center;flex-wrap:wrap}
header a{color:#fff;text-decoration:none;opacity:.9}header a:hover{opacity:1;text-decoration:underline}
header .brand{font-weight:700;font-size:1.05rem;opacity:1}
header .spacer{flex:1}header form{margin:0}
header button{background:rgba(255,255,255,.18);color:#fff;border:0;border-radius:6px;padding:.3rem .7rem;cursor:pointer}
main{max-width:1050px;margin:1.4rem auto;padding:0 1rem}
h1{font-size:1.4rem;margin:.2rem 0 1rem}h2{font-size:1.1rem;margin:1.3rem 0 .5rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:1rem 1.2rem;margin-bottom:1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:.9rem}
.grid a.card{color:inherit;text-decoration:none;margin:0}.grid a.card:hover{border-color:var(--brand)}
.muted{color:var(--muted);font-size:.9rem}
form.row{display:flex;gap:.8rem;flex-wrap:wrap;align-items:flex-end}
label{display:flex;flex-direction:column;font-size:.85rem;color:var(--muted);gap:.2rem}
input,select,textarea{font:inherit;padding:.4rem .55rem;border:1px solid #cfd4dc;border-radius:6px;background:#fff;color:var(--ink)}
button,.btn{font:inherit;background:var(--brand);color:#fff;border:0;border-radius:6px;padding:.45rem .9rem;cursor:pointer;text-decoration:none;display:inline-block}
button.danger{background:var(--bad)}button.ghost,.btn.ghost{background:#eef2ff;color:var(--brand)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:#fff}
th,td{padding:.5rem .65rem;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{background:#f8fafc;font-size:.82rem;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.badge{padding:.1rem .55rem;border-radius:99px;font-size:.8rem;background:#eee}
.badge.ok{background:var(--okbg);color:var(--ok)}.badge.bad{background:var(--badbg);color:var(--bad)}
.badge.warn{background:var(--warnbg);color:var(--warn)}.badge.info{background:var(--infobg);color:var(--brand)}
.msg{padding:.7rem 1rem;border-radius:8px;margin-bottom:1rem}
.msg.ok{background:var(--okbg);color:#065f46}.msg.err{background:var(--badbg);color:var(--bad)}
.msg.info{background:var(--infobg);color:#1e40af}.err{color:var(--bad)}
.big{font-size:1.8rem;font-weight:700;letter-spacing:.08em}
.req{color:var(--bad);font-size:.8rem}
.cmp td:first-child,.cmp th:first-child{background:#f8fafc;font-weight:600;color:var(--muted)}
.best{color:var(--ok);font-weight:700}
"""


def layout(req, judul, isi):
    nav = ""
    if req.user:
        for label, href, _ in MENU.get(req.user["role"], []):
            nav += f'<a href="{href}">{esc(label)}</a>'
        nav += (f'<span class="spacer"></span><span>{esc(req.user["nama"])} · {ROLE_LABEL[req.user["role"]]}</span>'
                f'<form method="post" action="/logout">{csrf(req)}<button>Keluar</button></form>')
    notes = ""
    if req.session and req.session["flash"]:
        for kind, m in req.session["flash"]:
            notes += f'<div class="msg {"err" if kind == "err" else kind}">{esc(m)}</div>'
        req.session["flash"] = []
    return (f'<!doctype html><html lang="id"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(judul)} · Tiket &amp; Cuti</title>'
            f'<style>{CSS}</style></head><body><header><a class="brand" href="/">Tiket &amp; Cuti</a>{nav}</header>'
            f'<main>{notes}<h1>{esc(judul)}</h1>{isi}</main></body></html>')


def page(req, judul, isi, status="200 OK"):
    return resp(layout(req, judul, isi), status)


def forbidden(req):
    if req.user["role"] == "pengguna":
        m = "Fitur ini hanya tersedia untuk Member."
    else:
        m = "Anda tidak memiliki akses ke halaman ini."
    return page(req, "Akses ditolak", f'<div class="msg err">{m}</div><a class="btn ghost" href="/">Kembali</a>',
                "403 Forbidden")


def tabel_flights(rows, aksi_fn=None, pilih_name=None):
    head = (["Pilih"] if pilih_name else []) + ["Penerbangan", "Maskapai", "Rute", "Tanggal", "Berangkat", "Tiba",
                                                 "Harga", "Kursi"] + (["Aksi"] if aksi_fn else [])
    body = []
    for f in rows:
        cells = []
        if pilih_name:
            cells.append(f'<input type="checkbox" name="{pilih_name}" value="{f["id"]}"'
                         f'{"" if f["kursi"] > 0 else " disabled"}>')
        cells += [esc(f["kode"]), esc(f["maskapai"]), f'{esc(f["asal"])} → {esc(f["tujuan"])}', tgl(f["tanggal"]),
                  f["jam_berangkat"], f["jam_tiba"], rp(f["harga"]),
                  str(f["kursi"]) if f["kursi"] > 0 else '<span class="err">Penuh</span>']
        if aksi_fn:
            cells.append(aksi_fn(f))
        body.append(cells)
    return tabel(head, body)


def link_pesan(f):
    if f["kursi"] > 0:
        return f'<a class="btn" href="/pesan/{f["id"]}">Pesan</a>'
    return '<span class="muted">Tidak dapat dipesan</span>'


def validasi_tanggal_depan(s):
    """Return (date|None, error|None)"""
    d = parse_date(s)
    if not d:
        return None, "Tanggal tidak valid."
    if d < date.today():
        return None, "Tanggal tidak boleh sebelum hari ini."
    return d, None


# --------------------------------------------------------------------------- auth & home
@route("GET", "/login")
def login_form(req):
    if req.user:
        return redirect("/")
    demo = ("<p class='muted'>Akun contoh: <b>hrd/hrd123</b> (HRD), <b>manajer/manajer123</b> (Manajer Travel), "
            "<b>budi/budi123</b> (Pengguna), <b>sari/sari123</b> (Member, profil lengkap), "
            "<b>andi/andi123</b> (Member, profil belum lengkap)</p>")
    return page(req, "Masuk", f'<div class="card"><form method="post" class="row">'
                f'<label>Username<input name="username" required autofocus></label>'
                f'<label>Password<input name="password" type="password" required></label>'
                f'<button>Masuk</button></form>{demo}</div>')


@route("POST", "/login")
def login_post(req):
    u = req.db.execute("SELECT * FROM users WHERE username=?", (req.f("username").lower(),)).fetchone()
    if u and cek_pw(req.raw("password"), u["password"]):
        sid = secrets.token_urlsafe(24)
        SESSIONS[sid] = {"user_id": u["id"], "csrf": secrets.token_urlsafe(16), "flash": []}
        return redirect("/", [("Set-Cookie", f"sid={sid}; Path=/; HttpOnly; SameSite=Lax")])
    return page(req, "Masuk", '<div class="msg err">Username atau password salah.</div>'
                '<a class="btn ghost" href="/login">Coba lagi</a>', "401 Unauthorized")


@route("POST", "/logout", "login")
def logout(req):
    SESSIONS.pop(req.sid, None)
    return redirect("/login", [("Set-Cookie", "sid=; Path=/; Max-Age=0")])


@route("GET", "/", "login")
def home(req):
    cards = "".join(f'<a class="card" href="{h}"><b>{esc(l)}</b><div class="muted">{esc(d)}</div></a>'
                    for l, h, d in MENU[req.user["role"]])
    return page(req, f"Halo, {req.user['nama']}",
                f'<p class="muted">Masuk sebagai {ROLE_LABEL[req.user["role"]]}. Pilih menu:</p><div class="grid">{cards}</div>')


# --------------------------------------------------------------------------- HRD: US-01, US-02
@route("GET", "/hrd/cuti", HRD)
def hrd_cuti(req):
    today = date.today()
    awal = parse_date(req.q("dari")) or today.replace(day=1)
    akhir = parse_date(req.q("sampai")) or ((today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
    form = (f'<div class="card"><form class="row" method="get">'
            f'<label>Dari tanggal<input type="date" name="dari" value="{awal}" required></label>'
            f'<label>Sampai tanggal<input type="date" name="sampai" value="{akhir}" required></label>'
            f'<button>Tampilkan</button></form></div>')
    if awal > akhir:
        return page(req, "Pengajuan Cuti per Periode",
                    form + '<div class="msg err">Tanggal mulai tidak boleh setelah tanggal akhir.</div>')
    rows = req.db.execute(
        "SELECT c.*, p.nama FROM cuti c JOIN pegawai p ON p.id=c.pegawai_id "
        "WHERE c.mulai<=? AND c.selesai>=? ORDER BY c.mulai, p.nama", (akhir.isoformat(), awal.isoformat())).fetchall()
    info = f'<p class="muted">Periode {tgl(awal.isoformat())} s/d {tgl(akhir.isoformat())}</p>'
    if not rows:
        return page(req, "Pengajuan Cuti per Periode",
                    form + info + '<div class="msg info">Tidak ada pengajuan cuti pada periode ini</div>')
    t = tabel(["Nama Pegawai", "Mulai", "Selesai", "Durasi (hari)", "Jenis", "Status"],
              [[esc(r["nama"]), tgl(r["mulai"]), tgl(r["selesai"]), str(durasi(r["mulai"], r["selesai"])),
                esc(r["jenis"]), badge(r["status"])] for r in rows])
    return page(req, "Pengajuan Cuti per Periode", form + info + t)


@route("GET", "/hrd/riwayat", HRD)
def hrd_riwayat(req):
    db = req.db
    pegawai = db.execute("SELECT * FROM pegawai ORDER BY nama").fetchall()
    pid = req.q("pegawai")
    opts = '<option value="">-- pilih pegawai --</option>' + "".join(
        f'<option value="{p["id"]}"{" selected" if str(p["id"]) == pid else ""}>{esc(p["nama"])}</option>'
        for p in pegawai)
    form = (f'<div class="card"><form class="row" method="get"><label>Pegawai<select name="pegawai" required>{opts}'
            f'</select></label><button>Lihat</button></form></div>')
    p = next((x for x in pegawai if str(x["id"]) == pid), None)
    if not p:
        return page(req, "Riwayat & Sisa Cuti", form)
    tahun = date.today().year
    cuti = db.execute("SELECT * FROM cuti WHERE pegawai_id=? AND status='Disetujui' ORDER BY mulai DESC",
                      (p["id"],)).fetchall()
    terpakai = sum(hari_di_tahun(c["mulai"], c["selesai"], tahun) for c in cuti)
    sisa = p["jatah_tahunan"] - terpakai
    ringkas = (f'<div class="card"><b>{esc(p["nama"])}</b> · tahun {tahun}<br>'
               f'Jatah tahunan: <b>{p["jatah_tahunan"]}</b> hari · Terpakai: <b>{terpakai}</b> hari · '
               f'Sisa: <b class="{"err" if sisa < 0 else ""}">{sisa}</b> hari'
               f'{" (melebihi jatah)" if sisa < 0 else ""}</div>')
    if not cuti:
        riwayat = '<div class="msg info">Belum pernah cuti (riwayat kosong).</div>'
    else:
        riwayat = tabel(["Periode", "Durasi (hari)", "Jenis Cuti"],
                        [[f'{tgl(c["mulai"])} – {tgl(c["selesai"])}', str(durasi(c["mulai"], c["selesai"])),
                          esc(c["jenis"])] for c in cuti])
    return page(req, "Riwayat & Sisa Cuti", form + ringkas + "<h2>Riwayat cuti disetujui</h2>" + riwayat)


# --------------------------------------------------------------------------- Pencarian: US-03
@route("GET", "/penerbangan", PENGGUNA)
def cari_penerbangan(req):
    asal, tanggal = req.q("asal"), req.q("tanggal")
    tujuan_dipilih = [t for t in req.ql("tujuan") if t in KOTA]
    cek = "".join(f'<label style="flex-direction:row;align-items:center;gap:.35rem;color:inherit">'
                  f'<input type="checkbox" name="tujuan" value="{k}"{" checked" if k in tujuan_dipilih else ""}>{esc(v)}</label>'
                  for k, v in KOTA.items())
    form = (f'<div class="card"><form method="get"><div class="row">'
            f'<label>Asal<select name="asal" required>{opsi_kota(asal, "-- pilih --")}</select></label>'
            f'<label>Tanggal<input type="date" name="tanggal" value="{esc(tanggal)}" min="{date.today()}" required></label>'
            f'</div><p class="muted" style="margin-bottom:.3rem">Tujuan (boleh lebih dari satu):</p>'
            f'<div class="row" style="gap:1rem">{cek}</div><p><button>Cari</button></p></form></div>')
    if "tanggal" not in req.query:
        return page(req, "Cari Jadwal Penerbangan", form)
    err = None
    d, e = validasi_tanggal_depan(tanggal)
    if asal not in KOTA:
        err = "Pilih kota asal."
    elif not tujuan_dipilih:
        err = "Pilih minimal satu tujuan."
    elif e:
        err = e
    if err:
        return page(req, "Cari Jadwal Penerbangan", form + f'<div class="msg err">{err}</div>')
    out = ""
    for tj in tujuan_dipilih:
        if tj == asal:
            continue
        rows = req.db.execute("SELECT * FROM flights WHERE asal=? AND tujuan=? AND tanggal=? ORDER BY jam_berangkat",
                              (asal, tj, d.isoformat())).fetchall()
        out += f"<h2>{esc(kota_label(asal))} → {esc(kota_label(tj))} · {tgl(d.isoformat())}</h2>"
        out += tabel_flights(rows, link_pesan) if rows else '<div class="msg info">Penerbangan tidak tersedia</div>'
    return page(req, "Cari Jadwal Penerbangan", form + (out or '<div class="msg err">Tujuan sama dengan asal.</div>'))


# --------------------------------------------------------------------------- Multi destinasi: US-04
@route("GET", "/multi", MEMBER)
def multi(req):
    asal = req.q("asal")
    baris = ""
    for i in range(1, 5):
        baris += (f'<div class="row" style="margin-bottom:.5rem"><b style="padding-bottom:.5rem">{i}.</b>'
                  f'<label>Tujuan<select name="tujuan{i}">{opsi_kota(req.q(f"tujuan{i}"), "-- kosongkan --")}</select></label>'
                  f'<label>Tanggal<input type="date" name="tanggal{i}" value="{esc(req.q(f"tanggal{i}"))}" '
                  f'min="{date.today()}"></label></div>')
    form = (f'<div class="card"><form method="get"><div class="row" style="margin-bottom:.8rem">'
            f'<label>Lokasi keberangkatan<select name="asal" required>{opsi_kota(asal, "-- pilih --")}</select></label></div>'
            f'<p class="muted">Isi tujuan secara berurutan (maksimal 4). Tanggal tiap segmen tidak boleh lebih awal dari segmen sebelumnya.</p>'
            f'{baris}<button>Cari</button></form></div>')
    if "asal" not in req.query:
        return page(req, "Penerbangan Multi-Destinasi", form)
    segs, prev, prev_tgl, err = [], asal, None, None
    if asal not in KOTA:
        err = "Pilih lokasi keberangkatan."
    for i in range(1, 5):
        tj, ts = req.q(f"tujuan{i}"), req.q(f"tanggal{i}")
        if not tj and not ts:
            continue
        d, e = validasi_tanggal_depan(ts)
        if tj not in KOTA:
            err = err or f"Segmen {i}: pilih tujuan."
        elif e:
            err = err or f"Segmen {i}: {e}"
        elif tj == prev:
            err = err or f"Segmen {i}: tujuan tidak boleh sama dengan lokasi sebelumnya."
        elif prev_tgl and d < prev_tgl:
            err = err or f"Segmen {i}: tanggal tidak boleh lebih awal dari segmen sebelumnya."
        else:
            segs.append((prev, tj, d))
            prev, prev_tgl = tj, d
    if not segs and not err:
        err = "Isi minimal satu tujuan."
    if err:
        return page(req, "Penerbangan Multi-Destinasi", form + f'<div class="msg err">{err}</div>')
    out, total, lengkap = "", 0, True
    for n, (a, b, d) in enumerate(segs, 1):
        rows = req.db.execute("SELECT * FROM flights WHERE asal=? AND tujuan=? AND tanggal=? ORDER BY harga",
                              (a, b, d.isoformat())).fetchall()
        out += f"<h2>Segmen {n}: {esc(kota_label(a))} → {esc(kota_label(b))} · {tgl(d.isoformat())}</h2>"
        ada = [r for r in rows if r["kursi"] > 0]
        if rows:
            out += tabel_flights(rows, link_pesan)
        if ada:
            total += ada[0]["harga"]
            out += f'<p class="muted">Termurah segmen ini: {rp(ada[0]["harga"])} ({esc(ada[0]["kode"])})</p>'
        else:
            lengkap = False
            out += '<div class="msg info">Penerbangan tidak tersedia</div>'
    ket = "" if lengkap else " (belum lengkap: ada segmen tanpa penerbangan)"
    out += f'<div class="card"><b>Total harga (opsi termurah tiap segmen){ket}:</b> <span class="big">{rp(total)}</span></div>'
    return page(req, "Penerbangan Multi-Destinasi", form + out)


# --------------------------------------------------------------------------- Bandingkan: US-05
@route("GET", "/bandingkan", MEMBER)
def bandingkan(req):
    asal, tujuan, tanggal = req.q("asal"), req.q("tujuan"), req.q("tanggal")
    jd, js, urut = req.q("jam_dari"), req.q("jam_sampai"), req.q("urut") or "harga"
    form = (f'<div class="card"><form class="row" method="get">'
            f'<label>Asal<select name="asal" required>{opsi_kota(asal, "-- pilih --")}</select></label>'
            f'<label>Tujuan<select name="tujuan" required>{opsi_kota(tujuan, "-- pilih --")}</select></label>'
            f'<label>Tanggal<input type="date" name="tanggal" value="{esc(tanggal)}" min="{date.today()}" required></label>'
            f'<label>Berangkat dari jam<input type="time" name="jam_dari" value="{esc(jd)}"></label>'
            f'<label>Sampai jam<input type="time" name="jam_sampai" value="{esc(js)}"></label>'
            f'<label>Urutkan<select name="urut"><option value="harga"{" selected" if urut == "harga" else ""}>Harga termurah</option>'
            f'<option value="jam"{" selected" if urut == "jam" else ""}>Jam keberangkatan</option></select></label>'
            f'<button>Cari</button></form></div>')
    if "tanggal" not in req.query:
        return page(req, "Bandingkan Penerbangan", form)
    d, e = validasi_tanggal_depan(tanggal)
    err = ("Pilih asal dan tujuan yang berbeda." if asal not in KOTA or tujuan not in KOTA or asal == tujuan else e)
    if err:
        return page(req, "Bandingkan Penerbangan", form + f'<div class="msg err">{err}</div>')
    sql, par = "SELECT * FROM flights WHERE asal=? AND tujuan=? AND tanggal=?", [asal, tujuan, d.isoformat()]
    if parse_time(jd):
        sql += " AND jam_berangkat>=?"
        par.append(parse_time(jd))
    if parse_time(js):
        sql += " AND jam_berangkat<=?"
        par.append(parse_time(js))
    sql += " ORDER BY " + ("jam_berangkat, harga" if urut == "jam" else "harga, jam_berangkat")
    rows = req.db.execute(sql, par).fetchall()
    if not rows:
        return page(req, "Bandingkan Penerbangan", form + '<div class="msg info">Penerbangan tidak tersedia</div>')
    daftar = (f'<form method="get" action="/bandingkan/hasil">{tabel_flights(rows, None, "id")}'
              f'<p><button>Bandingkan yang dicentang (minimal 2, maksimal 4)</button></p></form>')
    return page(req, "Bandingkan Penerbangan", form + daftar)


@route("GET", "/bandingkan/hasil", MEMBER)
def bandingkan_hasil(req):
    ids = []
    for v in req.ql("id")[:4]:
        if v.isdigit():
            ids.append(int(v))
    back = '<a class="btn ghost" href="javascript:history.back()">Kembali</a>'
    if len(ids) < 2:
        return page(req, "Perbandingan Penerbangan", f'<div class="msg err">Pilih minimal dua penerbangan.</div>{back}')
    fl = [req.db.execute("SELECT * FROM flights WHERE id=?", (i,)).fetchone() for i in ids]
    fl = [f for f in fl if f]
    if len(fl) < 2:
        return page(req, "Perbandingan Penerbangan", f'<div class="msg err">Penerbangan tidak ditemukan.</div>{back}')
    termurah = min(f["harga"] for f in fl)

    def jumlah(f):
        return req.db.execute("SELECT COUNT(*) FROM flights WHERE asal=? AND tujuan=? AND tanggal=? AND kursi>0",
                              (f["asal"], f["tujuan"], f["tanggal"])).fetchone()[0]

    baris = [
        ("Maskapai", [esc(f["maskapai"]) for f in fl]),
        ("Kode", [esc(f["kode"]) for f in fl]),
        ("Rute", [f'{esc(kota_label(f["asal"]))} → {esc(kota_label(f["tujuan"]))}' for f in fl]),
        ("Tanggal", [tgl(f["tanggal"]) for f in fl]),
        ("Jam berangkat", [f["jam_berangkat"] for f in fl]),
        ("Jam tiba", [f["jam_tiba"] for f in fl]),
        ("Harga", [f'<span class="{"best" if f["harga"] == termurah else ""}">{rp(f["harga"])}'
                   f'{" (termurah)" if f["harga"] == termurah else ""}</span>' for f in fl]),
        ("Kursi tersisa", [str(f["kursi"]) for f in fl]),
        ("Penerbangan tersedia di rute & tanggal ini", [str(jumlah(f)) for f in fl]),
        ("", [link_pesan(f) for f in fl]),
    ]
    head = "<tr><th></th>" + "".join(f"<th>Opsi {i}</th>" for i in range(1, len(fl) + 1)) + "</tr>"
    body = "".join(f"<tr><td>{esc(l)}</td>" + "".join(f"<td>{c}</td>" for c in cs) + "</tr>" for l, cs in baris)
    return page(req, "Perbandingan Penerbangan", f'<div class="scroll"><table class="cmp"><thead>{head}</thead>'
                f'<tbody>{body}</tbody></table></div><p>{back}</p>')


# --------------------------------------------------------------------------- Pesan: US-06
@route("GET", r"/pesan/(\d+)", PENGGUNA)
def pesan_form(req, fid):
    f = req.db.execute("SELECT * FROM flights WHERE id=?", (fid,)).fetchone()
    if not f:
        return page(req, "Pesan Tiket", '<div class="msg err">Penerbangan tidak ditemukan.</div>', "404 Not Found")
    detail = (f'<div class="card"><table><tbody>'
              f'<tr><td>Penerbangan</td><td><b>{esc(f["kode"])}</b></td></tr><tr><td>Maskapai</td><td>{esc(f["maskapai"])}</td></tr>'
              f'<tr><td>Rute</td><td>{esc(kota_label(f["asal"]))} → {esc(kota_label(f["tujuan"]))}</td></tr>'
              f'<tr><td>Tanggal</td><td>{tgl(f["tanggal"])}</td></tr>'
              f'<tr><td>Berangkat / Tiba</td><td>{f["jam_berangkat"]} / {f["jam_tiba"]}</td></tr>'
              f'<tr><td>Harga</td><td><b>{rp(f["harga"])}</b></td></tr></tbody></table></div>')
    if f["tanggal"] < date.today().isoformat():
        return page(req, "Pesan Tiket", detail + '<div class="msg err">Penerbangan ini sudah lewat dan tidak dapat dipesan.</div>')
    if f["kursi"] <= 0:
        return page(req, "Pesan Tiket", detail + '<div class="msg err">Maaf, kursi penerbangan ini sudah habis dan tidak dapat dipesan.</div>')
    return page(req, "Pesan Tiket", detail + f'<form method="post">{csrf(req)}<button>Konfirmasi Pesanan</button> '
                f'<a class="btn ghost" href="javascript:history.back()">Batal</a></form>')


@route("POST", r"/pesan/(\d+)", PENGGUNA)
def pesan_post(req, fid):
    bid, err = create_booking(req.db, req.user["id"], int(fid), req.user["id"])
    if err:
        flash(req, {"habis": "Maaf, kursi penerbangan ini sudah habis dan tidak dapat dipesan.",
                    "lewat": "Penerbangan ini sudah lewat dan tidak dapat dipesan.",
                    "tidak_ada": "Penerbangan tidak ditemukan."}[err], "err")
        return redirect(f"/pesan/{fid}")
    kirim_eticket(req.db, bid)
    ref = req.db.execute("SELECT ref FROM bookings WHERE id=?", (bid,)).fetchone()["ref"]
    flash(req, "Pesanan berhasil dibuat. Silakan simpan booking reference Anda.", "ok")
    return redirect(f"/booking/{ref}")


@route("GET", r"/booking/([A-Z0-9]+)", "login")
def booking_detail(req, ref):
    b = req.db.execute(BOOKING_SQL + " WHERE b.ref=?", (ref,)).fetchone()
    u = req.user
    if not b or not (u["role"] == "manajer_travel" or b["user_id"] == u["id"] or b["created_by"] == u["id"]):
        return page(req, "Booking", '<div class="msg err">Booking tidak ditemukan.</div>', "404 Not Found")
    dibuat = "" if b["created_by"] == b["user_id"] else f'<tr><td>Dipesan oleh</td><td>{esc(b["pembuat"])} (Manajer Travel)</td></tr>'
    val = (f'Sudah divalidasi atas nama {esc(b["validated_nama"] or "")} ({esc(b["validated_at"] or "")})'
           if b["validated"] else "Belum divalidasi")
    isi = (f'<div class="card"><div class="muted">Booking reference</div><div class="big">{esc(b["ref"])}</div>'
           f'<p>Status: {badge(b["status"])}</p></div>'
           f'<div class="card"><table><tbody><tr><td>Pemesan</td><td>{esc(b["pemesan"])}</td></tr>{dibuat}'
           f'<tr><td>Penerbangan</td><td>{esc(b["maskapai"])} {esc(b["kode"])}</td></tr>'
           f'<tr><td>Rute</td><td>{esc(kota_label(b["asal"]))} → {esc(kota_label(b["tujuan"]))}</td></tr>'
           f'<tr><td>Tanggal</td><td>{tgl(b["tanggal"])}, {b["jam_berangkat"]} – {b["jam_tiba"]}</td></tr>'
           f'<tr><td>Harga</td><td>{rp(b["harga"])}</td></tr><tr><td>Validasi tiket</td><td>{val}</td></tr>'
           f'<tr><td>E-tiket email</td><td>{status_email(req.db, b["id"])}</td></tr></tbody></table></div>')
    return page(req, "Detail Booking", isi)


# --------------------------------------------------------------------------- Batal: US-07, Tiket: US-12
def dapat_dibatalkan(b):
    return b["status"] != "Dibatalkan" and b["tanggal"] >= date.today().isoformat()


@route("GET", "/pesanan", PENGGUNA)
def pesanan(req):
    rows = req.db.execute(BOOKING_SQL + " WHERE b.user_id=? ORDER BY f.tanggal, f.jam_berangkat", (req.user["id"],)).fetchall()
    aktif = [b for b in rows if dapat_dibatalkan(b)]
    lain = [b for b in rows if not dapat_dibatalkan(b)]
    head = ["Booking", "Penerbangan", "Rute", "Tanggal", "Harga", "Status"]

    def r(b, aksi):
        c = [f'<a href="/booking/{b["ref"]}">{esc(b["ref"])}</a>', f'{esc(b["maskapai"])} {esc(b["kode"])}',
             f'{esc(b["asal"])} → {esc(b["tujuan"])}', f'{tgl(b["tanggal"])} {b["jam_berangkat"]}', rp(b["harga"]),
             badge(b["status"])]
        return c + ([f'<a class="btn danger" href="/batal/{b["ref"]}">Batalkan</a>'] if aksi else [])

    out = "<h2>Pesanan yang dapat dibatalkan</h2>"
    out += (tabel(head + ["Aksi"], [r(b, True) for b in aktif]) if aktif
            else '<div class="msg info">Tidak ada pesanan yang dapat dibatalkan.</div>')
    if lain:
        out += "<h2>Pesanan lainnya</h2>" + tabel(head, [r(b, False) for b in lain])
    return page(req, "Pesanan Saya", out)


def ambil_pesanan_milik(req, ref):
    b = req.db.execute(BOOKING_SQL + " WHERE b.ref=? AND b.user_id=?", (ref, req.user["id"])).fetchone()
    return b if b and dapat_dibatalkan(b) else None


@route("GET", r"/batal/([A-Z0-9]+)", PENGGUNA)
def batal_konfirmasi(req, ref):
    b = ambil_pesanan_milik(req, ref)
    if not b:
        return page(req, "Batalkan Pesanan", '<div class="msg err">Pesanan ini tidak dapat dibatalkan.</div>'
                    '<a class="btn ghost" href="/pesanan">Kembali</a>', "400 Bad Request")
    return page(req, "Batalkan Pesanan",
                f'<div class="card"><p>Anda yakin ingin membatalkan pesanan <b>{esc(b["ref"])}</b> '
                f'({esc(b["asal"])} → {esc(b["tujuan"])}, {tgl(b["tanggal"])})?</p>'
                f'<form method="post">{csrf(req)}<button class="danger">Ya, batalkan</button> '
                f'<a class="btn ghost" href="/pesanan">Tidak</a></form></div>')


@route("POST", r"/batal/([A-Z0-9]+)", PENGGUNA)
def batal_post(req, ref):
    b = ambil_pesanan_milik(req, ref)
    if not b:
        flash(req, "Pesanan ini tidak dapat dibatalkan.", "err")
        return redirect("/pesanan")
    req.db.execute("UPDATE bookings SET status='Dibatalkan' WHERE id=?", (b["id"],))
    req.db.execute("UPDATE flights SET kursi=kursi+1 WHERE id=?", (b["flight_id"],))
    flash(req, f"Pesanan {ref} dibatalkan. Kursi dikembalikan ke ketersediaan.", "ok")
    return redirect("/pesanan")


@route("GET", "/tiket", PENGGUNA)
def tiket(req):
    rows = req.db.execute(BOOKING_SQL + " WHERE b.user_id=? ORDER BY b.id DESC", (req.user["id"],)).fetchall()
    if not rows:
        return page(req, "Tiket Saya", '<div class="msg info">Belum ada tiket.</div>')
    t = tabel(["Booking", "Penerbangan", "Rute", "Tanggal", "Status", "Dipesan oleh", "E-tiket email"],
              [[f'<a href="/booking/{b["ref"]}">{esc(b["ref"])}</a>', f'{esc(b["maskapai"])} {esc(b["kode"])}',
                f'{esc(b["asal"])} → {esc(b["tujuan"])}', f'{tgl(b["tanggal"])} {b["jam_berangkat"]}',
                badge(b["status"]), "Saya" if b["created_by"] == b["user_id"] else esc(b["pembuat"]),
                status_email(req.db, b["id"])] for b in rows])
    return page(req, "Tiket Saya", t)


# --------------------------------------------------------------------------- Profil & validasi: US-08, US-09
PROFIL_FIELDS = [("nama_lengkap", "Nama lengkap (sesuai identitas)", "text"), ("nik", "NIK", "text"),
                 ("no_hp", "No. HP", "text"), ("tanggal_lahir", "Tanggal lahir", "date"), ("alamat", "Alamat", "text")]


def ambil_profil(db, uid):
    p = db.execute("SELECT * FROM profil WHERE user_id=?", (uid,)).fetchone()
    if not p:
        db.execute("INSERT INTO profil(user_id) VALUES(?)", (uid,))
        p = db.execute("SELECT * FROM profil WHERE user_id=?", (uid,)).fetchone()
    return p


def form_profil(req, nilai, kosong):
    isi = ""
    for k, label, tipe in PROFIL_FIELDS:
        tanda = '<span class="req">wajib diisi</span>' if k in kosong else ""
        isi += (f'<label style="margin-bottom:.7rem">{label} {tanda}<input type="{tipe}" name="{k}" '
                f'value="{esc(nilai.get(k, ""))}"{" style=border-color:#b42318" if k in kosong else ""}></label>')
    return f'<div class="card"><form method="post">{csrf(req)}{isi}<button>Simpan Profil</button></form></div>'


@route("GET", "/profil", MEMBER)
def profil(req):
    p = ambil_profil(req.db, req.user["id"])
    st = badge("Disetujui").replace("Disetujui", "Lengkap") if p["lengkap"] else badge("Diajukan").replace("Diajukan", "Tidak lengkap")
    kosong = [k for k, _, _ in PROFIL_FIELDS if not (p[k] or "").strip()]
    return page(req, "Profil Saya", f"<p>Status kelengkapan: {st}</p>" + form_profil(req, dict(p), kosong if "cek" in req.query else []))


@route("POST", "/profil", MEMBER)
def profil_post(req):
    nilai = {k: req.f(k) for k, _, _ in PROFIL_FIELDS}
    if nilai["tanggal_lahir"] and not parse_date(nilai["tanggal_lahir"]):
        nilai["tanggal_lahir"] = ""
    kosong = [k for k, v in nilai.items() if not v]
    lengkap = 0 if kosong else 1
    ambil_profil(req.db, req.user["id"])
    req.db.execute("UPDATE profil SET nama_lengkap=?,nik=?,no_hp=?,tanggal_lahir=?,alamat=?,lengkap=? WHERE user_id=?",
                   (*[nilai[k] for k, _, _ in PROFIL_FIELDS], lengkap, req.user["id"]))
    if lengkap:
        flash(req, "Profil tersimpan dan sudah lengkap.", "ok")
        return redirect("/profil")
    flash(req, "Profil tersimpan, tetapi belum lengkap. Kolom wajib yang kosong ditandai.", "err")
    st = badge("Diajukan").replace("Diajukan", "Tidak lengkap")
    return page(req, "Profil Saya", f"<p>Status kelengkapan: {st}</p>" + form_profil(req, nilai, kosong))


@route("GET", "/validasi", MEMBER)
def validasi(req):
    rows = req.db.execute(BOOKING_SQL + " WHERE b.user_id=? AND b.status!='Dibatalkan' ORDER BY f.tanggal",
                          (req.user["id"],)).fetchall()
    p = ambil_profil(req.db, req.user["id"])
    info = ("" if p["lengkap"] else '<div class="msg err">Profil Anda belum lengkap. '
            '<a href="/profil?cek=1">Lengkapi profil</a> sebelum memvalidasi tiket.</div>')
    if not rows:
        return page(req, "Validasi Tiket", info + '<div class="msg info">Belum ada tiket untuk divalidasi.</div>')
    t = tabel(["Booking", "Rute", "Tanggal", "Status Booking", "Validasi", "Aksi"],
              [[f'<a href="/booking/{b["ref"]}">{esc(b["ref"])}</a>', f'{esc(b["asal"])} → {esc(b["tujuan"])}',
                tgl(b["tanggal"]), badge(b["status"]),
                badge("Disetujui").replace("Disetujui", "Tervalidasi") if b["validated"] else "Belum",
                "—" if b["validated"] else
                f'<form method="post" action="/validasi/{b["ref"]}" style="margin:0">{csrf(req)}<button>Validasi</button></form>']
               for b in rows])
    return page(req, "Validasi Tiket", info + t)


@route("POST", r"/validasi/([A-Z0-9]+)", MEMBER)
def validasi_post(req, ref):
    b = req.db.execute("SELECT * FROM bookings WHERE ref=? AND user_id=? AND status!='Dibatalkan'",
                       (ref, req.user["id"])).fetchone()
    if not b:
        flash(req, "Tiket tidak ditemukan.", "err")
        return redirect("/validasi")
    p = ambil_profil(req.db, req.user["id"])
    if not p["lengkap"]:
        flash(req, "Validasi ditolak: data profil tidak lengkap. Lengkapi profil terlebih dahulu.", "err")
        return redirect("/profil?cek=1")
    req.db.execute("UPDATE bookings SET validated=1, validated_at=?, validated_nama=? WHERE id=?",
                   (datetime.now().strftime("%Y-%m-%d %H:%M"), p["nama_lengkap"], b["id"]))
    flash(req, f"Tiket {ref} berhasil divalidasi atas nama {p['nama_lengkap']}.", "ok")
    return redirect("/validasi")


# --------------------------------------------------------------------------- Manajer Travel: US-10, US-11, US-13
@route("GET", "/manajer/booking", MANAJER)
def manajer_booking(req):
    st = req.q("status")
    opts = '<option value="">Semua</option>' + "".join(
        f'<option{" selected" if s == st else ""}>{s}</option>' for s in STATUS_BOOKING)
    form = (f'<div class="card"><form class="row" method="get"><label>Status<select name="status">{opts}</select></label>'
            f'<button>Filter</button></form></div>')
    sql, par = BOOKING_SQL, []
    if st in STATUS_BOOKING:
        sql += " WHERE b.status=?"
        par.append(st)
    rows = req.db.execute(sql + " ORDER BY b.id DESC", par).fetchall()
    if not rows:
        return page(req, "Semua Booking", form + f'<div class="msg info">Tidak ada data booking{" dengan status " + esc(st) if st else ""}.</div>')
    t = tabel(["Booking", "Pemesan", "Penerbangan", "Rute", "Tanggal", "Harga", "Status", "Dibuat oleh"],
              [[f'<a href="/booking/{b["ref"]}">{esc(b["ref"])}</a>', esc(b["pemesan"]), f'{esc(b["maskapai"])} {esc(b["kode"])}',
                f'{esc(b["asal"])} → {esc(b["tujuan"])}', tgl(b["tanggal"]), rp(b["harga"]), badge(b["status"]),
                esc(b["pembuat"])] for b in rows])
    return page(req, "Semua Booking", form + f'<p class="muted">{len(rows)} booking</p>' + t)


@route("GET", "/manajer/pesan", MANAJER)
def manajer_pesan(req):
    users = req.db.execute("SELECT * FROM users WHERE role IN ('pengguna','member') ORDER BY nama").fetchall()
    uid, asal, tujuan, tanggal = req.q("user"), req.q("asal"), req.q("tujuan"), req.q("tanggal")
    uopts = '<option value="">-- pilih pengguna --</option>' + "".join(
        f'<option value="{u["id"]}"{" selected" if str(u["id"]) == uid else ""}>{esc(u["nama"])} ({ROLE_LABEL[u["role"]]})</option>'
        for u in users)
    form = (f'<div class="card"><form class="row" method="get">'
            f'<label>Pesan atas nama<select name="user" required>{uopts}</select></label>'
            f'<label>Asal<select name="asal" required>{opsi_kota(asal, "-- pilih --")}</select></label>'
            f'<label>Tujuan<select name="tujuan" required>{opsi_kota(tujuan, "-- pilih --")}</select></label>'
            f'<label>Tanggal<input type="date" name="tanggal" value="{esc(tanggal)}" min="{date.today()}" required></label>'
            f'<button>Cari</button></form></div>')
    if "tanggal" not in req.query:
        return page(req, "Pesan Tiket untuk Pengguna", form)
    d, e = validasi_tanggal_depan(tanggal)
    target = next((u for u in users if str(u["id"]) == uid), None)
    err = ("Pilih pengguna." if not target else "Pilih asal dan tujuan yang berbeda."
           if asal not in KOTA or tujuan not in KOTA or asal == tujuan else e)
    if err:
        return page(req, "Pesan Tiket untuk Pengguna", form + f'<div class="msg err">{err}</div>')
    rows = req.db.execute("SELECT * FROM flights WHERE asal=? AND tujuan=? AND tanggal=? ORDER BY jam_berangkat",
                          (asal, tujuan, d.isoformat())).fetchall()
    if not rows:
        return page(req, "Pesan Tiket untuk Pengguna", form + '<div class="msg info">Penerbangan tidak tersedia</div>')

    def aksi(f):
        if f["kursi"] <= 0:
            return '<span class="muted">Penuh</span>'
        return (f'<form method="post" action="/manajer/pesan/{f["id"]}" style="margin:0">{csrf(req)}'
                f'<input type="hidden" name="user" value="{target["id"]}"><button>Pesan untuk {esc(target["nama"].split()[0])}</button></form>')

    return page(req, "Pesan Tiket untuk Pengguna", form + tabel_flights(rows, aksi))


@route("POST", r"/manajer/pesan/(\d+)", MANAJER)
def manajer_pesan_post(req, fid):
    u = req.db.execute("SELECT * FROM users WHERE id=? AND role IN ('pengguna','member')", (req.f("user"),)).fetchone()
    if not u:
        flash(req, "Pengguna tidak valid.", "err")
        return redirect("/manajer/pesan")
    bid, err = create_booking(req.db, u["id"], int(fid), req.user["id"])
    if err:
        flash(req, {"habis": "Kursi penerbangan ini sudah habis.", "lewat": "Penerbangan sudah lewat.",
                    "tidak_ada": "Penerbangan tidak ditemukan."}[err], "err")
        return redirect("/manajer/pesan")
    st = kirim_eticket(req.db, bid)
    ref = req.db.execute("SELECT ref FROM bookings WHERE id=?", (bid,)).fetchone()["ref"]
    flash(req, f"Booking {ref} dibuat atas nama {u['nama']}. Status e-tiket: {st}.", "ok")
    return redirect(f"/booking/{ref}")


@route("GET", "/manajer/otorisasi", MANAJER)
def otorisasi_form(req):
    return page(req, "Otorisasi Booking", otorisasi_form_html(req))


def otorisasi_form_html(req, nilai=""):
    return (f'<div class="card"><form class="row" method="post">{csrf(req)}'
            f'<label>Booking reference<input name="ref" value="{esc(nilai)}" placeholder="mis. BKAB12CD" required autofocus></label>'
            f'<button>Verifikasi</button></form></div>')


@route("POST", "/manajer/otorisasi", MANAJER)
def otorisasi_post(req):
    ref = req.f("ref").upper()
    b = req.db.execute(BOOKING_SQL + " WHERE b.ref=?", (ref,)).fetchone()
    if not b:
        return page(req, "Otorisasi Booking",
                    '<div class="msg err">Gagal: kode booking reference tidak sesuai. Silakan masukkan ulang.</div>'
                    + otorisasi_form_html(req))
    return page(req, "Otorisasi Booking",
                f'<div class="msg ok">Otorisasi berhasil.</div>{otorisasi_form_html(req)}'
                f'<div class="card"><b>{esc(b["ref"])}</b> · Status: {badge(b["status"])}<br>'
                f'{esc(b["pemesan"])} · {esc(b["maskapai"])} {esc(b["kode"])} · {esc(b["asal"])} → {esc(b["tujuan"])} · '
                f'{tgl(b["tanggal"])}<br><a href="/booking/{b["ref"]}">Lihat detail</a></div>')


# --------------------------------------------------------------------------- WSGI app
def app(environ, start_response):
    req = Req(environ)
    db = connect()
    req.db = db
    try:
        if req.session:
            req.user = db.execute("SELECT * FROM users WHERE id=?", (req.session["user_id"],)).fetchone()
        result = None
        for method, rx, roles, fn in ROUTES:
            m = rx.match(req.path)
            if not m or method != req.method:
                continue
            if roles is not None:
                if not req.user:
                    result = redirect("/login")
                    break
                if roles != "login" and req.user["role"] not in roles:
                    result = forbidden(req)
                    break
            if req.method == "POST" and req.path != "/login":
                if not req.session or not hmac.compare_digest(req.f("_csrf"), req.session["csrf"]):
                    result = page(req, "Permintaan ditolak", '<div class="msg err">Token keamanan tidak valid. '
                                  'Muat ulang halaman lalu coba lagi.</div>', "400 Bad Request")
                    break
            result = fn(req, *m.groups())
            break
        if result is None:
            result = (page(req, "Tidak ditemukan", '<a class="btn ghost" href="/">Ke beranda</a>', "404 Not Found")
                      if req.user else redirect("/login"))
        db.commit()
    except Exception:
        db.rollback()
        traceback.print_exc()
        result = resp("<h1>Terjadi kesalahan pada server</h1><p>Lihat jendela terminal untuk detailnya.</p>",
                      "500 Internal Server Error")
    finally:
        db.close()
    status, headers, body = result
    start_response(status, headers)
    return [body]


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    if "--reset" in sys.argv:
        for p in (DB_PATH,):
            if os.path.exists(p):
                os.remove(p)
        print("Database direset.")
    init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    srv = make_server(host, port, app, server_class=ThreadedServer)
    print(f"\nAplikasi berjalan.  Buka:  http://127.0.0.1:{port}   (atau http://localhost:{port})")
    print("Tekan Ctrl+C untuk berhenti.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBerhenti.")


if __name__ == "__main__":
    main()
