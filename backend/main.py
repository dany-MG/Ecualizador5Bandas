import os
import numpy as np  # Librería para matemáticas pesadas y manejo de arrays
import matplotlib.pyplot as plt  # Para generar las gráficas (espectrogramas)
from scipy.io import wavfile  # Para leer archivos de audio .wav
from fastapi import FastAPI, UploadFile, File, Form, Request  # El cerebro del servidor web
from fastapi.middleware.cors import CORSMiddleware  # Seguridad para permitir conexiones externas
from fastapi.staticfiles import StaticFiles  # Para poder servir las imágenes generadas al mundo
import json
from pathlib import Path
import time

# Inicializamos la aplicación FastAPI
app = FastAPI()

# --- 1. CONFIGURACIÓN DE CARPETAS ---
# Usamos Path para que funcione igual en Windows, Mac o Linux (Render)
BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp"    # Aquí guardamos los audios que sube el usuario
OUTPUT_DIR = BASE_DIR / "output" # Aquí guardamos las imágenes generadas

# Creamos las carpetas si no existen
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# --- 2. CONFIGURACIÓN DE SEGURIDAD (CORS) ---
# Esto es CRUCIAL. Define quién tiene permiso de hablar con este servidor.
# Usamos ["*"] para permitir que Vercel (y cualquiera) se conecte sin bloqueos.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # Permitir acceso desde cualquier URL (Vercel, Localhost, etc.)
    allow_credentials=False,  # IMPORTANTE: En False si usamos "*", para evitar errores de navegador
    allow_methods=["*"],      # Permitir todos los métodos (GET, POST, etc.)
    allow_headers=["*"],      # Permitir todas las cabeceras
)

# "Montamos" la carpeta output para que sea accesible vía web
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

# --- FUNCIONES AUXILIARES ---

def normalize_audio(data):
    """
    Convierte el audio a números flotantes entre -1.0 y 1.0.
    Esto es necesario porque la FFT funciona mejor con números decimales normalizados.
    Maneja diferentes formatos de entrada (16-bit, 32-bit, etc.).
    """
    if data.dtype == np.int16:
        return data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        return data.astype(np.float32) / 2147483648.0
    elif data.dtype == np.uint8:
        return (data.astype(np.float32) - 128) / 128.0
    elif data.dtype == np.float32 or data.dtype == np.float64:
        return data.astype(np.float32)
    else:
        # Si es un formato desconocido, normalizamos basado en el valor máximo encontrado
        m = np.max(np.abs(data))
        if m == 0: return data.astype(np.float32)
        return data.astype(np.float32) / m

def apply_fft_equalizer(data_norm, rate, gains):
    """
    1. Convierte el audio de Tiempo -> Frecuencia (FFT).
    2. Sube o baja el volumen de frecuencias específicas (Ecualización).
    3. Convierte de Frecuencia -> Tiempo (IFFT) para recuperar el audio.
    """
    N = len(data_norm)

    # A. Transformada de Fourier (Time Domain -> Frequency Domain)
    fft_spectrum = np.fft.rfft(data_norm)
    freqs = np.fft.rfftfreq(N, 1/rate) # Obtenemos qué frecuencia es cada punto del array
    
    # Creamos una "máscara" (filtro) inicializada en 1 (sin cambios)
    mask = np.ones_like(fft_spectrum, dtype=np.float32)
    
    # Definimos los rangos de frecuencia para cada banda del ecualizador
    bands_ranges = {
        "60Hz":  (0, 150),       # Bajos profundos
        "250Hz": (150, 600),     # Graves / Cuerpo
        "1kHz":  (600, 2500),    # Medios (Voz humana)
        "4kHz":  (2500, 10000),  # Agudos / Presencia
        "16kHz": (10000, 22050)  # Brillo / Aire
    }

    # B. Aplicamos las ganancias (dB) del usuario
    for band_name, (low, high) in bands_ranges.items():
        if band_name in gains:
            db = gains[band_name]
            # Fórmula para convertir Decibeles a Factor Multiplicativo
            # Ejemplo: +6dB ≈ multiplicar por 2. -6dB ≈ dividir por 2.
            factor = 10 ** (db / 20.0)

            # Encontramos los índices del array que corresponden a estas frecuencias
            indices = np.where((freqs >= low) & (freqs < high))

            # Aplicamos el factor (volumen) solo a esas frecuencias
            mask[indices] *= factor

    # C. Aplicamos el filtro al espectro original
    processed_fft = fft_spectrum * mask

    # D. Transformada Inversa (Frequency Domain -> Time Domain)
    processed_audio = np.fft.irfft(processed_fft, n=N)

    # Aseguramos que el audio no sature (clipping) manteniéndolo entre -1 y 1
    return np.clip(processed_audio, -1.0, 1.0)

