FROM python:3.13-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY main.py .
COPY cache.py .
COPY custom_types.py .
COPY worshiptools_api.py .

# Create volume mount point for persistent database
VOLUME ["/app/data"]

# Expose Flask port
EXPOSE 5000

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "main:app"]
