# Local STT sidecar: FunASR serving SenseVoiceSmall (~234M params, zh/en friendly CTC
# model) behind an OpenAI-compatible /v1/audio/transcriptions endpoint — same integration
# shape as the Kokoro TTS sidecar. ffmpeg decodes the desktop's webm/opus uploads.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Model weights (SenseVoiceSmall + fsmn-vad) land under /root/.cache/modelscope; mount
# the funasr-cache volume there so restarts never re-download.
EXPOSE 8000
CMD ["funasr-server", "--host", "0.0.0.0", "--port", "8000", "--model", "sensevoice", "--device", "cpu"]
