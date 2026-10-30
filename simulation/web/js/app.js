// Uygulama: yapılandırmayı alır, haritayı ve paneli kurar, WebSocket olaylarını işler.
// Durumun sahibi sunucudur; tarayıcı yalnızca gösterir (tüm sekmeler aynı durumu görür).
import { connect, getJSON, post } from './ws.js';
import { MeshMap } from './map.js';
import { UI } from './ui.js';

const ALARM = new Set(['GAZ', 'ISI', 'YANGIN']);

const store = {
  connected: false,
  offset: 0,                // sunucu saati − tarayıcı saati (ms); aynı makinede 0
  now() { return Date.now() + this.offset; },
  source: {},
  nodes: {},
  incidents: {},
  route: null,
  disabled: [],
  history: {},
  lineToScreen: {},         // olay no → satırın sunucuya gelişinden ekrana yansımasına geçen süre (ölçüm)
  pendingMeasure: {},
};

async function main() {
  let config;
  try {
    config = await getJSON('/api/config');
  } catch (err) {
    document.getElementById('system-status').textContent = 'SUNUCUYA ULAŞILAMADI';
    return;
  }

  const report = (res) => {
    if (res && res.ok === false) {
      ui.appendLog({ at: Date.now(), level: 'warn', tag: '', text: `İşlem yapılamadı: ${res.message || 'bilinmeyen hata'}` });
    }
    return res;
  };

  let map = null;
  let layer = 'imagery';
  const actions = {
    trigger: () => post('/api/demo/trigger').then(report),
    reset: () => post('/api/reset').then(report),
    silence: (target) => post('/api/command', { target: Number(target), cmd: 'silence' }).then(report),
    closeIncident: (id) => post(`/api/incident/${id}/close`).then(report),
    resetView: () => map.frame(true),
    setLayer: (name) => { layer = name; if (map) map.setBaseLayer(name); },
  };

  const ui = new UI(config, store, actions);
  const mapHooks = {
    getNode: (id) => store.nodes[id],
    getDisabled: () => store.disabled,
    now: () => store.now(),
    onAction: (act, id) => post(`/api/node/${encodeURIComponent(id)}/${act === 'toggle' ? 'toggle' : 'simulate'}`).then(report),
    onTileStatus: (bad) => ui.setMapStatus(bad ? 'Harita karosu çevrimdışı — bu alan önbellekte yok (prefetch_tiles.py)' : ''),
  };
  // WebGL yoksa (GPU hızlandırma kapalı) harita açılamaz; paneller ve olay akışı yine çalışmalı.
  try {
    map = new MeshMap(document.getElementById('map'), config, mapHooks);
    map.setBaseLayer(layer);
  } catch (err) {
    console.error(err);
    map = new Proxy({}, { get: () => () => {} });
    ui.setMapStatus('Harita açılamadı: tarayıcıda WebGL kapalı. Donanım hızlandırmayı açın ya da başka tarayıcı deneyin.');
  }
  window.orman = { store, map };    // tarayıcı konsolunda sorun giderme için

  // Satır → ekran ölçümü: sunucunun satırı seri porttan okuduğu an (rx_at) ile tarayıcının
  // değişikliği çizdiği kare arasındaki süre. Kabul ölçütü: ≤ 150 ms.
  function measure(node, rxAt) {
    requestAnimationFrame(() => {
      const ms = Math.max(0, Math.round(store.now() - rxAt));
      if (ms > 5000) return;
      store.pendingMeasure[node] = { ms, at: Date.now() };
      attachMeasure();
    });
  }
  function attachMeasure() {
    let changed = false;
    for (const inc of Object.values(store.incidents)) {
      const p = store.pendingMeasure[inc.source];
      if (p && store.lineToScreen[inc.id] == null && Math.abs(p.at - inc.started_at + store.offset) < 3000) {
        store.lineToScreen[inc.id] = p.ms;
        changed = true;
      }
    }
    if (changed) ui.renderIncident();
  }

  function setNode(node, rxAt) {
    const prev = store.nodes[node.id];
    store.nodes[node.id] = node;
    if (node.tur === 'physical') ui.renderNode(node.id);
    map.updateNode(node.id);
    ui.renderTop();
    if (rxAt && ALARM.has(node.status) && (!prev || !ALARM.has(prev.status))) measure(node.id, rxAt);
  }

  function applySnapshot(s) {
    const off = s.server_time - Date.now();
    store.offset = Math.abs(off) < 250 ? 0 : off;
    store.source = s.source || {};
    store.nodes = {};
    for (const n of s.nodes) store.nodes[n.id] = n;
    store.route = s.route;
    store.disabled = s.disabled || [];
    store.incidents = {};
    for (const i of s.incidents) store.incidents[i.id] = i;
    store.history = s.history || {};
    ui.renderTop();
    for (const n of config.nodes) {
      if (n.tur === 'physical') ui.renderNode(n.id);
      map.updateNode(n.id);
    }
    ui.renderIncident();
    ui.renderLog(s.log || []);
    map.refreshLinks();
    map.stopPropagation();
    map.setBaseRoute(s.route ? s.route.path : null);
    const props = (s.propagations || []).sort((a, b) => b.started_at - a.started_at);
    if (props.length) map.startPropagation(props[0]);
  }

  const handlers = {
    snapshot: applySnapshot,
    telemetry(ev) {
      if (ev.point) {
        const h = (store.history[ev.node.id] ||= []);
        h.push(ev.point);
        const cutoff = ev.point[0] - 65000;
        while (h.length && h[0][0] < cutoff) h.shift();
      }
      setNode(ev.node, ev.rx_at);
    },
    node_state(ev) {
      setNode(ev.node, ev.rx_at);
    },
    link() {},
    source_status(ev) {
      store.source = ev.source;
      ui.renderTop();
      for (const n of config.nodes) if (n.tur === 'physical') ui.renderNode(n.id);
    },
    route(ev) {
      store.route = ev.route;
      store.disabled = ev.disabled || [];
      map.refreshLinks();
      map.setBaseRoute(ev.route ? ev.route.path : null);
      for (const n of config.nodes) map.updateNode(n.id);
    },
    propagation(ev) {
      map.startPropagation(ev);
    },
    incident_created(ev) {
      store.incidents[ev.incident.id] = ev.incident;
      ui.selectedIncident = ev.incident.id;
      ui.renderIncident();
      ui.flashIncident();
      attachMeasure();
    },
    incident_updated(ev) {
      store.incidents[ev.incident.id] = ev.incident;
      ui.renderIncident();
    },
    incident_received(ev) {
      store.incidents[ev.incident.id] = ev.incident;
      ui.renderIncident();
    },
    dispatch(ev) {
      const inc = store.incidents[ev.incident];
      if (!inc) return;
      inc.dispatch[ev.agency] = { ...(inc.dispatch[ev.agency] || {}), status: ev.status, status_text: ev.status_text, at: ev.at };
      ui.renderIncident();
    },
    incident_closed(ev) {
      delete store.incidents[ev.id];
      map.stopPropagation(ev.id);
      ui.renderIncident();
    },
    log(ev) {
      ui.appendLog(ev.entry);
    },
    reset() {
      store.incidents = {};
      store.lineToScreen = {};
      ui.silenceSent.clear();
      map.stopPropagation();
    },
  };

  connect(
    (ev) => {
      const fn = handlers[ev.type];
      if (fn) fn(ev);
    },
    (up) => {
      store.connected = up;
      ui.renderTop();
    },
  );
}

main();
