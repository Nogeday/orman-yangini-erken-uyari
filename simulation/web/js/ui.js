// Panel arayüzü: üst çubuk, fiziksel düğüm kartları (+ gaz çizgisi), olay, kayıt.
// Her sayı ya ölçülmüştür (F1/F2 satırları) ya da belgelenmiş modelden hesaplanmıştır ve "simüle" yazar.

const MAX_LOG = 200;
const ALARM = new Set(['GAZ', 'ISI', 'YANGIN']);

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const pad = (n, w = 2) => String(n).padStart(w, '0');
export function hms(ms, tenths = false) {
  if (!ms) return '—';
  const d = new Date(ms);
  const base = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return tenths ? `${base}.${Math.floor(d.getMilliseconds() / 100)}` : base;
}

function ago(ms, now) {
  if (!ms) return '—';
  const s = Math.max(0, (now - ms) / 1000);
  if (s < 10) return `${s.toFixed(1)} s önce`;
  if (s < 120) return `${Math.round(s)} s önce`;
  return `${Math.round(s / 60)} dk önce`;
}

export class UI {
  constructor(config, store, actions) {
    this.cfg = config;
    this.store = store;
    this.actions = actions;
    this.byId = Object.fromEntries(config.nodes.map((n) => [n.id, n]));
    this.physical = config.nodes.filter((n) => n.tur === 'physical');
    this.el = {
      area: document.getElementById('area-title'),
      status: document.getElementById('system-status'),
      source: document.getElementById('source-label'),
      clock: document.getElementById('clock'),
      cards: document.getElementById('node-cards'),
      incident: document.getElementById('incident'),
      incSection: document.getElementById('sec-incident'),
      log: document.getElementById('log'),
      mapStatus: document.getElementById('map-status'),
      range: document.getElementById('lg-range'),
    };
    this.el.area.textContent = config.area.title;
    this.el.range.textContent = (config.mesh.range_m / 1000).toLocaleString('tr-TR');
    this.selectedIncident = null;
    this.silenceSent = new Set();
    this.buildCards();
    document.getElementById('btn-trigger').addEventListener('click', () => actions.trigger());
    document.getElementById('btn-reset').addEventListener('click', () => actions.reset());
    document.getElementById('btn-view').addEventListener('click', () => actions.resetView());
    this.el.incident.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-act]');
      if (!btn) return;
      const id = Number(btn.dataset.id);
      if (btn.dataset.act === 'silence') {
        this.silenceSent.add(id);
        actions.silence(btn.dataset.target);
        this.renderIncident();
      } else if (btn.dataset.act === 'close') {
        actions.closeIncident(id);
      } else if (btn.dataset.act === 'select') {
        this.selectedIncident = id;
        this.renderIncident();
      }
    });
    setInterval(() => this.tick(), 250);
  }

  // ---------- üst çubuk ----------
  renderTop() {
    const s = this.store;
    const el = this.el.status;
    if (!s.connected) {
      el.textContent = 'SUNUCU BAĞLANTISI YOK';
      el.dataset.level = 'warn';
    } else {
      const alarms = [];
      const lost = [];
      for (const n of this.cfg.nodes) {
        const st = s.nodes[n.id];
        if (!st) continue;
        if (ALARM.has(st.status)) alarms.push(`${n.id} – ${st.status_text}`);
        else if (n.tur === 'physical' && st.status === 'BAGLANTI_YOK') lost.push(n.id);
      }
      if (alarms.length) {
        el.textContent = `ALARM: ${alarms.join(' · ')}`;
        el.dataset.level = 'alarm';
      } else if (lost.length) {
        el.textContent = `UYARI: ${lost.join(', ')} – BAĞLANTI YOK`;
        el.dataset.level = 'warn';
      } else {
        el.textContent = 'NORMAL';
        el.dataset.level = 'ok';
      }
    }
    const src = s.source || {};
    this.el.source.textContent = src.label || '—';
    this.el.source.title = src.message || '';
    this.el.source.dataset.state = src.state || 'ok';
  }

  tick() {
    const now = this.store.now();
    this.el.clock.textContent = hms(Date.now());
    for (const n of this.physical) {
      const st = this.store.nodes[n.id];
      const agoEl = this.el.cards.querySelector(`[data-ago="${n.id}"]`);
      if (agoEl) agoEl.textContent = ago(st && st.last_seen, now);
    }
    this.drawSpark();
  }

  setMapStatus(text) {
    this.el.mapStatus.hidden = !text;
    this.el.mapStatus.textContent = text || '';
  }

  // ---------- düğüm kartları ----------
  buildCards() {
    this.el.cards.innerHTML = this.physical.map((n) => `
      <article class="node-card" data-node="${esc(n.id)}" data-status="BAGLANTI_YOK">
        <header>
          <span class="node-chip">${esc(n.id)}</span>
          <span class="node-title">${esc(n.rol === 'sensor' ? 'Saha düğümü' : 'Köprü düğümü')}</span>
          <span class="badges" data-badges></span>
          <span class="node-status" data-status-text>BAĞLANTI YOK</span>
        </header>
        <dl class="metrics">
          <div><dt>Sıcaklık</dt><dd class="num" data-k="t">—</dd></div>
          <div><dt>Nem</dt><dd class="num" data-k="h">—</dd></div>
          <div><dt>Gaz (mV)</dt><dd class="num" data-k="gas" title="MQ gerilimi ve uyarlanabilir tabandan farkı (Δ)">—</dd></div>
          <div><dt>Son veri</dt><dd class="num" data-ago="${esc(n.id)}">—</dd></div>
          <div><dt>Bağlantı</dt><dd class="num" data-k="lq">—</dd></div>
        </dl>
        ${n.rol === 'sensor' ? `<div class="spark-wrap"><canvas data-spark="${esc(n.id)}"></canvas>
          <div class="spark-cap">Gaz, son 60 s<br><span class="num" data-spark-range></span></div></div>` : ''}
      </article>`).join('');
  }

  renderNode(id) {
    const card = this.el.cards.querySelector(`[data-node="${id}"]`);
    const st = this.store.nodes[id];
    if (!card || !st) return;
    const cfg = this.byId[id];
    card.dataset.status = st.status;
    card.classList.toggle('is-alarm', ALARM.has(st.status));
    card.querySelector('[data-status-text]').textContent = st.status_text;
    card.querySelector('[data-badges]').innerHTML = (st.badges || [])
      .map((b) => `<span class="badge" data-k="${esc(b)}">${esc(b)}</span>`).join('');
    const tel = st.tel || {};
    const set = (k, v) => { card.querySelector(`[data-k="${k}"]`).innerHTML = v; };
    set('t', tel.t == null ? '—' : `${tel.t.toFixed(1)} °C`);
    set('h', tel.h == null ? '—' : `%${Math.round(tel.h)}`);
    if (tel.gas == null) set('gas', '—');
    else {
      const d = tel.gas - tel.base;
      set('gas', `${tel.gas} <small>Δ${d >= 0 ? '+' : ''}${d}</small>`);
    }
    if (cfg.rol === 'sensor') {
      if (tel.via === 'usb') set('lq', 'USB');
      else set('lq', tel.lq == null || tel.lq < 0 ? '—' : `%${tel.lq}`);
    } else {
      const mode = (this.store.source || {}).mode;
      set('lq', mode === 'hardware' ? 'USB' : mode === 'demo' ? 'DEMO' : mode === 'replay' ? 'KAYIT' : '—');
    }
    const agoEl = card.querySelector(`[data-ago="${id}"]`);
    if (agoEl) agoEl.textContent = ago(st.last_seen, this.store.now());
    if (cfg.rol === 'sensor') this.drawSpark();
  }

  drawSpark() {
    const canvas = this.el.cards.querySelector('canvas[data-spark]');
    if (!canvas) return;
    const id = canvas.dataset.spark;
    const pts = this.store.history[id] || [];
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
    if (canvas.height !== Math.round(h * dpr)) canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const now = this.store.now();
    const t0 = now - 60000;
    const recent = pts.filter((p) => p[0] >= t0);
    const range = this.el.cards.querySelector('[data-spark-range]');
    if (recent.length < 2) {
      if (range) range.textContent = '';
      return;
    }
    let lo = Infinity;
    let hi = -Infinity;
    for (const [, g, b] of recent) {
      lo = Math.min(lo, g, b);
      hi = Math.max(hi, g, b);
    }
    const span = Math.max(40, hi - lo);
    const x = (t) => ((t - t0) / 60000) * (w - 2) + 1;
    const y = (v) => h - 3 - ((v - lo) / span) * (h - 6);
    const css = getComputedStyle(document.documentElement);
    ctx.lineWidth = 1;
    ctx.strokeStyle = css.getPropertyValue('--lost').trim();
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    recent.forEach(([t, , b], i) => (i ? ctx.lineTo(x(t), y(b)) : ctx.moveTo(x(t), y(b))));
    ctx.stroke();
    ctx.setLineDash([]);
    const status = (this.store.nodes[id] || {}).status;
    ctx.strokeStyle = css.getPropertyValue(status === 'YANGIN' ? '--alarm' : ALARM.has(status) ? '--warn' : '--normal').trim();
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    recent.forEach(([t, g], i) => (i ? ctx.lineTo(x(t), y(g)) : ctx.moveTo(x(t), y(g))));
    ctx.stroke();
    if (range) range.textContent = `${Math.round(lo)}–${Math.round(hi)} mV`;
  }

  // ---------- olay ----------
  currentIncident() {
    const list = Object.values(this.store.incidents).sort((a, b) => b.id - a.id);
    if (!list.length) return [null, []];
    const sel = list.find((i) => i.id === this.selectedIncident) || list[0];
    return [sel, list.filter((i) => i !== sel)];
  }

  flashIncident() {
    const sec = this.el.incSection;
    sec.classList.remove('flash');
    void sec.offsetWidth;   // animasyonu yeniden başlat
    sec.classList.add('flash');
  }

  renderIncident() {
    const [inc, others] = this.currentIncident();
    if (!inc) {
      this.el.incident.innerHTML = `<div class="inc-none"><p>Aktif olay yok</p>
        <p class="muted">F1'in MQ sensörüne çakmak gazı tutun ya da “Simüle Çakmak Tetikle”ye basın.</p></div>`;
      return;
    }
    const m = this.cfg.mesh;
    const lora = m.lora;
    const src = this.byId[inc.source] || {};
    const role = { sensor: 'Saha düğümü', gateway: 'Köprü düğümü', relay: 'Sanal LoRa rölesi' }[src.rol] || '';
    const tags = [inc.tag].filter(Boolean).map((t) => `<span class="badge" data-k="${esc(t)}">${esc(t)}</span>`).join('');
    const alarmNow = inc.alarm === inc.alarm_peak
      ? ''
      : (inc.alarm ? ` <span class="k">(şu an ${esc(inc.alarm_text)})</span>` : ' <span class="k">(kaynak şu an normal)</span>');
    const lines = [];
    lines.push(`<li><span class="inc-alarm" data-alarm="${inc.alarm_peak}">${esc(inc.alarm_peak_text)}</span>${alarmNow}
      · <span class="num">${inc.lat.toFixed(5)}, ${inc.lon.toFixed(5)}</span> · <span class="num">${inc.rakim_m} m</span></li>`);
    if (inc.route) {
      lines.push(`<li><span class="k">Rota:</span> ${esc(inc.route.join(' → '))}
        <small>${inc.hops} hop: ${inc.rf_hops} gerçek RF (nRF24) + ${inc.lora_hops} simüle LoRa</small></li>`);
      const perHop = m.airtime_ms + lora.processing_ms;
      lines.push(`<li><span class="k">Hesaplanan ağ gecikmesi:</span> <span class="num">${Math.round(inc.latency_ms)} ms</span> — simülasyon
        <small>${inc.lora_hops} hop × ~${Math.round(perHop)} ms (${m.airtime_ms} hava + ${lora.processing_ms} işlem) · SF${lora.sf}/${lora.bw_hz / 1000} kHz</small></li>`);
    } else {
      lines.push('<li class="k">Rota yok — MERKEZ\'e ulaşılamıyor (devre dışı düğümleri etkinleştirin)</li>');
    }
    const measured = this.store.lineToScreen[inc.id];
    if (measured != null) lines.push(`<li><span class="k">Satır → ekran:</span> <span class="num">${measured} ms</span> <span class="k">(bu tarayıcıda ölçüldü)</span></li>`);
    const durum = [inc.status === 'merkez_aldi'
      ? `<span class="inc-ok">MERKEZ aldı ${hms(inc.received_at)}</span>`
      : esc(inc.status_text)];
    if (inc.silenced_at) durum.push(`<small>${esc(inc.source)} onayladı: siren susturuldu (${hms(inc.silenced_at)})</small>`);
    else if (this.silenceSent.has(inc.id)) durum.push('<small>Susturma komutu gönderildi, onay bekleniyor…</small>');
    if (inc.source_normal_at) durum.push(`<small>Kaynak normale döndü ${hms(inc.source_normal_at)}</small>`);
    const tg = inc.notify && inc.notify.telegram;
    if (tg) durum.push(`<small>Telegram: ${esc(tg.text)}${tg.detail ? ` (${esc(tg.detail)})` : ''} ${hms(tg.at)}</small>`);
    lines.push(`<li>${durum.join('')}</li>`);
    const agencies = this.cfg.agencies.map((a) => {
      const d = inc.dispatch[a.id] || { status: 'bekliyor', status_text: 'Bekliyor', at: null };
      return `<li data-status="${esc(d.status)}"><span>${esc(a.name)}</span><span class="ag-status">${esc(d.status_text)}</span>
        <span class="ag-time num">${d.at ? hms(d.at, true) : '—'}</span></li>`;
    }).join('');
    const silenceTarget = inc.source === 'F1' ? '1' : inc.source === 'F2' ? '2' : '';
    const canSilence = inc.can_silence && silenceTarget && !inc.silenced_at && inc.alarm > 0;
    this.el.incident.innerHTML = `
      <div class="inc-head"><strong class="num">#${esc(inc.no)}</strong><span class="num">${hms(inc.started_at)}</span>
        <span class="src">${esc(inc.source)} ${esc(role)}</span><span class="badges">${tags}</span></div>
      <ul class="inc-lines">${lines.join('')}</ul>
      <ul class="agencies" aria-label="Kurum bildirimleri (simülasyon)">${agencies}</ul>
      <div class="inc-actions">
        <button class="btn" data-act="silence" data-id="${inc.id}" data-target="${silenceTarget}"${canSilence ? '' : ' disabled'}>Sahadaki sireni sustur</button>
        <button class="btn" data-act="close" data-id="${inc.id}">Olayı kapat</button>
      </div>
      ${others.length ? `<div class="inc-other">Diğer aktif olaylar: ${others.map((o) =>
        `<button data-act="select" data-id="${o.id}">#${esc(o.no)} ${esc(o.source)}</button>`).join(', ')}</div>` : ''}`;
  }

  // ---------- kayıt ----------
  renderLog(entries) {
    this.el.log.innerHTML = '';
    for (const e of entries.slice(-MAX_LOG)) this.appendLog(e);
  }

  appendLog(e) {
    const li = document.createElement('li');
    li.dataset.level = e.level || 'info';
    li.innerHTML = `<time>${hms(e.at, true)}</time>${e.tag ? `<span class="tag">${esc(e.tag)}</span>` : ''}<span class="txt">${esc(e.text)}</span>`;
    this.el.log.prepend(li);
    while (this.el.log.childElementCount > MAX_LOG) this.el.log.lastElementChild.remove();
  }
}
