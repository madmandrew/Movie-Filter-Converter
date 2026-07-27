# Movie Filter — web UI + Whisper audio filtering pipeline.
#
# Built for Unraid with GPU passthrough. Whisper runs on CUDA when a GPU is visible and
# falls back to CPU int8 otherwise (roughly 10-20x slower, but functional).
#
#   docker build -t movie-filter .
#   docker run -d --name movie-filter \
#     --gpus all \
#     -p 8080:8000 \
#     -v /mnt/user/media:/media \
#     -v /mnt/user/appdata/movie-filter:/data \
#     movie-filter
#
# On Unraid, `--gpus all` requires the Nvidia-Driver plugin. Without it the container
# still runs; check /api/health to see which device Whisper actually got.

FROM python:3.12-slim

# ffmpeg for all media work. The Debian build includes the QSV/VAAPI encoders, which
# matter because video cuts force a re-encode and software x264 is slower than realtime
# (measured 0.47x at preset slow on 1080p). Hardware encoders are probed at runtime by
# a real test encode — being listed by ffmpeg does not mean a given driver works.
#
# libgomp1 is OpenMP, required by onnxruntime (nudity detection). Note that OpenCV is
# installed as `opencv-python-headless`: the regular build needs libGL and GTK, which
# this image does not carry and which nothing here would use.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/ ./tools/
COPY app/ ./app/

# Model weights land here; mount it to avoid re-downloading on every container rebuild.
ENV HF_HOME=/data/models \
    FILTER_DB=/data/filter.db \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/tools:/app/app

VOLUME ["/data", "/media"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
