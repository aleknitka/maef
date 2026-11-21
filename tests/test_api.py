from fastapi.testclient import TestClient
from maef_api.main import app

client = TestClient(app)

def test_health():
    result = client.get('/health')
    assert result.status_code == 200
    assert result.json()['execution_enabled'] is False

def test_agents():
    result = client.get('/api/v1/agents')
    assert result.status_code == 200
    assert {'hyperframes-animator', 'research-analyst'} <= {a['id'] for a in result.json()}

def test_preview_clamps_budget():
    result = client.post('/api/v1/runs/preview', json={
        'agent_id': 'hyperframes-animator', 'instruction': 'Create intro',
        'budget': {'max_cost_usd': 20, 'max_duration_seconds': 1000, 'max_turns': 100}
    })
    assert result.status_code == 200
    body = result.json()
    assert body['status'] == 'preview_only'
    assert body['effective_budget'] == {'max_cost_usd': 0.75, 'max_duration_seconds': 600, 'max_turns': 25}

def test_execution_explicitly_unavailable():
    result = client.post('/api/v1/runs', json={'agent_id': 'hyperframes-animator', 'instruction': 'Hi'})
    assert result.status_code == 501

def test_path_traversal_rejected():
    assert client.get('/api/v1/agents/..%2F..%2Fetc').status_code in (404, 400)
