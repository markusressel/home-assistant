"""Hass.io Add-on ingress service."""
import asyncio
from ipaddress import ip_address
import os
from typing import Dict, Union

import aiohttp
from aiohttp import web
from aiohttp import hdrs
from aiohttp.web_exceptions import HTTPBadGateway
from multidict import CIMultiDict

from homeassistant.core import callback
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.typing import HomeAssistantType

from .const import X_HASSIO, X_INGRESS_PATH


@callback
def async_setup_ingress(hass: HomeAssistantType, host: str):
    """Auth setup."""
    websession = hass.helpers.aiohttp_client.async_get_clientsession()

    hassio_ingress = HassIOIngress(host, websession)
    hass.http.register_view(hassio_ingress)


class HassIOIngress(HomeAssistantView):
    """Hass.io view to handle base part."""

    name = "api:hassio:ingress"
    url = "/api/hassio_ingress/{addon}/{path:.+}"
    requires_auth = False

    def __init__(self, host: str, websession: aiohttp.ClientSession):
        """Initialize a Hass.io ingress view."""
        self._host = host
        self._websession = websession

    def _create_url(self, addon: str, path: str) -> str:
        """Create URL to service."""
        return "http://{}/addons/{}/web/{}".format(self._host, addon, path)

    async def _handle(
            self, request: web.Request, addon: str, path: str
    ) -> Union[web.Response, web.StreamResponse, web.WebSocketResponse]:
        """Route data to Hass.io ingress service."""
        try:
            # Websocket
            if _is_websocket(request):
                return await self._handle_websocket(request, addon, path)

            # Request
            return await self._handle_request(request, addon, path)

        except aiohttp.ClientError:
            pass

        raise HTTPBadGateway() from None

    get = _handle
    post = _handle
    put = _handle
    delete = _handle

    async def _handle_websocket(
            self, request: web.Request, addon: str, path: str
    ) -> web.WebSocketResponse:
        """Ingress route for websocket."""
        ws_server = web.WebSocketResponse()
        await ws_server.prepare(request)

        # Preparing
        url = self._create_url(addon, path)
        source_header = _init_header(request, addon)

        # Support GET query
        if request.query_string:
            url = "{}?{}".format(url, request.query_string)

        # Start proxy
        async with self._websession.ws_connect(
                url, headers=source_header
        ) as ws_client:
            # Proxy requests
            await asyncio.wait(
                [
                    _websocket_forward(ws_server, ws_client),
                    _websocket_forward(ws_client, ws_server),
                ],
                return_when=asyncio.FIRST_COMPLETED
            )

        return ws_server

    async def _handle_request(
            self, request: web.Request, addon: str, path: str
    ) -> Union[web.Response, web.StreamResponse]:
        """Ingress route for request."""
        url = self._create_url(addon, path)
        data = await request.read()
        source_header = _init_header(request, addon)

        async with self._websession.request(
                request.method, url, headers=source_header,
                params=request.query, data=data, cookies=request.cookies
        ) as result:
            headers = _response_header(result)

            # Simple request
            if hdrs.CONTENT_LENGTH in result.headers and \
                    int(result.headers.get(hdrs.CONTENT_LENGTH, 0)) < 4194000:
                # Return Response
                body = await result.read()
                return web.Response(
                    headers=headers,
                    status=result.status,
                    body=body
                )

            # Stream response
            response = web.StreamResponse(
                status=result.status, headers=headers)
            response.content_type = result.content_type

            try:
                await response.prepare(request)
                async for data in result.content:
                    await response.write(data)

            except (aiohttp.ClientError, aiohttp.ClientPayloadError):
                pass

            return response


def _init_header(
        request: web.Request, addon: str
) -> Union[CIMultiDict, Dict[str, str]]:
    """Create initial header."""
    headers = {}

    # filter flags
    for name, value in request.headers.items():
        if name in (hdrs.CONTENT_LENGTH, hdrs.CONTENT_TYPE):
            continue
        headers[name] = value

    # Inject token / cleanup later on Supervisor
    headers[X_HASSIO] = os.environ.get('HASSIO_TOKEN', "")

    # Ingress information
    headers[X_INGRESS_PATH] = "/api/hassio_ingress/{}".format(addon)

    # Set X-Forwarded-For
    forward_for = request.headers.get(hdrs.X_FORWARDED_FOR)
    connected_ip = ip_address(request.transport.get_extra_info('peername')[0])
    if forward_for:
        forward_for = "{}, {!s}".format(forward_for, connected_ip)
    else:
        forward_for = "{!s}".format(connected_ip)
    headers[hdrs.X_FORWARDED_FOR] = forward_for

    # Set X-Forwarded-Host
    forward_host = request.headers.get(hdrs.X_FORWARDED_HOST)
    if not forward_host:
        forward_host = request.host
    headers[hdrs.X_FORWARDED_HOST] = forward_host

    # Set X-Forwarded-Proto
    forward_proto = request.headers.get(hdrs.X_FORWARDED_PROTO)
    if not forward_proto:
        forward_proto = request.url.scheme
    headers[hdrs.X_FORWARDED_PROTO] = forward_proto

    return headers


def _response_header(response: aiohttp.ClientResponse) -> Dict[str, str]:
    """Create response header."""
    headers = {}

    for name, value in response.headers.items():
        if name in (hdrs.TRANSFER_ENCODING, hdrs.CONTENT_LENGTH,
                    hdrs.CONTENT_TYPE):
            continue
        headers[name] = value

    return headers


def _is_websocket(request: web.Request) -> bool:
    """Return True if request is a websocket."""
    headers = request.headers

    if headers.get(hdrs.CONNECTION) == "Upgrade" and \
            headers.get(hdrs.UPGRADE) == "websocket":
        return True
    return False


async def _websocket_forward(ws_from, ws_to):
    """Handle websocket message directly."""
    async for msg in ws_from:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await ws_to.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await ws_to.send_bytes(msg.data)
        elif msg.type == aiohttp.WSMsgType.PING:
            await ws_to.ping()
        elif msg.type == aiohttp.WSMsgType.PONG:
            await ws_to.pong()
        elif ws_to.closed:
            await ws_to.close(code=ws_to.close_code, message=msg.extra)
