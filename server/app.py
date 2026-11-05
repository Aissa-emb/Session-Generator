"""FastAPI control plane. Bind to loopback locally; remote API access requires a token."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from xcorgen import presets, spec as specs
from xcorgen.security import SecurityError, TransportError
from .service import Service
from . import readiness, secrets, verification, copilot, preparation, delivery_summary, destinations
from .mcp import build_server, caller
from mcp.server.transport_security import TransportSecuritySettings


class PatchBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    patch: list[dict] = Field(max_length=200)
    expected_version: int | None = None


class RunBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    spec_id: str
    mode: str | None = None
    destination: str | None = None
    confirm: bool = False
    dry_run: bool = False
    idempotency_key: str | None = Field(default=None, max_length=100)


class SecretBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    value: str = Field(min_length=1, max_length=8192, repr=False)


def create_app(service=None, enable_integrations=None):
    svc = service or Service()
    experimental = enable_integrations if enable_integrations is not None else os.environ.get("SG_ENABLE_EXPERIMENTAL") == "1"
    allowed_hosts = ['localhost', 'localhost:*', '127.0.0.1', '127.0.0.1:*', '[::1]', '[::1]:*', 'testserver']
    for host in filter(None, os.environ.get('SG_ALLOWED_HOSTS', '').split(',')):
        allowed_hosts += [host, host + ':*']
    mcp_app = build_server(svc).streamable_http_app(stateless_http=True, json_response=True, max_request_body_size=1024*1024,
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts, allowed_origins=['http://localhost:*', 'http://127.0.0.1:*'] + ['https://' + h for h in allowed_hosts if '*' not in h])) if experimental else None
    @asynccontextmanager
    async def lifespan(app):
        rotation = None
        if os.environ.get('SG_STUDIO_LOG'):
            from .retention import start_log_rotation
            rotation = start_log_rotation(os.environ['SG_STUDIO_LOG'])
        # Surviving worker processes own their journals. Never launch a duplicate on restart.
        for run in svc.store.list('runs'):
            if run['status'] in ('queued', 'running') and time.time() - run.get('heartbeat', run['created']) > 90:
                svc.store.update('runs', run['id'], status='failed', recovery_required=True)
                svc.store.event(run['id'], 'recovery_required', message='Worker heartbeat expired; review receipts before resuming.')
        if svc.launch_jobs:
            for record in svc.store.list('preparations'):
                if record['status'] == 'running' and record.get('authorized'):
                    preparation.launch(svc, record['id'])
        if svc.launch_jobs:
            for stream in svc.store.list('streams'):
                if stream['status']=='running' and stream.get('authorized') and stream.get('run_id'):
                    run=svc.store.get('runs',stream['run_id'])
                    if run['status'] in ('running','queued','paused') or (run['status']=='failed' and run.get('recovery_required') and run.get('desired_status')=='running'):
                        svc.store.update('runs',run['id'],desired_status='running',recovery_required=False)
                        svc.launch(run['id'])
        from .audit import emit
        emit('server_started',runner_region=os.environ.get('SG_RUNNER_REGION','local'))
        async with AsyncExitStack() as stack:
            if mcp_app:
                await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
            try:
                yield
            finally:
                if svc.launch_jobs:
                    for stream in svc.store.list('streams'):
                        if stream['status']=='running' and stream.get('run_id'):
                            run=svc.store.get('runs',stream['run_id'])
                            if run['status'] in ('running','queued'):
                                svc.store.update('runs',run['id'],desired_status='paused')
                    deadline=time.monotonic()+65
                    while any(p.poll() is None for p in svc.processes.values()) and time.monotonic()<deadline:
                        await asyncio.sleep(.2)
                emit('server_stopped')
                if rotation:
                    rotation[0].set()
                    rotation[1].join(timeout=1)
                svc.store.close()
    app = FastAPI(title='Session Generator', version='2.0', lifespan=lifespan)
    app.state.service = svc
    if mcp_app:
        app.router.routes.extend(mcp_app.routes)

    @app.middleware('http')
    async def guard(request, call_next):
        host = urlsplit('//' + request.headers.get('host', '')).hostname
        allowed = {'localhost', '127.0.0.1', '::1', 'testserver'} | set(filter(None, os.environ.get('SG_ALLOWED_HOSTS', '').split(',')))
        if host not in allowed:
            return JSONResponse({'detail': 'Unrecognized host.'}, 400)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Cross-origin requests are not allowed.'}, 403)
        if not experimental and request.url.path.startswith(('/mcp', '/api/copilot')):
            return JSONResponse({'detail': 'This integration is disabled.'}, 404)
        client = request.client.host if request.client else ''
        is_local = client in ('127.0.0.1', '::1', 'localhost', 'testclient')
        authorization = request.headers.get('authorization', '')
        actor = None
        if authorization.startswith('Bearer '):
            actor = svc.store.authenticate(authorization[7:])
            if not actor:
                return JSONResponse({'detail': 'Invalid access token.'}, 401)
        if (request.url.path.startswith('/api/') or request.url.path.startswith('/mcp')) and not actor and (not is_local or os.environ.get('SG_REQUIRE_AUTH') == '1' or request.url.path.startswith('/mcp')):
            return JSONResponse({'detail': 'A bearer access token is required.'}, 401)
        request.state.actor = actor or 'user'
        if request.method in ('POST', 'PUT', 'PATCH'):
            length = request.headers.get('content-length')
            if length and (not length.isdigit() or int(length) > 1024 * 1024):
                return JSONResponse({'detail': 'Request exceeds the 1 MiB limit.'}, 413)
            body = bytearray()
            try:
                async with asyncio.timeout(15):
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            return JSONResponse({'detail': 'Request exceeds the 1 MiB limit.'}, 413)
            except TimeoutError:
                return JSONResponse({'detail': 'Request body took longer than 15 seconds.'}, 408)
            request._body = bytes(body)
        context_token = caller.set(request.state.actor)
        try:
            response = await call_next(request)
        finally:
            caller.reset(context_token)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store' if request.url.path.startswith('/api') else 'no-cache'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request, error):
        return JSONResponse({'detail': [{'loc': e['loc'], 'msg': e['msg'], 'type': e['type']} for e in error.errors()]}, 422)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'detail': str(error)}, 422)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({'detail': 'Requested item was not found.'}, 404)

    @app.exception_handler(SecurityError)
    @app.exception_handler(TransportError)
    async def remote_error(request, error):
        return JSONResponse({'detail': 'The remote request could not be completed. Check credentials, endpoint settings and connectivity.'}, 502)

    @app.get('/api/status')
    @app.get('/health')
    def health():
        svc.store.list('settings')
        return {'status': 'ok', 'service': 'session-generator', 'version': '2.0', 'instance': os.environ.get('SG_STUDIO_INSTANCE'), 'pid': os.getpid(), 'active_runs': sum(r['status'] in ('queued', 'running', 'paused') for r in svc.store.list('runs')), 'runner_region':os.environ.get('SG_RUNNER_REGION','local')}

    @app.get('/api/capabilities')
    def capabilities():
        return svc.capabilities()

    @app.get('/api/targets')
    def targets():
        return svc.store.list('targets')

    @app.post('/api/targets', status_code=201)
    def create_target(value: dict = Body()):
        return svc.create_target(value)

    @app.post('/api/targets/{id}/test')
    async def test_target(id: str):
        return await svc.test_target(id)

    @app.get('/api/targets/{id}/readiness')
    def check_readiness(id: str):
        return readiness.check(svc.store, svc.store.get('targets', id))

    @app.put('/api/targets/{id}/readiness/{item}')
    def evidence(id: str, item: str, value: dict = Body()):
        target = svc.store.get('targets', id)
        if item not in {i['id'] for i in readiness.setup_kit(target)['items']} or value.get('state') not in ('unknown', 'ok', 'missing', 'needs_action'):
            raise ValueError('Unknown readiness item or state.')
        if value.get('state') == 'ok' and not value.get('evidence'):
            raise ValueError('Evidence is required before marking an item ready.')
        return svc.store.put('dashboard_setup', {'id': id + ':' + item, 'target_id': id, 'item': item,
            'state': value['state'], 'evidence': str(value.get('evidence', ''))[:8000], 'checked_at': time.time(), 'source': 'operator'})

    @app.get('/api/targets/{id}/setup-kit')
    def setup_kit(id: str, format: str = 'json'):
        kit = readiness.setup_kit(svc.store.get('targets', id))
        if format == 'md':
            return PlainTextResponse(readiness.markdown(kit), media_type='text/markdown')
        if format != 'json':
            raise ValueError('Format must be md or json.')
        return kit

    @app.get('/api/presets')
    def list_presets():
        return presets.listing()

    @app.get('/api/presets/{id}')
    def get_preset(id: str):
        p = presets.catalog_presets()[id]
        return {'id': id, 'title': p[0], 'group': p[1], 'description': p[2], 'fragment': p[3]}

    @app.post('/api/presets/{id}/apply')
    def apply_preset(id: str, value: dict = Body()):
        return svc.require_valid(presets.apply(value, id))

    @app.get('/api/specs')
    def list_specs():
        return svc.store.list('scenario_specs')

    @app.post('/api/specs/validate')
    def validate(value: dict = Body()):
        return svc.validate(value)

    @app.post('/api/specs', status_code=201)
    def create_spec(request: Request, value: dict = Body()):
        return svc.create_spec(value, request.state.actor)

    @app.get('/api/specs/{id}')
    def get_spec(id: str, version: int | None = None):
        return svc.store.spec_version(id, version)

    @app.patch('/api/specs/{id}')
    def patch_spec(id: str, value: PatchBody, request: Request):
        return svc.patch_spec(id, value.patch, request.state.actor, value.expected_version)

    @app.post('/api/specs/{id}/explain')
    def explain(id: str):
        return {'lines': specs.explain(svc.store.get('scenario_specs', id)['spec'])}

    @app.post('/api/regions/plan')
    def region_plan(value: dict):
        from xcorgen.regions import plan
        return plan(svc.require_valid(value['spec']),value['runners'])

    @app.get('/api/specs/{id}/symbols')
    def symbol_plan(id: str):
        from .symbols import plan
        return plan(svc.store.get('scenario_specs',id)['spec'])

    @app.post('/api/specs/{id}/symbols/upload')
    def upload_symbols(id: str, value: dict, request: Request):
        from .symbols import upload
        return upload(svc,id,value.get('confirm',False),request.state.actor)

    @app.post('/api/specs/{id}/preview')
    def preview(id: str):
        return svc.preview(svc.store.get('scenario_specs', id)['spec'])

    @app.post('/api/distributions/preview')
    def distribution_preview(value: dict = Body()):
        from xcorgen.distributions import preview
        return preview(value.get('distribution'), value.get('quantity'), value.get('effect'))

    @app.post('/api/preview')
    def preview_unsaved(value: dict = Body()):
        return svc.preview(value)

    @app.post('/api/runs', status_code=201)
    def start_run(request: Request, value: RunBody):
        return svc.start_run(**value.model_dump(), author=request.state.actor)

    @app.get('/api/runs')
    def list_runs():
        return svc.store.list('runs')

    @app.get('/api/runs/{id}')
    def get_run(id: str):
        run = svc.store.get('runs', id)
        return dict(run, delivery=delivery_summary.summarize(svc.store, run),
                    verifications=svc.store.list_for('verifications', 'run_id', id, limit=50),
                    receipts=svc.store.list_for('receipts', 'run_id', id, limit=500))

    @app.post('/api/runs/{id}/verify')
    async def verify(id: str):
        return await verification.verify(svc.store, svc.store.get('runs', id))

    @app.post('/api/runs/{id}/incidents')
    def schedule(id: str, request: Request, value: dict = Body()):
        return svc.schedule_incident(id, value, request.state.actor)

    @app.delete('/api/runs/{id}/incidents/{incident_id}')
    def cancel(id: str, incident_id: str, request: Request):
        return svc.cancel_incident(id, incident_id, request.state.actor)

    @app.get('/api/runs/{id}/events')
    async def run_events(id: str, request: Request, after: int = 0, once: bool = False):
        svc.store.get('runs', id)
        try:
            cursor = max(after, int(request.headers.get('last-event-id', '0')))
        except ValueError:
            raise HTTPException(400, 'Invalid event cursor') from None
        async def stream():
            nonlocal cursor
            while True:
                for event in svc.store.events(id, cursor):
                    cursor = event['id']
                    yield f'id: {cursor}\nevent: progress\ndata: {json.dumps(event)}\n\n'
                if once or await request.is_disconnected():
                    break
                yield ': keepalive\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.post('/api/runs/{id}/observations')
    def observations(id: str, value: dict = Body()):
        return verification.compare_observations(svc.store, svc.store.get('runs', id), value)

    @app.post('/api/runs/{id}/{action}')
    def control_run(id: str, action: str, request: Request):
        return svc.control(id, action, request.state.actor)

    @app.post('/api/preparations/preview')
    def preparation_plan(value: dict = Body()):
        allowed = {'spec_id','history_hours','rate_per_minute','minutes_until_demo','duration_minutes','prepare_ahead','incident'}
        if set(value) - allowed:
            raise ValueError('Unknown preparation option.')
        return preparation.plan(svc, **value)

    @app.get('/api/preparations')
    def preparation_list():
        return svc.store.list('preparations')

    @app.get('/api/preparations/{id}')
    def preparation_status(id: str):
        return svc.store.get('preparations', id)

    @app.post('/api/preparations/{id}/start')
    def preparation_start(id: str, request: Request, value: dict = Body()):
        return preparation.start(svc, id, value.get('confirm',False), value.get('dry_run',False), request.state.actor)

    @app.post('/api/preparations/{id}/cancel')
    def preparation_cancel(id: str, request: Request):
        return preparation.cancel(svc,id,request.state.actor)

    @app.get('/api/copilot/turns')
    def copilot_history(spec_id: str):
        return svc.store.list_for('copilot_turns', 'spec_id', spec_id, limit=100)

    @app.post('/api/copilot/turns')
    async def copilot_turn(request: Request, value: dict = Body()):
        return await asyncio.wait_for(copilot.turn(svc, value['spec_id'], value['message'], request.state.actor), timeout=180)

    @app.post('/api/copilot/turns/{id}/apply')
    def apply_proposal(id: str, value: dict = Body()):
        return copilot.apply(svc, id, value.get('confirm', False))

    @app.post('/api/copilot/turns/{id}/discard')
    def discard_proposal(id: str):
        return copilot.discard(svc, id)

    @app.get('/api/settings')
    def settings():
        defaults = next((s for s in svc.store.list('settings') if s['id'] == 'defaults'), {})
        configured = secrets.status(svc.store)
        if not experimental:
            configured.pop('anthropic', None)
        return {'secrets': configured, 'secret_suffixes': {k: v for k, v in secrets.masks(svc.store).items() if k in configured}, 'defaults': defaults.get('value', {}), 'models': ['claude-sonnet-5-5', 'claude-opus-5-5'] if experimental else []}

    @app.get('/api/destinations')
    def saved_destinations():
        return destinations.listing(svc.store)

    @app.post('/api/destinations', status_code=201)
    def save_destination(value: dict = Body()):
        return destinations.save(svc, value)

    @app.put('/api/destinations/{id}')
    def update_destination(id: str, value: dict = Body()):
        return destinations.save(svc, value, id)

    @app.post('/api/destinations/{id}/select')
    def select_destination(id: str):
        return destinations.select(svc, id)

    @app.delete('/api/destinations/{id}')
    def remove_destination(id: str):
        return destinations.remove(svc, id)

    @app.put('/api/settings/defaults')
    def defaults(value: dict = Body()):
        allowed = {'model', 'destination', 'runner_region'}
        if set(value) - allowed or value.get('model', 'claude-sonnet-5-5') not in ('claude-sonnet-5-5', 'claude-opus-5-5'):
            raise ValueError('Unsupported default setting.')
        return svc.store.put('settings', {'id': 'defaults', 'value': value})

    @app.put('/api/settings/secrets/{name}')
    def save_secret(name: str, value: SecretBody):
        if name == 'anthropic' and not experimental:
            raise HTTPException(404, 'This integration is disabled.')
        return secrets.save(svc.store, name, value.value)

    @app.get('/api/settings/tokens')
    def tokens():
        return [{k: v for k, v in r.items() if k != 'hash'} for r in svc.store.list('api_tokens')]

    @app.post('/api/settings/tokens', status_code=201)
    def new_token(value: dict = Body()):
        return svc.store.create_token(value.get('name'))

    @app.delete('/api/settings/tokens/{id}')
    def revoke_token(id: str):
        row = svc.store.update('api_tokens', id, revoked=True)
        return {k: v for k, v in row.items() if k != 'hash'}

    @app.post('/api/targets/{id}/probe')
    def probe(id: str, request: Request, value: dict = Body()):
        return svc.forwarding_probe(id, value['spec_id'], value.get('confirm', False), value.get('dry_run', False), request.state.actor, value.get('idempotency_key'))

    @app.get('/api/streams')
    def list_streams():
        return [dict(s, summary=svc.stream_summary(s['id'])) for s in svc.store.list('streams')]

    @app.post('/api/streams', status_code=201)
    def create_stream(request: Request, value: dict = Body()):
        return svc.create_stream(value['spec_id'], value['name'], value.get('confirm', False), value.get('dry_run', False), request.state.actor, value.get('idempotency_key'))

    @app.get('/api/streams/{id}')
    def get_stream(id: str):
        return dict(svc.store.get('streams', id), summary=svc.stream_summary(id))

    @app.patch('/api/streams/{id}')
    def update_stream(id: str, value: dict = Body()):
        if set(value) - {'name'} or not isinstance(value.get('name'), str) or not 1 <= len(value['name']) <= 100:
            raise ValueError('Supply a stream name of 1–100 characters. Edit its scenario through the versioned spec API.')
        return svc.store.update('streams', id, name=value['name'])

    @app.delete('/api/streams/{id}')
    def delete_stream(id: str, request: Request):
        svc.control_stream(id, 'stop', author=request.state.actor)
        svc.store.delete('streams', id)
        return {'deleted': True}

    @app.get('/api/streams/{id}/summary')
    def summary(id: str, day: str | None = None):
        return svc.stream_summary(id, day)

    @app.post('/api/streams/{id}/{action}')
    def control_stream(id: str, action: str, request: Request, value: dict = Body(default={})):
        return svc.control_stream(id, action, value.get('confirm', False), request.state.actor)

    @app.get('/{path:path}', include_in_schema=False)
    def ui(path: str):
        if path.startswith(('api/', 'mcp')):
            raise HTTPException(404)
        root = (Path(__file__).resolve().parents[1] / 'apps/web/dist').resolve()
        file = (root / path).resolve()
        if file.is_relative_to(root) and file.is_file():
            return FileResponse(file)
        if (root / 'index.html').exists():
            return FileResponse(root / 'index.html')
        return JSONResponse({'message': 'Build the UI with npm --prefix apps/web run build.', 'api': '/docs'})
    return app


app = create_app()
