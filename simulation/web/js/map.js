// 3B harita: gerçek DEM (AWS Terrain Tiles) + uydu görüntüsü, düğümler (HTML Marker), linkler,
// aktif rota ve mesh'te ilerleyen paket. Tüm karolar sunucu vekilinden (/tiles/...) gelir.
/* global maplibregl */

const COLOR = {
  bg: '#0E1116', panel: '#151A21', line: '#262D36', text: '#E6E9EE', route: '#4C9AFF', lost: '#5B6470',
};
const ALARM_STATUSES = new Set(['GAZ', 'ISI', 'YANGIN']);
const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export class MeshMap {
  constructor(container, config, hooks) {
    this.cfg = config;
    this.hooks = hooks;              // getNode(id), getDisabled(), now(), onAction(act, id)
    this.byId = Object.fromEntries(config.nodes.map((n) => [n.id, n]));
    this.markers = {};
    this.prop = null;
    this.raf = null;
    this.ready = false;
    this.popup = null;
    this.popupId = null;
    this.baseRoute = null;
    this.tileErrorUntil = 0;

    const { area } = config;
    const b = area.bounds;
    const minZ = area.map.min_zoom;
    const maxZ = area.map.max_zoom;
    this.map = new maplibregl.Map({
      container,
      style: this.style(),
      center: area.camera.center,
      zoom: area.camera.zoom,
      pitch: area.camera.pitch,
      bearing: area.camera.bearing,
      minZoom: minZ,
      maxZoom: maxZ,
      maxPitch: 75,
      attributionControl: false,
      // maxBounds (bbox + pay): kamera merkezi bu kutuda tutulur. MapLibre'nin hazır maxBounds'u
      // görünümün tamamını kutuya sığdırmak için zoom'u zorla büyütür ve tüm ağ ekrana sığmaz.
      transformConstrain: (lngLat, zoom) => ({
        center: new maplibregl.LngLat(clamp(lngLat.lng, b.west, b.east), clamp(lngLat.lat, b.south, b.north)),
        zoom: clamp(zoom, minZ, maxZ),
      }),
    });
    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
    this.map.addControl(new maplibregl.AttributionControl({ compact: false }), 'bottom-right');
    this.map.on('load', () => this.onLoad());
    this.map.on('error', (e) => this.onError(e));
    this.map.on('click', () => this.closePopup());
  }

  style() {
    const { area, layers } = this.cfg;
    const dem = (attribution) => ({
      type: 'raster-dem', tiles: [`${location.origin}/tiles/dem/{z}/{x}/{y}`], encoding: 'terrarium',
      tileSize: 256, maxzoom: 15, ...(attribution ? { attribution } : {}),
    });
    return {
      version: 8,
      sources: {
        imagery: {
          type: 'raster', tiles: [`${location.origin}/tiles/imagery/{z}/{x}/{y}`], tileSize: 256, maxzoom: 16,
          attribution: layers.imagery.attribution,
        },
        dem: dem(layers.dem.attribution),
        hillshade: dem(null),
      },
      layers: [
        { id: 'zemin', type: 'background', paint: { 'background-color': COLOR.bg } },
        {
          id: 'uydu', type: 'raster', source: 'imagery',
          paint: { 'raster-saturation': area.map.imagery_saturation, 'raster-fade-duration': 0 },
        },
        {
          id: 'golgelendirme', type: 'hillshade', source: 'hillshade',
          paint: {
            'hillshade-exaggeration': 0.25,
            'hillshade-shadow-color': 'rgba(14, 17, 22, 0.55)',
            'hillshade-highlight-color': 'rgba(230, 233, 238, 0.12)',
            'hillshade-accent-color': 'rgba(14, 17, 22, 0.25)',
          },
        },
      ],
      sky: {
        'sky-color': COLOR.bg, 'horizon-color': COLOR.line, 'fog-color': COLOR.panel,
        'sky-horizon-blend': 0.6, 'horizon-fog-blend': 0.5, 'fog-ground-blend': 0.9, 'atmosphere-blend': 0,
      },
    };
  }

  onLoad() {
    const m = this.map;
    const empty = { type: 'FeatureCollection', features: [] };
    m.addSource('links', { type: 'geojson', data: empty });
    m.addSource('route', { type: 'geojson', data: empty });
    m.addSource('route-done', { type: 'geojson', data: empty });
    m.addLayer({
      id: 'linkler', type: 'line', source: 'links', filter: ['==', ['get', 'off'], false],
      layout: { 'line-cap': 'round' }, paint: { 'line-color': 'rgba(255, 255, 255, 0.35)', 'line-width': 1.5 },
    });
    m.addLayer({
      id: 'linkler-kapali', type: 'line', source: 'links', filter: ['==', ['get', 'off'], true],
      paint: { 'line-color': COLOR.lost, 'line-width': 1.5, 'line-dasharray': [2, 2] },
    });
    m.addLayer({
      id: 'rota', type: 'line', source: 'route', layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': COLOR.route, 'line-width': 4, 'line-opacity': 0.55 },
    });
    m.addLayer({
      id: 'rota-gecilen', type: 'line', source: 'route-done', layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': COLOR.route, 'line-width': 4, 'line-opacity': 1 },
    });
    for (const n of this.cfg.nodes) this.addMarker(n);
    // İşaretler eklendikten sonra: MapLibre işaretleri 'terrain' olayıyla arazi yüksekliğine oturtur.
    m.setTerrain({ source: 'dem', exaggeration: this.cfg.area.map.terrain_exaggeration });
    this.ready = true;
    this.refreshLinks();
    this.setBaseRoute(this.baseRoute);
    this.frame(false);
    // DEM karoları geldikçe işaretleri yeniden konumla; DEM ilk kez tamamlanınca kamerayı yeniden çerçevele
    // (kullanıcı haritayı oynatmadıysa).
    let reframed = false;
    m.on('sourcedata', (e) => {
      if (e.sourceId !== 'dem') return;
      this.refreshMarkerPositions();
      if (!reframed && !this.userMoved && m.isSourceLoaded('dem')) {
        reframed = true;
        this.frame(false);
      }
    });
    for (const ev of ['dragstart', 'zoomstart', 'rotatestart', 'pitchstart']) {
      m.on(ev, (e) => { if (e.originalEvent) this.userMoved = true; });
    }
    if (this.prop) this.startLoop();
  }

  refreshMarkerPositions() {
    if (this.markerRaf) return;
    this.markerRaf = requestAnimationFrame(() => {
      this.markerRaf = null;
      for (const mk of Object.values(this.markers)) mk.marker.setLngLat(mk.marker.getLngLat());
    });
  }

  onError(e) {
    const src = e && e.sourceId;
    if (src === 'imagery' || src === 'dem' || src === 'hillshade' || (e && e.tile)) {
      this.tileErrorUntil = Date.now() + 8000;
      this.hooks.onTileStatus(true);
      clearTimeout(this.tileTimer);
      this.tileTimer = setTimeout(() => this.hooks.onTileStatus(false), 8200);
    }
  }

  // ---------- düğümler ----------
  addMarker(n) {
    const el = document.createElement('div');
    el.className = `mk mk-${n.tur}`;
    el.dataset.status = 'NORMAL';
    el.innerHTML = `<span class="mk-shape">${n.tur === 'physical' ? esc(n.id) : ''}</span><span class="mk-label"></span>`;
    el.setAttribute('role', 'button');
    el.setAttribute('aria-label', n.ad);
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      this.openPopup(n.id);
    });
    // Sırtın arkasında kalan düğüm soluklaşır ama kaybolmaz.
    const marker = new maplibregl.Marker({ element: el, anchor: 'center', subpixelPositioning: true, opacityWhenCovered: '0.6' })
      .setLngLat([n.lon, n.lat])
      .addTo(this.map);
    this.markers[n.id] = { el, marker, label: el.querySelector('.mk-label') };
    this.updateNode(n.id);
  }

  updateNode(id) {
    const mk = this.markers[id];
    const cfg = this.byId[id];
    if (!mk || !cfg) return;
    const st = this.hooks.getNode(id) || { status: cfg.tur === 'physical' ? 'BAGLANTI_YOK' : 'NORMAL' };
    const status = st.status || 'NORMAL';
    const alarm = ALARM_STATUSES.has(status);
    mk.el.dataset.status = status;
    mk.el.classList.toggle('is-alarm', alarm);
    let label;
    if (cfg.tur === 'physical') {
      if (alarm) label = `ALARM · ${st.status_text}`;
      else if (status === 'BAGLANTI_YOK') label = `${id} · BAĞLANTI YOK`;
      else label = cfg.rol === 'sensor' ? 'Saha düğümü' : 'Köprü';
    } else if (cfg.tur === 'virtual') {
      if (status === 'DEVRE_DISI') label = `${id} · DEVRE DIŞI`;
      else if (status === 'YANGIN') label = `${id} · YANGIN (simüle)`;
      else label = id;
    } else {
      label = 'MERKEZ';
    }
    mk.label.textContent = label;
    if (this.popupId === id) this.renderPopup();
  }

  refreshLinks() {
    if (!this.ready) return;
    const off = new Set(this.hooks.getDisabled());
    const features = this.cfg.links.map((l) => {
      const a = this.byId[l.a];
      const b = this.byId[l.b];
      return {
        type: 'Feature',
        properties: { kind: l.kind, off: off.has(l.a) || off.has(l.b) },
        geometry: { type: 'LineString', coordinates: [[a.lon, a.lat], [b.lon, b.lat]] },
      };
    });
    this.map.getSource('links').setData({ type: 'FeatureCollection', features });
  }

  coords(path) {
    return path.map((id) => [this.byId[id].lon, this.byId[id].lat]);
  }

  setLine(source, coords) {
    if (!this.ready) return;
    const data = coords && coords.length > 1
      ? { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: coords } }
      : { type: 'FeatureCollection', features: [] };
    this.map.getSource(source).setData(data);
  }

  setBaseRoute(path) {
    this.baseRoute = path;
    if (this.prop) return;           // paket yoldayken animasyonun rotası görünür
    this.setLine('route', path ? this.coords(path) : null);
    this.setLine('route-done', null);
    this.markRoute(path, -1);
  }

  markRoute(path, reachedIndex) {
    const on = new Set(path || []);
    const reached = new Set((path || []).slice(0, reachedIndex + 1));
    for (const [id, mk] of Object.entries(this.markers)) {
      mk.el.classList.toggle('on-route', on.has(id) && reachedIndex >= 0);
      mk.el.classList.toggle('reached', reached.has(id));
    }
  }

  // ---------- paket animasyonu ----------
  startPropagation(prop) {
    this.prop = { ...prop, coords: this.coords(prop.path) };
    if (!this.packet) {
      const el = document.createElement('div');
      el.className = 'packet';
      el.innerHTML = '<span class="packet-dot"></span><span class="packet-label"></span>';
      this.packet = {
        el,
        label: el.querySelector('.packet-label'),
        marker: new maplibregl.Marker({ element: el, anchor: 'center', opacityWhenCovered: '1' }),
      };
    }
    this.packet.marker.setLngLat(this.prop.coords[0]).addTo(this.map);
    this.setLine('route', this.prop.coords);
    this.startLoop();
  }

  startLoop() {
    if (this.ready && !this.raf) this.raf = requestAnimationFrame(() => this.animate());
  }

  stopPropagation(incidentId) {
    if (!this.prop || (incidentId !== undefined && this.prop.incident !== incidentId)) return;
    this.prop = null;
    if (this.packet) this.packet.marker.remove();
    this.setBaseRoute(this.baseRoute);
  }

  animate() {
    this.raf = null;
    const p = this.prop;
    if (!p) return;
    const hops = p.path.length - 1;
    const t = this.hooks.now() - p.started_at;
    if (t >= hops * p.hop_ms) {
      this.setLine('route-done', p.coords);
      this.markRoute(p.path, hops);
      this.packet.marker.setLngLat(p.coords[hops]);
      this.packet.label.textContent = 'MERKEZ aldı';
      const done = p;
      setTimeout(() => { if (this.prop === done) this.stopPropagation(); }, 1500);
      return;
    }
    const i = clamp(Math.floor(t / p.hop_ms), 0, hops - 1);
    const f = reduceMotion() ? 0 : clamp((t - i * p.hop_ms) / p.hop_ms, 0, 1);
    const a = p.coords[i];
    const b = p.coords[i + 1];
    const pos = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
    this.packet.marker.setLngLat(pos);
    this.packet.label.textContent = p.kinds[i] === 'rf24'
      ? 'gerçek RF · nRF24 2.4 GHz'
      : `simüle LoRa 868 MHz · hop ${i + 1}/${hops}`;
    this.setLine('route-done', [...p.coords.slice(0, i + 1), pos]);
    this.markRoute(p.path, i);
    this.raf = requestAnimationFrame(() => this.animate());
  }

  // ---------- açılır kutu ----------
  openPopup(id) {
    this.closePopup();
    const n = this.byId[id];
    this.popupId = id;
    this.markers[id].el.classList.add('selected');
    this.popup = new maplibregl.Popup({ closeButton: true, closeOnClick: false, offset: 16, maxWidth: '300px' })
      .setLngLat([n.lon, n.lat])
      .setHTML('<div class="pop"></div>')
      .addTo(this.map);
    this.popup.on('close', () => {
      if (this.markers[id]) this.markers[id].el.classList.remove('selected');
      if (this.popupId === id) this.popupId = null;
    });
    this.renderPopup();
  }

  closePopup() {
    if (this.popup) this.popup.remove();
    this.popup = null;
  }

  renderPopup() {
    if (!this.popup || !this.popupId) return;
    const id = this.popupId;
    const n = this.byId[id];
    const st = this.hooks.getNode(id) || {};
    const off = new Set(this.hooks.getDisabled());
    const nb = this.cfg.links
      .filter((l) => l.a === id || l.b === id)
      .map((l) => {
        const other = l.a === id ? l.b : l.a;
        const km = (l.distance_m / 1000).toFixed(1);
        return `${esc(other)} ${km} km${off.has(other) ? ' (kapalı)' : ''}${l.kind === 'rf24' ? ' (nRF24)' : ''}`;
      })
      .join(', ');
    const tur = { physical: 'Fiziksel — gerçek donanım', virtual: 'Sanal — simüle LoRa 868 MHz', center: 'Merkez — simüle' }[n.tur];
    const disabled = st.status === 'DEVRE_DISI';
    const actions = n.tur === 'virtual'
      ? `<div class="pop-actions">
           <button class="btn" data-act="toggle">${disabled ? 'Yeniden etkinleştir' : 'Devre dışı bırak'}</button>
           <button class="btn" data-act="fire"${disabled || st.status === 'YANGIN' ? ' disabled' : ''}>Burada yangın simüle et</button>
         </div>`
      : '';
    const root = this.popup.getElement().querySelector('.pop');
    root.innerHTML = `
      <h3>${esc(n.ad)}</h3>
      <dl>
        <dt>Tür</dt><dd>${esc(tur)}</dd>
        <dt>Durum</dt><dd>${esc(st.status_text || 'NORMAL')}</dd>
        <dt>Koordinat</dt><dd class="num">${n.lat.toFixed(6)}, ${n.lon.toFixed(6)}</dd>
        <dt>Rakım</dt><dd class="num">${n.rakim_m} m · direk ${n.direk_m} m</dd>
        <dt>Donanım</dt><dd>${esc(n.donanim)}</dd>
        <dt>Komşular</dt><dd class="num">${nb || '—'}</dd>
      </dl>${actions}`;
    root.querySelectorAll('[data-act]').forEach((btn) => {
      btn.addEventListener('click', () => this.hooks.onAction(btn.dataset.act, id));
    });
  }

  // ---------- kamera ----------
  // Varsayılan kamera (area.json) F1'den MERKEZ'e tüm ağı çerçeveleyene kadar uzaklaşır.
  frame(animate) {
    const m = this.map;
    const cam = this.cfg.area.camera;
    const before = { center: m.getCenter(), zoom: m.getZoom(), pitch: m.getPitch(), bearing: m.getBearing() };
    const canvas = m.getCanvas();
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    let zoom = cam.zoom;
    m.jumpTo({ center: cam.center, zoom, pitch: cam.pitch, bearing: cam.bearing });
    for (let i = 0; i < 16; i++) {
      const fits = this.cfg.nodes.every((n) => {
        const p = m.project([n.lon, n.lat]);
        return p.x >= 50 && p.x <= w - 110 && p.y >= 70 && p.y <= h - 140;
      });
      if (fits) break;
      zoom -= 0.15;
      m.jumpTo({ zoom });
    }
    if (animate) {
      const target = { center: cam.center, zoom: m.getZoom(), pitch: cam.pitch, bearing: cam.bearing };
      m.jumpTo(before);
      m.easeTo({ ...target, duration: reduceMotion() ? 0 : 900 });
    }
  }

}
