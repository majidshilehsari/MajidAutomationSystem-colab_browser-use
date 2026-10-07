// Real WebSocket client used by tests/test_server.py to prove that the
// /websockify path still reaches x11vnc after the API was layered on top.
import WebSocket from 'ws';

const url = process.argv[2];
const ws = new WebSocket(url, { perMessageDeflate: false });
let stage = 0;

const bail = (code, label) => {
  console.log(label);
  try { ws.close(); } catch (_) {}
  process.exit(code);
};

ws.on('open', () => console.log('OPEN'));
ws.on('message', (data) => {
  const text = data.toString();
  console.log('MSG:' + text);
  if (stage === 0) {
    stage = 1;
    ws.send(Buffer.from('PING'));
  } else {
    bail(0, 'DONE');
  }
});
ws.on('error', (err) => bail(1, 'ERR:' + err.message));
setTimeout(() => bail(2, 'TIMEOUT'), 10000);
