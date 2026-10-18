FROM python:3.11-slim

# Install system dependencies: FFmpeg, curl, Node.js, xvfb, Google Chrome
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    xvfb \
    xauth \
    nodejs \
    npm \
    gnupg \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/trusted.gpg.d/google-chrome.gpg \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google.list \
    && apt-get update && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# gflow-cli deliberately removes Playwright's --no-sandbox argument.
# Chrome's namespace sandbox is unavailable inside our unprivileged Docker
# container, so generation exits before opening Flow unless the flag is
# restored. The patcher is version- and source-guarded and fails the image
# build if the pinned dependency changes unexpectedly.
RUN python scripts/patch_gflow_container.py

# Install Node.js dependencies
RUN npm install --omit=dev

# Create data directories and non-root user with home directory
RUN mkdir -p /app/data/images /app/data/tmp /app/logs \
    && groupadd -r veoapi && useradd -r -m -d /home/veoapi -g veoapi veoapi \
    && chown -R veoapi:veoapi /app /home/veoapi

USER veoapi

# Expose port
EXPOSE 4000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:4000/health || exit 1

# Run the application
CMD ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", "python", "main.py"]
