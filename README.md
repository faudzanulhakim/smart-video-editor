# Auto Video Editor

Tools otomatisasi edit video, tersedia dalam 2 bentuk:

- **Web app** (disarankan) — upload video lewat browser, centang fitur yang mau dipakai, download hasil.
- **CLI/batch** — proses banyak video sekaligus dari folder, tanpa UI.

Fitur:
- Potong bagian sepi/diam otomatis
- Subtitle otomatis dari suara (speech-to-text)
- Watermark & intro/outro
- **AI**: judul + caption otomatis dari isi video, dan pemilihan bagian highlight otomatis
  (dipotong jadi video pendek terpisah) — pakai Claude API

## Setup AI (wajib untuk fitur judul/caption & highlight otomatis)

Fitur AI pakai standar OpenAI-compatible API, jadi bisa diarahkan ke provider AI
mana saja yang kompatibel (termasuk yang gratis) tanpa ubah kode -- cukup set 3
env var ini:

- `AI_API_KEY` (wajib) -- API key dari provider pilihanmu
- `AI_BASE_URL` (opsional, default: Cerebras -- gratis, ambil di https://cloud.cerebras.ai)
- `AI_MODEL` (opsional, default: `gpt-oss-120b`)

Tanpa `AI_API_KEY` ini, fitur potong-sepi/subtitle/watermark tetap jalan normal — hanya fitur AI yang nonaktif.

Provider lain yang bisa dipakai (tinggal ganti `AI_BASE_URL`/`AI_MODEL`/`AI_API_KEY`, tanpa ubah kode):
- **Cerebras** (default) — `https://api.cerebras.ai/v1` — gratis, daftar di cloud.cerebras.ai
- **Groq** — `https://api.groq.com/openai/v1` — gratis, daftar di console.groq.com
- **OpenRouter** — `https://openrouter.ai/api/v1` — banyak model gratis
- **Together.ai** — `https://api.together.xyz/v1`

## Web App (Docker) — disarankan

```bash
export AI_API_KEY=xxxxxxxxxx
docker compose up --build
```

Buka **http://localhost:8000** di browser → upload video → centang fitur yang diinginkan →
klik "Proses Video". Hasil (video final, highlight, judul/caption) langsung tampil di halaman
dan bisa didownload.

---

## CLI / Batch mode

Untuk proses banyak video sekaligus dari folder tanpa UI (belum termasuk fitur AI):

### 1. Instalasi (sekali saja)

```bash
sudo apt install ffmpeg          # Ubuntu/Debian
# atau: brew install ffmpeg      # Mac
# atau download dari https://ffmpeg.org untuk Windows

pip install -r requirements.txt
```

### 2. Atur aturan edit di `config.json`

- **remove_silence** — potong otomatis bagian video yang sepi/diam
- **subtitle** — generate subtitle otomatis dari suara, burn ke video
  - `model_size`: `tiny` (tercepat) s/d `large-v3` (paling akurat, paling berat)
  - `language`: `"id"` untuk Bahasa Indonesia, atau `null` untuk auto-detect
- **intro_outro** — tambahkan video intro/outro tetap di awal/akhir
- **watermark** — tambahkan gambar watermark (logo, dll) di pojok video

### 3. Jalankan

```bash
python auto_video_editor.py --input ./videos_mentah --output ./videos_hasil --config config.json
```

Semua video di folder `videos_mentah` (mp4/mov/mkv/avi/webm/m4v) diproses otomatis sesuai
`config.json`, hasilnya masuk ke `videos_hasil`.

---

## Struktur project

```
core/editor.py       - logika edit video (potong sepi, subtitle, watermark, dll)
core/ai_helper.py     - fitur AI (judul/caption, pemilihan highlight) via OpenAI-compatible API
web/main.py           - backend web app (FastAPI)
web/static/index.html - halaman upload
auto_video_editor.py  - CLI / batch mode
Dockerfile, docker-compose.yml - jalankan sebagai web app
```

## Catatan

- Proses subtitle (speech-to-text) butuh waktu tergantung `model_size` dan panjang video —
  `small` biasanya cukup akurat dan tidak terlalu lambat untuk CPU biasa.
- Job di web app disimpan di memory (cukup untuk pemakaian sendiri/tim kecil). Kalau nanti
  butuh multi-user dengan banyak proses paralel/antrian, bisa dikembangkan pakai queue
  (Redis/Celery) — bilang saja kalau itu dibutuhkan.