def generate_spectrogram(data_norm, rate, output_path):
    """
    Genera una imagen visual del sonido usando Matplotlib.
    Eje X = Tiempo, Eje Y = Frecuencia, Color = Intensidad.
    """
    plt.figure(figsize=(12, 4))

    # Renderizado 100% sin bordes
    plt.axes([0, 0, 1, 1], frameon=False)
    plt.axis('off')

    # Generar el espectrograma (Mapa de calor)
    plt.specgram(data_norm, NFFT=1024, Fs=rate, noverlap=512, cmap='inferno', vmin=-90, vmax=0)

    # Ajustes visuales: Escala logarítmica para ver mejor las frecuencias bajas
    plt.yscale('symlog', linthresh=700)
    plt.ylim(20, 4000) # Límite igual al Frontend

    # Guardamos la imagen con fondo transparente
    plt.savefig(output_path, bbox_inches=None, pad_inches=0, transparent=True)
    plt.close() # Cerramos la figura para liberar memoria RAM

# --- ENDPOINTS (RUTAS) ---

# 1. RUTA DE BIENVENIDA (GET /)
# Esta es la que responde "Hola" cuando entras al link de Render
@app.get("/")
def read_root():
    """Ruta de prueba para verificar si el servidor está vivo."""
    return {"mensaje": "¡El Backend está VIVO y funcionando! 🚀"}

# 2. RUTA DE PROCESAMIENTO (POST /process)
# Esta es la que usa tu página web para enviar los audios
@app.post("/process") 
async def process_audio(
    request: Request,
    song: UploadFile = File(...), # Recibe archivo 1
    voice: UploadFile = File(...), #Recibe archivo 2
    gains: str = Form(...) # Recibe las ganancias como JSON en texto
):
    print("📥 Recibiendo solicitud...")
    eq_gains = json.loads(gains) # Convertimos el texto JSON a diccionario Python

    # ... (Lógica de guardado y procesamiento) ...
    # Definimos rutas temporales para guardar los audios subidos
    song_path = TEMP_DIR / "temp_song.wav"
    voice_path = TEMP_DIR / "temp_voice.wav"

    # Guardamos los archivos subidos en disco del servidor
    with open(song_path, "wb") as f:
        f.write(await song.read())
    with open(voice_path, "wb") as f:
        f.write(await voice.read())

    # Leemos los archivos con Scipy
    rate_s, data_s = wavfile.read(song_path)
    rate_v, data_v = wavfile.read(voice_path)

    # Convertimos estéreo a mono si es necesario (tomamos solo el canal 0)
    if len(data_s.shape) > 1: data_s = data_s[:, 0]
    if len(data_v.shape) > 1: data_v = data_v[:, 0]

    # Normalizamos el audio para trabajar con flotantes entre -1.0 y 1.0
    audio_s_norm = normalize_audio(data_s)
    audio_v_norm = normalize_audio(data_v)

    # Aplicamos el ecualizador basado en FFT
    processed_song = apply_fft_equalizer(audio_s_norm, rate_s, eq_gains)
    processed_voice = apply_fft_equalizer(audio_v_norm, rate_v, eq_gains)

    # Generamos y guardamos los espectrogramas como imágenes PNG de salida
    song_out_name = "spec_song_latest.png"
    voice_out_name = "spec_voice_latest.png"
    
    # Generamos las imágenes y las guardamos en la carpeta OUTPUT_DIR
    generate_spectrogram(processed_song, rate_s, OUTPUT_DIR / song_out_name)
    generate_spectrogram(processed_voice, rate_v, OUTPUT_DIR / voice_out_name)

    # Agregamos timestamp para evitar que el navegador use imágenes viejas de la caché
    timestamp = int(time.time())

    # Detectamos la URL actual (localhost o Render) automáticamente
    base_url = str(request.base_url).rstrip("/")

    # Devolvemos las URLs finales al Frontend
    return {
        "status": "success",
        "images": {
           "song": f"{base_url}/output/{song_out_name}?t={timestamp}",
           "voice": f"{base_url}/output/{voice_out_name}?t={timestamp}"
        }
    }

# Esto permite correr el archivo localmente con 'python main.py'
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)