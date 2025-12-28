import os
import numpy as np
import matplotlib.pyplot as plt
from scipy.io import wavfile
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import json
from pathlib import Path
import time # <--- Usaremos el tiempo como "versión" para engañar al caché

app = FastAPI()

# --- CONFIGURACIÓN ---
BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "output"

TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

origins = [
    "http://localhost:4321",              # Para cuando pruebas en tu PC
    "http://localhost:3000",              # Por si usas otro puerto local
    "https://ecualizador5-bandas.vercel.app"  # <--- ¡TU URL DE VERCEL! (Sin barra al final)
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

# --- FUNCIONES ---

def normalize_audio(data):
    if data.dtype == np.int16:
        return data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        return data.astype(np.float32) / 2147483648.0
    elif data.dtype == np.uint8:
        return (data.astype(np.float32) - 128) / 128.0
    elif data.dtype == np.float32 or data.dtype == np.float64:
        return data.astype(np.float32)
    else:
        m = np.max(np.abs(data))
        if m == 0: return data.astype(np.float32)
        return data.astype(np.float32) / m

def apply_fft_equalizer(data_norm, rate, gains):
    N = len(data_norm)
    fft_spectrum = np.fft.rfft(data_norm)
    freqs = np.fft.rfftfreq(N, 1/rate)
    
    mask = np.ones_like(fft_spectrum, dtype=np.float32)
    
    bands_ranges = {
        "60Hz":  (0, 150),
        "250Hz": (150, 600),
        "1kHz":  (600, 2500),
        "4kHz":  (2500, 10000),
        "16kHz": (10000, 22050)
    }

    for band_name, (low, high) in bands_ranges.items():
        if band_name in gains:
            db = gains[band_name]
            factor = 10 ** (db / 20.0)
            indices = np.where((freqs >= low) & (freqs < high))
            mask[indices] *= factor

    processed_fft = fft_spectrum * mask
    processed_audio = np.fft.irfft(processed_fft, n=N)

    return np.clip(processed_audio, -1.0, 1.0)

def generate_spectrogram(data_norm, rate, output_path):
    plt.figure(figsize=(12, 4))
    
    # Configuración "estilo WaveSurfer"
    plt.specgram(data_norm, NFFT=1024, Fs=rate, noverlap=512, cmap='inferno', vmin=-90, vmax=0)
    plt.yscale('symlog', linthresh=700)
    plt.ylim(20, rate/2)
    plt.axis('off')
    
    # Sobreescribimos el archivo si existe
    plt.savefig(output_path, bbox_inches='tight', pad_inches=0, transparent=True)
    plt.close()

# --- ENDPOINT ---

@app.get("/")
async def process_audio(
    request: Request,
    song: UploadFile = File(...),
    voice: UploadFile = File(...),
    gains: str = Form(...)
):
    print("📥 Recibiendo solicitud...")
    eq_gains = json.loads(gains)

    print("📈 Ganancias recibidas:", eq_gains)
    
    # 1. Guardar archivos (Sobreescribimos siempre los mismos 2 archivos temporales)
    song_path = TEMP_DIR / "temp_song.wav"
    voice_path = TEMP_DIR / "temp_voice.wav"

    with open(song_path, "wb") as f:
        f.write(await song.read())
    with open(voice_path, "wb") as f:
        f.write(await voice.read())

    rate_s, data_s = wavfile.read(song_path)
    rate_v, data_v = wavfile.read(voice_path)

    if len(data_s.shape) > 1: data_s = data_s[:, 0]
    if len(data_v.shape) > 1: data_v = data_v[:, 0]

    audio_s_norm = normalize_audio(data_s)
    audio_v_norm = normalize_audio(data_v)

    # 2. Procesar
    print("🎛️ Procesando...")
    processed_song = apply_fft_equalizer(audio_s_norm, rate_s, eq_gains)
    processed_voice = apply_fft_equalizer(audio_v_norm, rate_v, eq_gains)

    # 3. NOMBRES ESTÁTICOS (Sobreescribimos siempre la misma imagen)
    song_out_name = "spec_song_latest.png"
    voice_out_name = "spec_voice_latest.png"
    
    print(f"🎨 Actualizando imágenes: {song_out_name}")
    generate_spectrogram(processed_song, rate_s, OUTPUT_DIR / song_out_name)
    generate_spectrogram(processed_voice, rate_v, OUTPUT_DIR / voice_out_name)

    # 4. EL TRUCO: Agregamos un timestamp a la URL
    # El archivo en disco es el mismo, pero la URL cambia (?t=1708...)
    # esto obliga al navegador a recargar la imagen.
    timestamp = int(time.time())
    base_url = str(request.base_url).rstrip("/")

    return {
        "status": "success",
        "images": {
           "song": f"{base_url}/output/{song_out_name}?t={timestamp}",
           "voice": f"{base_url}/output/{voice_out_name}?t={timestamp}"
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)