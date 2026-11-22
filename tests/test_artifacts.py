import hashlib
import re

import pytest
from fastapi.testclient import TestClient
from maef_api import artifacts as artifacts_module
from maef_api import ledger as ledger_module
from maef_api import main
from maef_api import outputs as outputs_module
from maef_api.adapters.base import ModelResponse
from maef_api.artifacts import (
    ArtifactCorrupt,
    ArtifactIntake,
    ArtifactNotFound,
    ArtifactRef,
    ArtifactRejected,
    InMemoryArtifactStore,
    LocalArtifactStore,
    sha256_digest,
)
from maef_api.channel import LocalChannel
from maef_api.leases import FencingError
from maef_api.ledger import Ledger
from maef_api.outputs import collect_outputs
from maef_api.runs import Runs
from maef_api.sandbox import ExecResult, FakeSandbox, FakeSession, SandboxError
from maef_api.worker import Worker
from test_worker import AUTH, TOKEN, ToolThenAnswer, admit, admit_sandboxed, run_once

DIGEST_OF_HELLO = 'sha256:' + hashlib.sha256(b'hello').hexdigest()


@pytest.fixture
def store(tmp_path):
    s = Ledger(tmp_path / 'artifacts.db')
    s.ensure_account('default', 1_000_000)
    return s


@pytest.fixture(params=['local', 'memory'])
def blobs(request, tmp_path):
    if request.param == 'local':
        return LocalArtifactStore(tmp_path / 'blobs')
    return InMemoryArtifactStore()


def corrupt(store, digest):
    if isinstance(store, LocalArtifactStore):
        hex_ = digest.removeprefix('sha256:')
        (store.root / 'sha256' / hex_[:2] / hex_[2:]).write_bytes(b'tampered')
    else:
        store._blobs[digest] = b'tampered'


def claim_run(store, files=None):
    run_id = admit(store, files=files)
    return run_id, LocalChannel(store).claim('w1')


def types(store, run_id):
    return [e.type for e in store.get(run_id).events]


# --- LocalArtifactStore and InMemoryArtifactStore ---

def test_put_returns_sha256_digest_and_size(blobs):
    ref = blobs.put(b'hello', name='a.txt', mime='text/plain')
    assert ref.digest == DIGEST_OF_HELLO
    assert (ref.size, ref.name, ref.mime) == (5, 'a.txt', 'text/plain')


def test_read_returns_bytes_by_ref_or_digest(blobs):
    ref = blobs.put(b'hello', name='a.txt', mime='text/plain')
    assert blobs.read(ref) == b'hello'
    assert blobs.read(ref.digest) == b'hello'
    assert blobs.exists(ref.digest) is True


def test_same_bytes_are_deduplicated(blobs):
    first = blobs.put(b'hello', name='a.txt', mime='text/plain')
    second = blobs.put(b'hello', name='a.txt', mime='text/plain')
    assert first == second
    assert blobs.read(first) == b'hello'


def test_local_layout_and_single_file_for_duplicate_bytes(tmp_path):
    store = LocalArtifactStore(tmp_path)
    ref = store.put(b'hello', name='a.txt', mime='text/plain')
    hex_ = ref.digest.removeprefix('sha256:')
    assert (tmp_path / 'sha256' / hex_[:2] / hex_[2:]).read_bytes() == b'hello'
    store.put(b'hello', name='b.txt', mime='text/plain')
    files = [p.name for p in (tmp_path / 'sha256').rglob('*') if p.is_file()]
    assert files == [hex_[2:]]


def test_corrupt_bytes_raise_artifact_corrupt(blobs):
    ref = blobs.put(b'hello', name='a.txt', mime='text/plain')
    corrupt(blobs, ref.digest)
    with pytest.raises(ArtifactCorrupt):
        blobs.read(ref)


@pytest.mark.parametrize('digest', ['sha256:' + '0' * 64, 'not-a-digest'])
def test_unknown_or_malformed_digest_is_not_found(blobs, digest):
    with pytest.raises(ArtifactNotFound):
        blobs.read(digest)
    assert blobs.exists(digest) is False


# --- ArtifactRef sanitising ---

