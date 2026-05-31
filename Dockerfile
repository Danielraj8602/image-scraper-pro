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

# Expose the Flask development/production port
EXPOSE 5000

# Start the application using a production WSGI server (Gunicorn)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "backend.app:app"]
