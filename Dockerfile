FROM python:3.13-slim

WORKDIR /app

# Install system dependencies for headless PowerPoint & PDF slide rendering on Linux
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-impress \
    libreoffice-common \
    poppler-utils \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-noto-core \
    fonts-roboto \
    fonts-open-sans \
    fonts-lato \
    fontconfig \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create data directory for persistent database
RUN mkdir -p /app/data

# Expose port
EXPOSE 5000

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=5000

# Run the application with waitress (production WSGI server)
CMD ["python", "main.py"]