@pytest.mark.parametrize('raw,expected', [
    ('../../etc/passwd', 'passwd'),
    ('dir/sub/report.md', 'report.md'),
    ('C:\\temp\\evil.exe', 'evil.exe'),
    ('bad\x00\x07name\n.txt', 'badname.txt'),
    ('a b/c:d*e?.txt', 'c_d_e_.txt'),
    ('..', 'artifact'),
    ('', 'artifact'),
    ('x' * 200, 'x' * 128),
])
def test_names_are_sanitised_to_safe_basenames(raw, expected):
    ref = ArtifactRef(digest=sha256_digest(b''), size=0, name=raw)
    assert ref.name == expected
    assert re.fullmatch(r'[\w.\- ()+,@=~]{1,128}', ref.name)


@pytest.mark.parametrize('raw,expected', [
    ('text/markdown', 'text/markdown'),
    ('Text/Plain; charset=utf-8', 'text/plain'),
    ('image/png', 'image/png'),
    ('text/html', 'application/octet-stream'),
    ('application/x-msdownload', 'application/octet-stream'),
    ('', 'application/octet-stream'),
])
def test_mime_allow_list(raw, expected):
    assert ArtifactRef(digest=sha256_digest(b''), size=0, name='x', mime=raw).mime == expected


# --- ArtifactIntake and the ledger ---

def test_digest_mismatch_is_rejected_before_storage(store, blobs):
    run_id, lease = claim_run(store)
    intake = ArtifactIntake(store, blobs)
    with pytest.raises(ArtifactRejected) as exc:
        intake.accept(run_id, token=lease.fencing_token, name='a.txt', mime='text/plain',
                      data=b'x', digest=sha256_digest(b'y'))
    assert exc.value.code == 'digest_mismatch'
    assert blobs.exists(sha256_digest(b'x')) is False
    assert store.artifacts(run_id) == []


def test_oversize_is_rejected_with_patched_limit(store, blobs, monkeypatch):
    monkeypatch.setattr(artifacts_module, 'MAX_ARTIFACT_BYTES', 8)
    run_id, lease = claim_run(store)
    intake = ArtifactIntake(store, blobs)
    with pytest.raises(ArtifactRejected) as exc:
        intake.accept(run_id, token=lease.fencing_token, name='a.bin', mime='text/plain', data=b'x' * 9)
    assert exc.value.code == 'too_large'
    assert blobs.exists(sha256_digest(b'x' * 9)) is False
    assert intake.accept(run_id, token=lease.fencing_token, name='a.bin', mime='text/plain', data=b'x' * 8).size == 8


def test_per_run_count_limit(store, blobs, monkeypatch):
    monkeypatch.setattr(ledger_module, 'MAX_RUN_ARTIFACTS', 2)
    run_id, lease = claim_run(store)
    intake = ArtifactIntake(store, blobs)
    for i in range(2):
        intake.accept(run_id, token=lease.fencing_token, name=f'f{i}', mime='text/plain', data=bytes([i]))
    with pytest.raises(ArtifactRejected) as exc:
        intake.accept(run_id, token=lease.fencing_token, name='f2', mime='text/plain', data=b'2')
    assert exc.value.code == 'run_limit'
    assert len(store.artifacts(run_id)) == 2
    assert blobs.exists(sha256_digest(b'2')) is False


def test_per_run_byte_limit(store, blobs, monkeypatch):
    monkeypatch.setattr(ledger_module, 'MAX_RUN_ARTIFACT_BYTES', 10)
    run_id, lease = claim_run(store)
    intake = ArtifactIntake(store, blobs)
    intake.accept(run_id, token=lease.fencing_token, name='a', mime='text/plain', data=b'a' * 6)
    intake.accept(run_id, token=lease.fencing_token, name='b', mime='text/plain', data=b'b' * 4)
    with pytest.raises(ArtifactRejected) as exc:
        intake.accept(run_id, token=lease.fencing_token, name='c', mime='text/plain', data=b'c')
    assert exc.value.code == 'run_limit'
    assert sum(a.size for a in store.artifacts(run_id)) == 10


def test_stale_fencing_token_is_refused_and_nothing_stored(store, blobs):
    run_id, lease = claim_run(store)
    intake = ArtifactIntake(store, blobs)
    with pytest.raises(FencingError):
        intake.accept(run_id, token=lease.fencing_token + 1, name='a.txt', mime='text/plain', data=b'z')
    assert blobs.exists(sha256_digest(b'z')) is False
    assert store.artifacts(run_id) == []


