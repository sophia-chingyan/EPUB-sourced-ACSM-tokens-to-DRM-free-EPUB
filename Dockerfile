FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake make g++ \
    libpugixml-dev libzip-dev libssl-dev libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone libgourou.
# Primary: canonical upstream on the author's self-hosted forge.
# Fallback: BentonEdmondson's GitHub mirror — confirmed available and has the
#           same Makefile layout as the upstream.
# NOTE: github.com/Soutade/libgourou does NOT exist; that was a bad fallback.
RUN git clone --recurse-submodules \
        https://forge.soutade.fr/soutade/libgourou.git \
        /app/libgourou \
    || git clone --recurse-submodules \
        https://github.com/BentonEdmondson/the-one-with-libgourou-and-utilities.git \
        /app/libgourou

# Build and immediately verify all three binaries exist, then install them
# to /usr/local/bin so shutil.which() always finds them regardless of how
# SCRIPT_DIR resolves at runtime.
RUN cd /app/libgourou \
    && make BUILD_UTILS=1 BUILD_STATIC=1 BUILD_SHARED=0 \
    && test -x utils/acsmdownloader \
    && test -x utils/adept_activate \
    && test -x utils/adept_remove \
    && cp utils/acsmdownloader utils/adept_activate utils/adept_remove /usr/local/bin/ \
    && echo "libgourou build OK — binaries installed to /usr/local/bin"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py converter.py ./
COPY templates/ templates/

# /data must be a persistent volume in Zeabur (dashboard → Volumes → /data).
# /data/adept holds Adobe device credentials. Without persistence these are
# wiped on every redeploy, causing repeated re-registration and eventual
# Adobe device-limit bans.
RUN mkdir -p /data/uploads /data/output /data/covers /data/adept \
    && mkdir -p /root/.config \
    && ln -s /data/adept /root/.config/adept

EXPOSE 8080

CMD sh -c "gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --threads 4 --timeout 300"
