// Fake Chrome DevTools Protocol endpoint: HTTP /json/list plus a WebSocket that
// answers Runtime.evaluate. Used by tests/test_cdp.py to exercise the real
// client in automation/cdp.py.
import http from 'node:http';
import { WebSocketServer } from 'ws';

const dom = {
  url: 'https://example.com/search?q=panda',
  title: 'Example search',
  scroll: { x: 0, y: 0 },
  window: { screenX: 0, screenY: 0, innerWidth: 1366, innerHeight: 650,
            outerWidth: 1366, outerHeight: 768, devicePixelRatio: 1 },
  bodyText: 'Search results for cute panda',
  elementCount: 2,
  elements: [
    { tag: 'input', selector: '#search', text: '', type: 'text', href: null,
      visible: true, page: { x: 683, y: 300 }, size: { w: 400, h: 30 },
      desktop: { x: 683, y: 418 } },
    { tag: 'a', selector: 'a.result', text: 'Pandas', type: null,
      href: 'https://example.com/pandas', visible: true,
      page: { x: 200, y: 400 }, size: { w: 120, h: 18 },
      desktop: { x: 200, y: 518 } },
  ],
};

const server = http.createServer((req, res) => {
  if (req.url === '/json/list') {
    const port = server.address().port;
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify([
      { type: 'service_worker', id: 'sw', url: 'about:blank', title: 'sw' },
      { type: 'page', id: 'PAGE1', url: dom.url, title: dom.title,
        webSocketDebuggerUrl: `ws://127.0.0.1:${port}/devtools/page/PAGE1` },
    ]));
    return;
  }
  if (req.url === '/json/broken') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end('not json at all');
    return;
  }
  res.writeHead(404);
  res.end('nope');
});

const wss = new WebSocketServer({ server, path: '/devtools/page/PAGE1' });
wss.on('connection', (ws) => {
  ws.on('message', (raw) => {
    const request = JSON.parse(raw.toString());
    if (request.method === 'Runtime.evaluate') {
      ws.send(JSON.stringify({
        id: request.id,
        result: { result: { type: 'object', value: dom } },
      }));
    } else {
      ws.send(JSON.stringify({ id: request.id, error: { message: 'unsupported' } }));
    }
  });
});

server.listen(0, '127.0.0.1', () => {
  console.log('PORT:' + server.address().port);
});
