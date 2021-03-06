import os
import uvloop
from aiohttp import web
import aiohttp_session
from kubernetes_asyncio import client, config
import logging
from hailtop.auth import async_get_userinfo
from hailtop.tls import internal_server_ssl_context
from hailtop.hail_logging import AccessLogger, configure_logging
from gear import setup_aiohttp_session, maybe_parse_bearer_header

uvloop.install()

configure_logging()
log = logging.getLogger('router-resolver')

app = web.Application()
setup_aiohttp_session(app)

routes = web.RouteTableDef()


@routes.get('/auth/{namespace}')
async def auth(request):
    app = request.app
    k8s_client = app['k8s_client']
    namespace = request.match_info['namespace']

    if 'X-Hail-Internal-Authorization' in request.headers:
        session_id = maybe_parse_bearer_header(
            request.headers['X-Hail-Internal-Authorization'])
    elif 'Authorization' in request.headers:
        session_id = maybe_parse_bearer_header(
            request.headers['Authorization'])
    else:
        session = await aiohttp_session.get_session(request)
        session_id = session.get('session_id')

    if not session_id:
        raise web.HTTPUnauthorized()

    userdata = await async_get_userinfo(session_id=session_id)
    is_developer = userdata is not None and userdata['is_developer'] == 1
    if not is_developer:
        raise web.HTTPUnauthorized()

    try:
        router = await k8s_client.read_namespaced_service('router', namespace)
    except client.rest.ApiException as err:
        if err.status == 404:
            return web.Response(status=403)
        raise
    ports = router.spec.ports
    assert len(ports) == 1
    port = ports[0].port
    if port == 80:
        scheme = 'http'
    else:
        assert port == 443
        scheme = 'https'
    return web.Response(status=200,
                        headers={
                            'X-Router-IP': router.spec.cluster_ip,
                            'X-Router-Scheme': scheme})


@routes.get('/router-scheme/{namespace}')
async def router_scheme(request):
    app = request.app
    k8s_client = app['k8s_client']
    namespace = request.match_info['namespace']

    try:
        router = await k8s_client.read_namespaced_service('router', namespace)
    except client.rest.ApiException as err:
        if err.status == 404:
            return web.Response(status=403)
        raise
    ports = router.spec.ports
    assert len(ports) == 1
    port = ports[0].port
    if port == 80:
        scheme = 'http'
    else:
        assert port == 443
        scheme = 'https'
    return web.Response(status=200,
                        headers={
                            'X-Router-Scheme': scheme})


app.add_routes(routes)


async def on_startup(app):
    if 'BATCH_USE_KUBE_CONFIG' in os.environ:
        await config.load_kube_config()
    else:
        config.load_incluster_config()
    app['k8s_client'] = client.CoreV1Api()


app.on_startup.append(on_startup)

web.run_app(app,
            host='0.0.0.0',
            port=5000,
            access_log_class=AccessLogger,
            ssl_context=internal_server_ssl_context())
