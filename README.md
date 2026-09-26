# DRPL - AI-Powered Tender Intelligence Platform

## Architecture

```
drpl-platform/
├── drpl-extension/     # Chrome Extension (Manifest V3) - Data Ingestion
├── drpl-backend/       # FastAPI Backend - API, Storage, AI Pipeline
└── README.md
```

### How It Works
1. User logs into IREPS/GeM with their DSC token (manual, as usual)
2. Chrome Extension detects the portal and extracts tender data from the DOM
3. Data is sent to the FastAPI backend via authenticated API calls
4. Backend stores, deduplicates, and triggers AI classification/matching

---

## Quick Start

### 1. Backend Setup
```bash
cd drpl-backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # Edit with your database credentials
alembic upgrade head            # Run database migrations
uvicorn app.main:app --reload   # Start dev server at http://localhost:8000
```

### 2. Extension Setup
```bash
cd drpl-extension
npm install
npm run dev                     # Build with watch mode
```
Then in Chrome:
1. Go to `chrome://extensions/`
2. Enable "Developer mode"
3. Click "Load unpacked" → select `drpl-extension/dist/`
4. Navigate to IREPS/GeM and log in with your DSC

---

## Tech Stack

| Component         | Technology                          |
|-------------------|-------------------------------------|
| Chrome Extension  | Manifest V3, TypeScript, React 18   |
| Extension UI      | React + Tailwind CSS                |
| Backend API       | FastAPI (Python 3.11+)              |
| Database          | PostgreSQL 15+                      |
| ORM               | SQLAlchemy 2.0 + Alembic            |
| Auth              | JWT (PyJWT)                         |
| Task Queue        | (Phase 2) Celery + Redis            |
| AI Pipeline       | (Phase 2) LangChain + OpenAI/Claude |

---

## Development Workflow

- **Extension** changes: `npm run dev` auto-rebuilds → reload extension in Chrome
- **Backend** changes: `uvicorn --reload` auto-restarts
- **Selector updates**: Edit `drpl-extension/src/config/selectors.json` → push to backend for OTA
- **Database changes**: Create migration with `alembic revision --autogenerate -m "description"`