def test_reupload_of_same_run_digest_and_name_counts_once(store, blobs):
    run_id, lease = claim_run(store)
    intake = ArtifactIntake(store, blobs)
    first = intake.accept(run_id, token=lease.fencing_token, name='out.txt', mime='text/plain', data=b'same')
    second = intake.accept(run_id, token=lease.fencing_token, name='out.txt', mime='text/plain', data=b'same')
    assert first == second
    assert [a.digest for a in store.artifacts(run_id)] == [first.digest]
    assert types(store, run_id).count('artifact.stored') == 1


def test_artifact_stored_event_is_emitted(store, blobs):
    run_id, lease = claim_run(store)
    ArtifactIntake(store, blobs).accept(run_id, token=lease.fencing_token, name='out.txt',
                                        mime='text/plain', data=b'hello')
    event = next(e for e in store.get(run_id).events if e.type == 'artifact.stored')
    assert event.payload == {'name': 'out.txt', 'digest': DIGEST_OF_HELLO, 'size': 5, 'mime': 'text/plain'}


@pytest.mark.parametrize('type_', ['artifact.stored', 'artifact.anything'])
def test_workers_cannot_report_artifact_events(store, type_):
    run_id, lease = claim_run(store)
    with pytest.raises(ValueError):
        LocalChannel(store).report(lease, 1, type_, {})
    assert 'artifact.stored' not in types(store, run_id)


# --- Harness and worker, end to end ---

class MarkdownAnswer:
    TEXT = '# Result\n\nSome *markdown* answer.\n'

    def converse(self, **kwargs):
        return ModelResponse(text=self.TEXT, tool_calls=(), stop_reason='end_turn', input_tokens=7, output_tokens=5)


def test_final_answer_is_stored_as_output_md(store, tmp_path):
    run_id = admit(store)
    channel = LocalChannel(store, artifacts=LocalArtifactStore(tmp_path / 'blobs'))
    assert run_once(channel, MarkdownAnswer()) is True
    detail = store.get(run_id)
    assert detail.status == 'succeeded'

    run_output = next(e.payload for e in detail.events if e.type == 'run.output')
    assert set(run_output) == {'digest', 'size', 'mime', 'name'}
    assert (run_output['name'], run_output['mime']) == ('output.md', 'text/markdown')
    assert run_output['size'] == len(MarkdownAnswer.TEXT.encode())
    assert channel.artifacts.read(run_output['digest']) == MarkdownAnswer.TEXT.encode()

    manifest = next(e.payload for e in detail.events if e.type == 'run.manifest')
    assert manifest['artifacts'] == [run_output]


def test_collected_sandbox_files_are_stored_and_listed_in_manifest(store, tmp_path):
    run_id = admit_sandboxed(store)

    def handler(argv, stdin):
        if argv[0] == 'find':
            return ExecResult(0, b'/workspace/out/report.md\0', b'')
        sandbox.sessions[0].files['/workspace/out/report.md'] = b'# Report'
        return ExecResult(0, b'hi\n', b'')

    sandbox = FakeSandbox(handler)
    worker = Worker(LocalChannel(store, artifacts=LocalArtifactStore(tmp_path / 'blobs')),
                    lambda p: ToolThenAnswer(), worker_id='w1', sandbox=sandbox)
    assert worker.run_once() is True
    detail = store.get(run_id)
    assert detail.status == 'succeeded'
    manifest = next(e.payload for e in detail.events if e.type == 'run.manifest')
    assert [(a['name'], a['mime']) for a in manifest['artifacts']] == [('output.md', 'text/markdown'),
                                                                        ('report.md', 'text/markdown')]


# --- collect_outputs ---

def listing(*paths):
    return ExecResult(0, b''.join(p.encode() + b'\0' for p in paths), b'')


def test_direct_files_collected_with_guessed_mimes():
    files = {'/workspace/out/notes.md': b'# hi', '/workspace/out/chart.png': b'\x89PNG',
             '/workspace/out/page.html': b'<b>x</b>', '/workspace/out/blob.bin': b'\x00'}
    session = FakeSession(lambda argv, stdin: listing(*files), files=files)
    collected = collect_outputs(session, budget_files=10)
    assert {f.name: f.mime for f in collected.files} == {
        'notes.md': 'text/markdown', 'chart.png': 'image/png',
        'page.html': 'application/octet-stream', 'blob.bin': 'application/octet-stream'}
    assert collected.skipped == []


