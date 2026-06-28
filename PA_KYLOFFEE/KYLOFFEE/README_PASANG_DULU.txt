KYLOFFEE ALL FILES READY

Isi ini sudah lengkap untuk login/register + owner menu management + POS sederhana.

Cara pasang:
1. Extract zip ini.
2. Copy semua isi folder ke root project Flask kamu.
3. Replace file lama kalau diminta.
4. Copy .env.example menjadi .env.
5. Untuk sekarang biar login/register jalan dulu, pastikan di .env:
   DB_FORCE_SQLITE=1
   DB_FALLBACK_SQLITE=1
6. Jalankan:
   pip install -r requirements.txt
   python app.py
7. Buka:
   http://127.0.0.1:5000
8. Buat akun owner di:
   http://127.0.0.1:5000/register/owner
9. Login. Owner akan masuk ke /owner/menu.

Catatan:
- File .env asli tidak ikut dimasukkan karena berisi password/API key.
- Jangan commit .env dan database.db.
- Kalau nanti mau database online bareng teman, baru isi credential TiDB dan ubah DB_FORCE_SQLITE=0.
