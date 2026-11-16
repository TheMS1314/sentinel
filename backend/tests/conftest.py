"""Never connect tests to the application's real database or provider credentials."""
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
url = os.environ.get('SENTINEL_TEST_DATABASE_URL')
if url and not url.rsplit('/', 1)[-1].endswith('_test'):
    raise RuntimeError('Test database name must end in _test')
os.environ['SENTINEL_DATABASE_URL'] = url or 'sqlite:///' + tempfile.mktemp(prefix='sentinel-test-', suffix='.sqlite')
os.environ['SENTINEL_ENV'] = 'test'
os.environ['QUEUE_BACKEND'] = 'memory'
os.environ['CACHE_BACKEND'] = 'memory'
os.environ['SENTINEL_RATE_LIMIT_REQUESTS'] = '5000'
os.environ['SENTINEL_API_KEY'] = 'test-key-contracts'
os.environ['LLM_PROVIDER'] = 'openrouter'
os.environ['OPENROUTER_API_KEY'] = ''
os.environ['ANTHROPIC_API_KEY'] = ''
os.environ['RAZORPAY_KEY_ID'] = ''
os.environ['RAZORPAY_KEY_SECRET'] = ''
import pytest

@pytest.fixture(autouse=True)
def offline_provider_keys(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', '')
    monkeypatch.setenv('ANTHROPIC_API_KEY', '')
    monkeypatch.setenv('SENTINEL_ENV', 'test')
