FROM python:3.11-slim

# Install build dependencies for libgourou
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake make g++ \
    libpugixml-dev libzip-dev libssl-dev libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone libgourou.
#
# The canonical upstream is forge.soutade.fr — a self-hosted Forgejo instance
# that can be slow or temporarily unreachable from Zeabur's build servers.
# We use a GitHub mirror as primary and fall back to the original, so builds
# are resilient without losing the ability to pick up upstream fixes.
#
RUN git clone --recurse-submodules \
        https://github.com/BentonEdmondson/the-one-with-libgourou-and-utilities.git \
        /app/libgourou \
    || git clone --recurse-submodules \
        https://forge.soutade.fr/soutade/libgourou.git \
        /app/libgourou

# Build all three utilities and verify they were produced
RUN cd /app/libgourou \
    && make BUILD_UTILS=1 BUILD_STATIC=1 BUILD_SHARED=0 \
    && ls -la /app/libgourou/utils/acsmdownloader \
    && ls -la /app/libgourou/utils/adept_activate \
    && ls -la /app/libgourou/utils/adept_remove

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app.py converter.py ./
COPY templates/ templates/

# Persistent data directories.
# On Zeabur: mount a Volume to /data so these survive restarts/redeploys.
# /data/adept holds the Adobe device registration — losing it forces a
# re-register with Adobe's servers on every cold start.
RUN mkdir -p /data/uploads /data/output /data/covers /data/adept \
    && ln -s /data/uploads /app/uploads \
    && ln -s /data/output  /app/output \
    && ln -s /data/covers  /app/covers \
    && mkdir -p /root/.config \
    && ln -s /data/adept   /root/.config/adept

EXPOSE 8080

# Shell form so Zeabur's injected $PORT is expanded at runtime.
# Falls back to 8080 for local docker run.
CMD sh -c "gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --threads 4 --timeout 300"
