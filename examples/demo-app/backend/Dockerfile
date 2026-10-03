FROM python:3.12.10-alpine3.21
WORKDIR /app
COPY app.py .
USER 65534:65534
EXPOSE 8000
CMD ["python", "app.py"]
