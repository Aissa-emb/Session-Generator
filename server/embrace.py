"""Read-only Embrace MCP verification using the official SDK and a service-account token.

Tool schemas are discovered before invocation. Remote tool text is evidence, never
instructions or executable code. No telemetry or tenant mutation tool is exposed.
"""
import json
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ALLOWED = {'list_apps', 'get_app_details', 'get_session_distribution', 'list_network_domains', 'list_root_spans',
           'get_web_vital_distribution', 'get_web_vital_timeseries', 'list_exceptions'}


async def query(token, calls):
    import jsonschema
    outputs = []
    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + token}, follow_redirects=False, timeout=45) as http:
        async with streamable_http_client('https://mcp.embrace.io/mcp', http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                available = {t.name: t for t in (await session.list_tools()).tools}
                for name, args in calls:
                    if name not in ALLOWED or name not in available:
                        outputs.append({'tool': name, 'state': 'needs_action', 'reason': 'Read tool unavailable'})
                        continue
                    try:
                        jsonschema.validate(args, available[name].input_schema)
                    except jsonschema.ValidationError:
                        outputs.append({'tool': name, 'state': 'needs_action', 'reason': 'Tool schema changed; review the current input schema', 'schema': available[name].input_schema})
                        continue
                    response = await session.call_tool(name, args)
                    body = response.model_dump(mode='json')
                    if len(json.dumps(body)) > 2 * 1024 * 1024:
                        raise ValueError('Verification response exceeds the size limit.')
                    outputs.append({'tool': name, 'arguments': args, 'state': 'error' if response.is_error else 'observed', 'result': body})
    return outputs
