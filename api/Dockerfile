# CutFlow•CAD API — Phase 0 minimal image
# Python 3.12 slim base; runs uvicorn as non-root user.
FROM python:3.12-slim

# System dependencies for C extensions used by Phase 1+ libraries
# (shapely needs libgeos, pyclipper builds via gcc/g++). Phase 6 adds
# Noto CJK fonts so the ezdxf SVGBackend can render Japanese text
# (寸法ラベル, ノート) instead of falling back to tofu boxes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        libgeos-dev \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

# Non-root user for runtime
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Install Python deps first to maximize layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Eagerly build ezdxf font cache so the first /render-svg call doesn't pay
# the 30+ second scan cost. Swallow failures — the runtime fallback retries
# with MTEXT placeholders if no fonts are found.
RUN python -c "import ezdxf; ezdxf.options.use_matplotlib = False; \
from ezdxf.fonts import fonts as _f; \
[fn() for fn in (getattr(_f, 'build_system_fonts_cache', None),) if fn] \
or [print('font cache helper not in this ezdxf version')]" \
 || echo 'font cache build skipped'

# Drop privileges
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/api/health').getcode()==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
