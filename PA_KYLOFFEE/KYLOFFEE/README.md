# Kyloffee POS

Project Flask untuk autentikasi Owner/Staff, Owner Menu Management, dan POS sederhana.

## Jalankan lokal

```bash
pip install -r requirements.txt
python app.py
```

Buka `http://127.0.0.1:5000`.

## Database

Default lokal memakai SQLite `database.db`. Untuk menghindari error TiDB saat credential belum benar, pakai:

```env
DB_FORCE_SQLITE=1
DB_FALLBACK_SQLITE=1
```

Kalau ingin database online/shared, isi credential TiDB di `.env`, lalu ubah:

```env
DB_FORCE_SQLITE=0
DB_FALLBACK_SQLITE=0
```

## Akun

- Owner: `/register/owner`
- Staff: `/register/staff`
- Kode staff: `KYLOFFEE-STAFF`