def test_listing_is_top_level_regular_files_only():
    files = {'/workspace/out/top.txt': b'1', '/workspace/out/sub/deep.txt': b'2',
             '/workspace/out/link.txt': b'3'}
    session = FakeSession(lambda argv, stdin: listing('/workspace/out/top.txt'), files=files)
    collected = collect_outputs(session)
    assert [f.name for f in collected.files] == ['top.txt']
    assert session.execs == [['find', '/workspace/out', '-maxdepth', '1', '-type', 'f', '-print0']]


def test_oversize_file_is_skipped_with_reason(monkeypatch):
    monkeypatch.setattr(outputs_module, 'MAX_ARTIFACT_BYTES', 4)
    files = {'/workspace/out/small.txt': b'abc', '/workspace/out/big.txt': b'abcdef'}
    session = FakeSession(lambda argv, stdin: listing(*files), files=files)
    collected = collect_outputs(session, budget_files=10)
    assert [f.name for f in collected.files] == ['small.txt']
    assert collected.skipped == [{'name': 'big.txt', 'reason': 'too_large'}]


def test_byte_budget_skips_later_files_with_run_limit():
    files = {'/workspace/out/a.txt': b'aaa', '/workspace/out/b.txt': b'bbb'}
    session = FakeSession(lambda argv, stdin: listing(*files), files=files)
    collected = collect_outputs(session, budget_files=10, budget_bytes=5)
    assert [f.name for f in collected.files] == ['a.txt']
    assert collected.skipped == [{'name': 'b.txt', 'reason': 'run_limit'}]


def test_missing_out_directory_yields_nothing():
    session = FakeSession(lambda argv, stdin: ExecResult(1, b'', b'find: no such file'))
    collected = collect_outputs(session)
    assert collected.files == [] and collected.skipped == []


def test_failed_listing_exec_raises_sandbox_error():
    def handler(argv, stdin):
        raise SandboxError('exec_failed')

    with pytest.raises(SandboxError):
        collect_outputs(FakeSession(handler))


def test_failed_collection_is_recorded_and_run_still_succeeds(store, tmp_path):
    run_id = admit_sandboxed(store)

    def handler(argv, stdin):
        if argv[0] == 'find':
            raise SandboxError('exec_failed')
        return ExecResult(0, b'hi\n', b'')

    worker = Worker(LocalChannel(store, artifacts=LocalArtifactStore(tmp_path / 'blobs')),
                    lambda p: ToolThenAnswer(), worker_id='w1', sandbox=FakeSandbox(handler))
    assert worker.run_once() is True
    detail = store.get(run_id)
    assert detail.status == 'succeeded'
    assert next(e.payload for e in detail.events if e.type == 'outputs.collection_failed') == {'code': 'exec_failed'}


# --- HTTP routes ---

@pytest.fixture
def http(store, tmp_path, monkeypatch):
    monkeypatch.setenv('MAEF_WORKER_TOKEN', TOKEN)
    blobs = LocalArtifactStore(tmp_path / 'http-blobs')
    monkeypatch.setattr(main, 'ledger', store)
    monkeypatch.setattr(main, 'runs', Runs(main.runs.definitions, store,
                                           execution_enabled=lambda: True, allow_dirty=lambda: True))
    monkeypatch.setattr(main, 'artifacts', blobs)
    return TestClient(main.app), blobs


def upload(client, run_id, data, *, token=1, digest=None, auth=AUTH, name='out.txt', mime='text/plain'):
    headers = {**(auth or {}), 'X-Fencing-Token': str(token), 'X-Content-Digest': digest or sha256_digest(data),
               'Content-Type': 'application/octet-stream'}
    return client.post(f'/api/v1/worker/runs/{run_id}/artifacts', params={'name': name, 'mime': mime},
                       content=data, headers=headers)


def test_upload_refused_when_worker_token_unset(http, store, monkeypatch):
    client, _ = http
    run_id, _ = claim_run(store)
    monkeypatch.delenv('MAEF_WORKER_TOKEN')
    assert upload(client, run_id, b'x').status_code == 503


def test_upload_with_wrong_or_missing_token_is_401(http, store):
    client, _ = http
    run_id, _ = claim_run(store)
    assert upload(client, run_id, b'x', auth={'Authorization': 'Bearer nope'}).status_code == 401
    assert upload(client, run_id, b'x', auth={}).status_code == 401


