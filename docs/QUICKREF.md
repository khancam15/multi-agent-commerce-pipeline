# Quick Reference - New Infrastructure

## 🚀 Quick Start

```bash
# Test everything
./test_infrastructure.sh

# Check health
python scripts/health_check.py

# Verify config
python scripts/config.py
```

## 📝 Common Tasks

### Using Logging
```python
from logger import get_logger

logger = get_logger(__name__)
logger.info("Message", extra={"product_name": "Example"})
logger.error("Failed", extra={"status_code": 503})
logger.exception("Caught error")  # Includes traceback
```

### Validating Input
```python
from validation import validate_product_name, validate_price

product = validate_product_name(user_input)  # Raises ValidationError
price = validate_price(9.99, min_price=0.99, max_price=999.99)
```

### Using Config
```python
from config import settings

key = settings.stripe_secret_key
limit = settings.weekly_publish_limit
retries = settings.max_retries
```

### Health Checks
```bash
# Full check
python scripts/health_check.py

# Quick (no API calls)
python scripts/health_check.py --quick

# JSON output
python scripts/health_check.py --json

# Web endpoint
python scripts/health_check.py --serve --port 8080
```

## 🔧 Development Commands

```bash
# Install dev tools
pip install -r requirements-dev.txt

# Run tests with coverage
pytest --cov=scripts --cov-report=html

# Type checking
mypy scripts/

# Linting
ruff check scripts/

# Auto-fix linting
ruff check --fix scripts/

# Security scan
bandit -r scripts/

# Format code
ruff format scripts/
```

## 🐛 Debugging

### Check Circuit Breaker Status
```python
from api_retry import _circuit_state
print(_circuit_state)  # Shows state for each host
```

### Reset Circuit Breaker
```python
from api_retry import _circuit_state
_circuit_state.clear()  # Reset all circuits
```

### View Logs
```bash
# Console logs
tail -f logs/pipeline.log

# Structured logs (if LOG_FORMAT=json)
jq . logs/pipeline.log | less
```

### Debug Database Issues
```bash
# Check for locks
fuser outputs/pipeline.db

# Kill lock holders
fuser -k outputs/pipeline.db

# Verify tables
sqlite3 outputs/pipeline.db ".tables"
```

## ⚙️ Environment Variables

```bash
# Logging
LOG_LEVEL=DEBUG              # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FORMAT=json              # json or console

# Circuit Breaker
CIRCUIT_FAILURE_THRESHOLD=5  # Failures before opening
CIRCUIT_TIMEOUT_SECONDS=300  # Seconds to wait

# API Retry
MAX_RETRIES=3
BACKOFF_BASE=2               # Exponential backoff base
MAX_BACKOFF=60               # Max wait time
```

## 🎯 File Locations

- **Config:** `scripts/config.py`
- **Logger:** `scripts/logger.py`
- **Validation:** `scripts/validation.py`
- **Health:** `scripts/health_check.py`
- **API Retry:** `scripts/api_retry.py` (updated)
- **Docs:** `docs/IMPLEMENTATION.md`
- **Test:** `./test_infrastructure.sh`

## 📊 Status Codes

### Health Check Exit Codes
- `0` = Healthy
- `1` = Unhealthy

### Health Check Status
- `healthy` = All checks passed
- `degraded` = Warnings present
- `unhealthy` = Failures present

### Circuit Breaker States
- `closed` = Normal operation
- `open` = Blocking requests (service down)
- `half_open` = Testing recovery

## 🔍 Quick Diagnostics

```bash
# Is the pipeline healthy?
python scripts/health_check.py && echo "✅ Healthy" || echo "❌ Unhealthy"

# Is the database locked?
sqlite3 outputs/pipeline.db "SELECT 1" && echo "✅ DB OK" || echo "❌ DB locked"

# Are API keys valid?
python scripts/config.py

# Check recent activity
sqlite3 outputs/pipeline.db "SELECT * FROM run_log ORDER BY run_at DESC LIMIT 5"

# Check circuit breaker state
python -c "from scripts.api_retry import _circuit_state; print(_circuit_state)"
```

## 📚 More Info

Full documentation: `docs/IMPLEMENTATION.md`

GitHub Issues: Report problems or suggestions

---

**Last Updated:** 2026-05-05  
**Quick Ref Version:** 1.0
