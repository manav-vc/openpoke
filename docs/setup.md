# Run locally

Requires Python 3.10+, Node.js 18+, and npm 9+. Run commands from the repository root.

## Configure

Copy `.env.example` to `.env` (`Copy-Item .env.example .env` in PowerShell, or `cp .env.example .env` on macOS/Linux). Fill in:

- `OPENROUTER_API_KEY` from [OpenRouter](https://openrouter.ai/).
- `COMPOSIO_API_KEY` and `COMPOSIO_GMAIL_AUTH_CONFIG_ID` from a Gmail integration in [Composio](https://composio.dev/).

The defaults allow **100 running executions globally**, **100 waiting**, and **one active execution per EA**. Existing `.env` files retain their overrides: set `OPENPOKE_AGENT_MAX_CONCURRENCY=100` to use the updated limit.

## Install

Create a virtual environment with Python 3.10+:

```bash
python -m venv .venv
```

Activate it for your shell:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r server/requirements.txt
npm install --prefix web
```

## Start

Backend, with **one process**:

```bash
python -m server.server --reload
```

Frontend, in a second terminal:

```bash
npm run dev --prefix web
```

Open [localhost:3000](http://localhost:3000), go to **Settings > Gmail**, and complete the OAuth flow for email workflows. Keep both services running. Runtime data is saved under `server/data/` and ignored by Git.

See [implementation and configuration](agent-optimization.md) or [tests and evaluation](../tests/README.md) for more detail.
