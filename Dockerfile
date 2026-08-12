FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py oauth.py ./

# OAuth state (registered clients, tokens) lives here — mount a volume on it.
ENV MCP_STATE_DB=/state/oauth_state.db
VOLUME /state

EXPOSE 5002
CMD ["python", "server.py", "--http"]
