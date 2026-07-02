# RADAR — Enterprise Document Search & OCR Platform

**RADAR (Rapid Archival Document Analysis & Retrieval)** is a production-grade document ingestion, OCR extraction, and full-text search platform built for enterprise environments. It processes large document repositories with intelligent routing, PaddleOCR-powered text extraction, and real-time search via OpenSearch.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Source Directories                             │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Discovery Workers  │  Bloom Filter Dedup  │  SHA-256 Hashing        │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│              Size-Based Extraction Track Routing                      │
│  ┌──────────┐ ┌──────────────┐ ┌────────────┐ ┌──────────────────┐ │
│  │ Fast     │ │ Standard     │ │ Heavy      │ │ Extreme          │ │
│  │ < 1 MB   │ │ 1–10 MB      │ │ 10–50 MB   │ │ > 50 MB          │ │
│  └──────────┘ └──────────────┘ └────────────┘ └──────────────────┘ │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Apache Tika Extraction  →  Micro-Batch Indexing  →  OpenSearch     │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PaddleOCR Workers (background)  │  Visual Snippet Review (HITL)    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Key Capabilities

- **Multi-track extraction** — files routed by size to specialized worker pools
- **PaddleOCR integration** — GPU/CPU OCR for scanned documents and images
- **Real-time search** — OpenSearch with multi-field ranking and OCR-aware fuzzy matching
- **Human-in-the-loop review** — visual snippet verification portal (signatures, stamps, logos)
- **CNN visual memory** — approved visual elements auto-tagged in future scans
- **Bloom filter deduplication** — near-zero false positive file-level dedup
- **Intelligent metadata tagging** — spaCy NLP + optional external metadata Excel
- **Live monitoring dashboard** — Streamlit-based real-time pipeline metrics
- **Crash recovery** — checkpointing, Redis persistence, graceful shutdown/resume

---

## Performance Specifications

| Metric | Value |
|--------|-------|
| Extraction Throughput | 245–420 files/sec |
| Indexing Throughput | 12,000–20,000 docs/sec |
| Time-to-Searchable | 10–30 seconds |
| OCR Pages/Hour | 5,000+ |

---

## Project Structure

```
RADAR/
├── bin/                        # Service startup/shutdown scripts
│   ├── start-system.ps1
│   ├── start-dashboard.ps1
│   ├── start_opensearch.bat
│   └── ...
├── config/
│   ├── config.yaml             # Main pipeline configuration
│   ├── jvm.options             # OpenSearch JVM settings
│   └── opensearch.yml          # OpenSearch cluster config
├── src/
│   ├── main.py                 # CLI entry point (start/stop/reset/status)
│   ├── orchestrator/           # Master orchestrator & worker coordination
│   ├── core/                   # Config, queue manager, logging, constants
│   ├── discovery/              # File system scanner & Bloom filter
│   ├── extraction/             # Tika-based text extraction workers
│   ├── indexing/               # OpenSearch bulk indexing
│   ├── ocr/                    # PaddleOCR pipeline & visual memory CNN
│   ├── tagging/                # NLP metadata tagging (spaCy + taxonomy)
│   ├── nlp/                    # Language detection & entity extraction
│   ├── ui/                     # Streamlit dashboard & PDF reports
│   ├── api/                    # FastAPI search REST endpoints
│   ├── tools/                  # Diagnostic & maintenance utilities
│   └── utils/                  # Shared helper functions
├── models/                     # ML model weights (not in git)
│   ├── mobilenetv3.onnx
│   └── yolov8n.pt
├── runtime/                    # Generated at runtime (not in git)
│   ├── logs/
│   ├── cache/
│   ├── checkpoints/
│   ├── audit/
│   ├── reports/
│   ├── review_snippets/
│   └── metadata/
├── requirements.txt            # Python dependencies
├── start_everything.bat        # One-click startup (Windows)
└── README.md
```

---

## Prerequisites

| Component | Version | Purpose |
|-----------|---------|---------|
| Python | 3.10+ | Runtime |
| Redis | 3.2+ | Queue management & caching |
| OpenSearch | 2.12+ | Full-text search index |
| Apache Tika | 2.9+ | Document text extraction |
| Poppler | Latest | PDF-to-image for OCR |

---

## Quick Start

### 1. Install Python dependencies

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Start services

```powershell
# Option A: All-in-one
.\start_everything.bat

# Option B: Individual services
.\bin\start_opensearch.bat
.\bin\start-tika.ps1
# Redis starts automatically if installed as service
```

### 3. Start the processing pipeline

```bash
python src/main.py start
```

### 4. Launch the monitoring dashboard

```powershell
.\bin\start-dashboard.ps1
# → http://localhost:8501
```

---

## CLI Commands

```bash
python src/main.py start              # Start full pipeline
python src/main.py start --mode resume  # Resume from checkpoint
python src/main.py stop               # Graceful shutdown
python src/main.py status             # Queue status summary
python src/main.py stats              # Detailed statistics
python src/main.py reset              # Full system reset
python src/main.py reset --force      # Non-interactive reset
python src/main.py reset_stale        # Recover stuck items
```

---

## Configuration

All pipeline settings are in `config/config.yaml`:

- **paths** — source directories, working root, log location
- **discovery** — scan workers, exclusion patterns, size categories
- **extraction** — worker pools, Tika instances, timeouts
- **indexing** — OpenSearch connection, batch sizes, mapping
- **ocr** — PaddleOCR settings, preprocessing, visual review policy
- **tagging** — NLP model, taxonomy, metadata input
- **orchestrator** — resource thresholds, circuit breakers, checkpointing

---

## Dashboard Features

| Tab | Description |
|-----|-------------|
| **Search** | Full-text search with filters, highlighting, PDF reports |
| **Live Audit** | Real-time event feed, state matrix export |
| **Snippet Review** | Visual verification portal (HITL) with CNN auto-tagging |
| **System Monitor** | Pipeline metrics, ETA, failure analysis |

---

## System Reset

The `reset` command clears all processing state for a fresh start:

- Redis queues & counters
- Queue database (SQLite)
- Bloom filters
- Cache files
- Checkpoints
- OpenSearch index
- Log files
- Audit database & state matrices
- Visual memory vectors
- Review snippet images
- Redis backups
- Metadata uploads

---

## Monitoring & Logs

- **Runtime logs:** `runtime/logs/` (per-component: discovery, extraction, indexing, ocr, orchestrator)
- **Dashboard:** http://localhost:8501
- **API:** http://localhost:8080/docs (FastAPI Swagger)
- **OpenSearch:** http://localhost:9200

---

## License

Proprietary. Internal use only.
