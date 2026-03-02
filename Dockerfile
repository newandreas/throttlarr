FROM python:alpine
WORKDIR /app

# Added gunicorn to the requirements
RUN pip install --no-cache-dir flask qbittorrent-api requests gunicorn

COPY app.py /app/app.py

EXPOSE 5000

# Change the CMD to start Gunicorn instead of Python directly
# --workers 1 is best here because we use an in-memory 'active_sessions' set.
# --bind 0.0.0.0:5000 tells it to listen on all interfaces.
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:5000", "app:app"]