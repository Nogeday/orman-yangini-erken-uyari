// Sunucu bağlantısı: WebSocket (olay akışı) + REST (operatör eylemleri).
// WebSocket koparsa 1 s'de bir yeniden bağlanır; sunucu her bağlantıda güncel anlık görüntüyü yollar.

export function connect(onEvent, onConnection) {
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
  let ws = null;
  let timer = null;

  function open() {
    ws = new WebSocket(url);
    ws.onopen = () => onConnection(true);
    ws.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch { return; }
      onEvent(ev);
    };
    ws.onclose = () => {
      onConnection(false);
      clearTimeout(timer);
      timer = setTimeout(open, 1000);
    };
    ws.onerror = () => ws.close();
  }

  open();
}

export async function getJSON(path) {
  const res = await fetch(path, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

export async function post(path, body) {
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return await res.json();
  } catch (err) {
    return { ok: false, message: `sunucuya ulaşılamadı (${err.message})` };
  }
}
