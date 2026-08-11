# Use the official Microsoft Playwright image which has all browser dependencies pre-installed!
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python requirements
RUN pip install --no-cache-dir -r requirements.txt

# Initialize Python-level Playwright binaries
RUN playwright install chromium

# Copy the rest of the application
COPY . .

# Set default port
ENV PORT=5000

# Expose port
EXPOSE 5000

# Start the application using Gunicorn reading dynamic PORT env var
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --threads 4 backend.app:app"]

