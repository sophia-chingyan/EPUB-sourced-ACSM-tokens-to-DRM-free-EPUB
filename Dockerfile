FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake make g++ \
    libpugixml-dev libzip-dev libssl-dev libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone libgourou from the canonical upstream.
# forge.soutade.fr can be slow; we try it first, then fall back to a known
# stable GitHub mirror. Both are checked in the same RUN layer so Docker
# caches a successful build even if one source is down.
#
# FIX: removed BentonEdmondson mirror — it has a different repo structure and
# its availability is not guaranteed. The two sources below are both the same
# upstream codebase with the same Makefile layout.
RUN git clone --recurse-submodules \
        https://forge.soutade.fr/soutade/libgourou.git \
        /app/libgourou \
    || git clone --recurse-submodules \
        https://github.com/Soutade/libgourou.git \
        /app/libgourou

RUN cd /app/libgourou \
    && make BUILD_UTILS=1 BUILD_STATIC=1 BUILD_SHARED=0 \
    && ls -la /app/libgourou/utils/acsmdownloader \
    && ls -la /app/libgourou/utils/adept_activate \
    && ls -la /app/libgourou/utils/adept_remove \
    && cp /app/libgourou/utils/acsmdownloader /usr/local/bin/ \
    && cp /app/libgourou/utils/adept_activate  /usr/local/bin/ \
    && cp /app/libgourou/utils/adept_remove    /usr/local/bin/ \
    && chmod +x /usr/local/bin/acsmdownloader /usr/local/bin/adept_activate /usr/local/bin/adept_remove

# Make the utils findable via both the local path check and shutil.which
ENV PATH="/app/libgourou/utils:/usr/local/bin:${PATH}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py converter.py ./
COPY templates/ templates/

# /data is the persistent volume mount point (configure in Zeabur dashboard).
# /data/adept holds Adobe device credentials — losing it means re-registration
# on every cold start, which eventually triggers Adobe's device-limit ban.
# Mount a volume at /data or you WILL hit that limit.
RUN mkdir -p /data/uploads /data/output /data/covers /data/adept \
    && mkdir -p /root/.config \
    && ln -s /data/adept /root/.config/adept

EXPOSE 8080

CMD sh -c "gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --threads 4 --timeout 300"