def test_upload_digest_mismatch_is_400(http, store):
    client, blobs = http
    run_id, lease = claim_run(store)
    res = upload(client, run_id, b'data', token=lease.fencing_token, digest=sha256_digest(b'other'))
    assert res.status_code == 400 and res.json()['detail']['code'] == 'digest_mismatch'
    assert blobs.exists(sha256_digest(b'data')) is False


def test_upload_oversize_is_413(http, store, monkeypatch):
    client, blobs = http
    run_id, lease = claim_run(store)
    monkeypatch.setattr(main, 'MAX_ARTIFACT_BYTES', 8)
    res = upload(client, run_id, b'x' * 9, token=lease.fencing_token)
    assert res.status_code == 413 and res.json()['detail']['code'] == 'too_large'
    assert blobs.exists(sha256_digest(b'x' * 9)) is False


def test_upload_stale_token_is_409(http, store):
    client, blobs = http
    run_id, lease = claim_run(store)
    res = upload(client, run_id, b'data', token=lease.fencing_token + 5)
    assert res.status_code == 409 and res.json()['detail']['code'] == 'fencing'
    assert blobs.exists(sha256_digest(b'data')) is False


def test_worker_event_route_refuses_artifact_types(http, store):
    client, _ = http
    run_id, lease = claim_run(store)
    res = client.post(f'/api/v1/worker/runs/{run_id}/events', headers=AUTH, json={
        'fencing_token': lease.fencing_token, 'wseq': 1, 'type': 'artifact.stored', 'payload': {}})
    assert res.status_code == 422


def test_upload_is_listed_and_fetched_with_safe_headers(http, store):
    client, _ = http
    run_id, lease = claim_run(store)
    data = b'# Title\n'
    res = upload(client, run_id, data, token=lease.fencing_token, name='report.md', mime='text/markdown')
    assert res.status_code == 200
    ref = res.json()
    assert ref == {'digest': sha256_digest(data), 'size': len(data), 'mime': 'text/markdown', 'name': 'report.md'}

    assert client.get(f'/api/v1/runs/{run_id}/artifacts').json() == [ref]

    got = client.get(f"/api/v1/artifacts/{ref['digest']}")
    assert got.status_code == 200 and got.content == data
    assert got.headers['content-type'].startswith('text/markdown')
    assert got.headers['content-disposition'] == 'attachment; filename="report.md"'
    assert got.headers['x-content-type-options'] == 'nosniff'
    assert got.headers['content-security-policy'] == 'sandbox'


def test_digest_present_in_store_but_never_recorded_is_404(http):
    client, blobs = http
    ref = blobs.put(b'orphan', name='x.txt', mime='text/plain')
    assert client.get(f'/api/v1/artifacts/{ref.digest}').status_code == 404


def test_unknown_digest_is_404(http):
    client, _ = http
    assert client.get('/api/v1/artifacts/sha256:' + '0' * 64).status_code == 404
    assert client.get('/api/v1/artifacts/not-a-digest').status_code == 404


def test_retry_of_recorded_upload_is_noop_at_run_limit(tmp_path, monkeypatch):
    import maef_api.ledger as ledger_mod
    from maef_api.artifacts import ArtifactIntake, InMemoryArtifactStore
    from maef_api.ledger import Ledger
    from maef_api.models import Budget
    from test_ledger import definition
    monkeypatch.setattr(ledger_mod, 'MAX_RUN_ARTIFACTS', 1)
    ledger = Ledger(tmp_path / 'l.db'); ledger.ensure_account('default', 10_000_000)
    run_id = ledger.admit(definition(), Budget(max_turns=1), prompt_bytes=10, instruction='x')
    intake = ArtifactIntake(ledger, InMemoryArtifactStore())
    first = intake.accept(run_id, token=None, name='a.txt', mime='text/plain', data=b'hi')
    assert intake.accept(run_id, token=None, name='a.txt', mime='text/plain', data=b'hi') == first
    assert len(ledger.artifacts(run_id)) == 1


def test_collect_outputs_ignores_nested_paths_even_if_listed():
    from maef_api.outputs import collect_outputs
    from maef_api.sandbox import FakeSession
    files = {'/workspace/out/top.txt': b'top', '/workspace/out/sub/deep.txt': b'deep'}
    session = FakeSession(lambda argv, stdin: listing(*files), files=files)
    names = [f.name for f in collect_outputs(session).files]
    assert names == ['top.txt']
