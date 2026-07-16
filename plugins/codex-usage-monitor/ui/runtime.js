(() => {
  "use strict";
  const mount = () => {
  const boot = window.__CODEX_USAGE_BOOT__ || {};
  if (window.__codexUsageRuntime && window.__codexUsageRuntime.destroy) window.__codexUsageRuntime.destroy();

  const state = {
    snapshot: null,
    history: new Map(),
    anchors: new Map(),
    observer: null,
    frame: 0,
    supported: !!boot.supported,
  };
  const host = document.createElement("div");
  host.id = "codex-usage-monitor-runtime";
  host.style.cssText = "position:fixed;inset:0;z-index:2147483000;pointer-events:none;font-family:system-ui,sans-serif";
  document.documentElement.appendChild(host);
  const shadow = host.attachShadow({mode: "closed"});
  shadow.innerHTML = `
    <style>
      :host{all:initial;color-scheme:light dark}
      #dock{position:fixed;pointer-events:auto;box-sizing:border-box;background:color-mix(in srgb,Canvas 94%,transparent);color:CanvasText;
        border:1px solid color-mix(in srgb,CanvasText 18%,transparent);box-shadow:0 12px 36px #0003;backdrop-filter:blur(16px);
        min-width:220px;min-height:120px;max-width:min(80vw,720px);max-height:90vh;overflow:auto;border-radius:12px;padding:10px}
      #dock.right_dock{right:8px;top:54px;bottom:8px;resize:horizontal;direction:rtl} #dock.right_dock>*{direction:ltr}
      #dock.left_dock{left:8px;top:54px;bottom:8px;resize:horizontal}
      #dock.bottom_dock{left:12%;right:12%;bottom:8px;resize:vertical}
      #dock.floating{right:24px;bottom:24px;resize:both;height:260px}
      header{display:flex;align-items:center;gap:8px;position:sticky;top:0;background:Canvas;padding:4px 2px 8px;z-index:2}
      header strong{flex:1;font-size:13px} button{font:inherit;border:0;border-radius:6px;padding:3px 7px;background:color-mix(in srgb,CanvasText 9%,transparent);color:inherit;cursor:pointer}
      #notice{font-size:11px;padding:7px;border-radius:7px;background:#b7791f22;color:#b7791f;margin-bottom:8px}
      .metric{display:grid;grid-template-columns:1fr auto;gap:8px;font-size:12px;padding:4px 2px;border-bottom:1px solid color-mix(in srgb,CanvasText 9%,transparent)}
      .muted{opacity:.62}.bar{height:5px;background:color-mix(in srgb,CanvasText 12%,transparent);border-radius:9px;overflow:hidden;margin:3px 0 7px}.bar i{display:block;height:100%;background:#10a37f}
      .footer{position:fixed;pointer-events:none;box-sizing:border-box;border-radius:7px;padding:4px 8px;font-size:11px;line-height:16px;
        background:color-mix(in srgb,Canvas 92%,transparent);color:color-mix(in srgb,CanvasText 72%,transparent);border:1px solid color-mix(in srgb,CanvasText 10%,transparent);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      iframe{width:100%;border:0;min-height:100px;background:transparent}
      #widgets{display:flex;flex-direction:column;gap:8px}.widget{pointer-events:auto;border:1px solid color-mix(in srgb,CanvasText 10%,transparent);border-radius:8px;padding:7px;font-size:12px;background:Canvas;color:CanvasText}
      .widget>header{position:static;padding:0 0 6px;font-size:11px}.widget>header span{flex:1}.widget.floating{position:fixed;right:24px;bottom:24px;width:320px;max-height:70vh;overflow:auto;box-shadow:0 12px 36px #0004}.widget.modal{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);width:min(560px,80vw);max-height:80vh;overflow:auto;box-shadow:0 18px 60px #0006}
    </style>
    <aside id="dock"><header><strong>Codex Usage</strong><button id="move" title="Move dock">⇆</button><button id="collapse" title="Collapse">−</button></header>
      <div id="notice" hidden></div><div id="metrics"></div><div id="widgets"></div></aside><div id="footers"></div>`;
  const dock = shadow.getElementById("dock");
  const metrics = shadow.getElementById("metrics");
  const notice = shadow.getElementById("notice");
  const footers = shadow.getElementById("footers");
  const widgetsRoot = shadow.getElementById("widgets");
  const positions = ["right_dock", "bottom_dock", "left_dock", "floating"];
  let position = localStorage.getItem("codexUsageDockPosition") || boot.dockPosition || "right_dock";
  if (!positions.includes(position)) position = "right_dock";
  dock.className = position;
  dock.style.width = `${Math.max(220, Number(localStorage.getItem("codexUsageDockSize")) || boot.dockSize || 340)}px`;
  dock.addEventListener("mouseup", () => localStorage.setItem("codexUsageDockSize", String(dock.getBoundingClientRect().width)));
  shadow.getElementById("move").onclick = () => {
    position = positions[(positions.indexOf(position) + 1) % positions.length];
    dock.className = position; localStorage.setItem("codexUsageDockPosition", position);
  };
  shadow.getElementById("collapse").onclick = event => {
    const collapsed = dock.dataset.collapsed === "1";
    dock.dataset.collapsed = collapsed ? "0" : "1";
    metrics.hidden = !collapsed; widgetsRoot.hidden = !collapsed; notice.hidden = collapsed || state.supported;
    event.currentTarget.textContent = collapsed ? "−" : "+";
  };
  if (!state.supported) {
    notice.hidden = false;
    notice.textContent = "Compatibility mode: persistent dock is active; message footers are disabled for this Codex version.";
  }

  function percent(value) { const number = Number(value); return Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : null; }
  function compact(value) { const n=Number(value); if(!Number.isFinite(n)) return "unavailable"; return new Intl.NumberFormat(undefined,{notation:"compact",maximumFractionDigits:1}).format(n); }
  function metric(label, value, progress) {
    const p = percent(progress);
    return `<div class="metric"><span>${label}</span><strong>${value}</strong></div>${p===null?"":`<div class="bar"><i style="width:${p}%"></i></div>`}`;
  }
  function rate(snapshot, kind) {
    const values = Object.values((snapshot && snapshot.rates) || {});
    return values.find(value => value.window_kind === kind) || null;
  }
  function render() {
    const s=state.snapshot||{}, token=s.token||{}, primary=rate(s,"primary"), secondary=rate(s,"secondary");
    metrics.innerHTML = metric("5h used", primary ? `${Number(primary.used_percent).toFixed(1)}%` : "unavailable", primary&&primary.used_percent)
      + metric("Week used", secondary ? `${Number(secondary.used_percent).toFixed(1)}%` : "unavailable", secondary&&secondary.used_percent)
      + metric("Latest model call", compact(token.last_total_tokens))
      + metric("Thread tokens", compact(token.total_tokens))
      + `<div class="metric muted"><span>Updated</span><span>${new Date().toLocaleTimeString()}</span></div>`;
    updateFooters(); publishWidgets();
  }
  function footerText(snapshot) {
    if (!snapshot) return "Codex usage · unavailable";
    const token=snapshot.token||{}, primary=rate(snapshot,"primary"), secondary=rate(snapshot,"secondary");
    return `Codex · call ${compact(token.last_total_tokens)} · thread ${compact(token.total_tokens)} · 5h ${primary?Number(primary.used_percent).toFixed(1)+"%":"—"} · week ${secondary?Number(secondary.used_percent).toFixed(1)+"%":"—"}`;
  }
  function templateValue(snapshot,key){const aliases={"thread.total_tokens":snapshot?.token?.total_tokens,"turn.total_tokens":snapshot?.token?.last_total_tokens,"primary.used_percent":rate(snapshot,"primary")?.used_percent,"secondary.used_percent":rate(snapshot,"secondary")?.used_percent};const value=aliases[key];return value==null?"unavailable":String(value);}
  function footerMarkup(snapshot){const custom=(boot.widgets||[]).filter(w=>w.default_placement==="message_footer"&&w.content_type!=="javascript");if(!custom.length)return escapeHtml(footerText(snapshot));return custom.map(w=>String(w.source).replace(/\{([a-z0-9_.]+)\}/gi,(_,key)=>escapeHtml(templateValue(snapshot,key)))).join("");}
  function escapeHtml(value){const span=document.createElement("span");span.textContent=String(value);return span.innerHTML;}

  function findFiber(element) { const key=Object.keys(element).find(name=>name.startsWith("__reactFiber$")||name.startsWith("__reactInternalInstance$")); return key?element[key]:null; }
  function metadataFromFiber(fiber) {
    let node=fiber, depth=0;
    while(node && depth++<24) {
      const props=node.memoizedProps||node.pendingProps;
      const result=inspectProps(props,0,new Set()); if(result) return result;
      node=node.return;
    }
    return null;
  }
  function inspectProps(value, depth, seen) {
    if(!value||typeof value!=="object"||depth>3||seen.has(value)) return null; seen.add(value);
    const type=value.type||value.item_type||value.itemType;
    const phase=value.phase||value.messagePhase;
    const itemId=value.item_id||value.itemId||value.id;
    const threadId=value.thread_id||value.threadId;
    const turnId=value.turn_id||value.turnId;
    if((type==="assistant-message"||phase==="commentary"||phase==="final_answer") && itemId)
      return {itemId:String(itemId),threadId:String(threadId||location.pathname),turnId:turnId?String(turnId):null,phase:String(phase||"unknown"),completed:!!(value.completed||value.isComplete||value.status==="completed")};
    for(const key of Object.keys(value).slice(0,30)) {
      if(/text|content|prompt|message|output|input/i.test(key)) continue;
      const found=inspectProps(value[key],depth+1,seen); if(found) return found;
    }
    return null;
  }
  function scan() {
    state.frame=0; if(!state.supported) return;
    const next=new Map(), walker=document.createTreeWalker(document.body,NodeFilter.SHOW_ELEMENT); let element, count=0;
    while((element=walker.nextNode()) && count++<12000) {
      const fiber=findFiber(element); if(!fiber) continue;
      const meta=metadataFromFiber(fiber); if(!meta || !(boot.footerPhases||[]).includes(meta.phase) || next.has(meta.itemId)) continue;
      const container=containerFor(element); let slot=container.querySelector(`:scope > [data-codex-usage-slot="${CSS.escape(meta.itemId)}"]`);
      if(!slot){slot=document.createElement("div");slot.dataset.codexUsageSlot=meta.itemId;slot.setAttribute("aria-hidden","true");slot.style.cssText="height:25px;min-height:25px;pointer-events:none";container.appendChild(slot);}
      next.set(meta.itemId,{element:container,slot,meta});
      if(!state.anchors.has(meta.itemId) || meta.completed) send({type:"item",...meta});
    }
    state.anchors=next; updateFooters();
  }
  function containerFor(element){let current=element;for(let i=0;i<7&&current.parentElement;i++){const style=getComputedStyle(current);const rect=current.getBoundingClientRect();if(style.display!=="inline"&&rect.width>180)return current;current=current.parentElement;}return element;}
  function scheduleScan(){ if(!state.frame) state.frame=requestAnimationFrame(scan); }
  function updateFooters() {
    if(!state.supported) return;
    const existing=new Map(Array.from(footers.children).map(node=>[node.dataset.itemId,node]));
    for(const [itemId,anchor] of state.anchors) {
      let footer=existing.get(itemId); if(!footer){footer=document.createElement("div");footer.className="footer";footer.dataset.itemId=itemId;footers.appendChild(footer);}
      existing.delete(itemId); const rect=anchor.element.getBoundingClientRect(), slotRect=anchor.slot.getBoundingClientRect();
      footer.style.left=`${Math.max(4,rect.left)}px`; footer.style.top=`${Math.max(4,slotRect.top+2)}px`; footer.style.width=`${Math.max(180,rect.width)}px`;
      const historic=state.history.get(`${anchor.meta.threadId}:${itemId}`); footer.innerHTML=footerMarkup(historic?historic.snapshot:state.snapshot);
      if(rect.bottom<0||rect.top>innerHeight) footer.hidden=true; else footer.hidden=false;
    }
    for(const footer of existing.values()) footer.remove();
  }
  function send(value){ try{ if(typeof window.__codexUsageHost==="function") window.__codexUsageHost(JSON.stringify(value)); }catch(_){} }
  function renderWidgets(){
    widgetsRoot.replaceChildren();shadow.querySelectorAll(".widget.floating,.widget.modal").forEach(node=>node.remove());
    const storedOrder=JSON.parse(localStorage.getItem("codexUsageWidgetOrder")||"[]");const order=new Map(storedOrder.map((id,index)=>[id,index]));
    const widgets=[...(boot.widgets||[])].sort((a,b)=>(order.get(a.id)??a.order)-(order.get(b.id)??b.order));
    for(const widget of widgets) {
      if(widget.default_placement==="message_footer") continue;
      const wrap=document.createElement("section");let placement=localStorage.getItem(`codexUsageWidgetPlacement:${widget.id}`)||widget.default_placement;wrap.className=`widget ${placement}`;wrap.dataset.widgetId=widget.id;wrap.draggable=true;
      const cardHeader=document.createElement("header"),label=document.createElement("span"),move=document.createElement("button");label.textContent=widget.name;move.textContent="↗";move.title="Move widget";cardHeader.append(label,move);wrap.appendChild(cardHeader);
      move.onclick=()=>{const allowed=widget.placements.filter(p=>p!=="message_footer");placement=allowed[(allowed.indexOf(placement)+1)%allowed.length];localStorage.setItem(`codexUsageWidgetPlacement:${widget.id}`,placement);renderWidgets();};
      wrap.ondragstart=event=>event.dataTransfer.setData("text/plain",widget.id);wrap.ondragover=event=>event.preventDefault();wrap.ondrop=event=>{event.preventDefault();const source=event.dataTransfer.getData("text/plain"),ids=widgets.map(w=>w.id),from=ids.indexOf(source),to=ids.indexOf(widget.id);if(from>=0&&to>=0){ids.splice(to,0,ids.splice(from,1)[0]);localStorage.setItem("codexUsageWidgetOrder",JSON.stringify(ids));renderWidgets();}};
      if(widget.content_type==="javascript") {
        const iframe=document.createElement("iframe");iframe.setAttribute("sandbox","allow-scripts");
        const source=String(widget.source).replace(/<\/script/gi,"<\\/script");
        iframe.srcdoc=`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:"><style>body{font:12px system-ui;color:CanvasText;background:transparent;margin:0}</style><script>const api={getSnapshot:()=>new Promise(r=>{window.__resolve=r;parent.postMessage({source:'codex-usage-widget',type:'snapshot',id:${JSON.stringify(widget.id)}},'*')}),subscribeTelemetry:f=>addEventListener('message',e=>{if(e.data&&e.data.type==='telemetry')f(e.data.snapshot)}),getTheme:()=>matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light',requestResize:size=>parent.postMessage({source:'codex-usage-widget',type:'resize',id:${JSON.stringify(widget.id)},size},'*'),openSettings:()=>parent.postMessage({source:'codex-usage-widget',type:'settings',id:${JSON.stringify(widget.id)}},'*')};<\/script><script>${source}<\/script>`;
        wrap.appendChild(iframe);
      } else { wrap.innerHTML=widget.source; }
      if(placement==="floating"||placement==="modal")shadow.appendChild(wrap);else{widgetsRoot.appendChild(wrap);if(positions.includes(placement)&&dock.className!==placement){dock.className=placement;position=placement;}}
    }
  }
  function publishWidgets(){ for(const frame of shadow.querySelectorAll(".widget iframe")) frame.contentWindow.postMessage({type:"telemetry",snapshot:state.snapshot},"*"); }
  addEventListener("message",event=>{const data=event.data;if(!data||data.source!=="codex-usage-widget")return;const widget=(boot.widgets||[]).find(w=>w.id===data.id);const frame=widget&&shadow.querySelector(`[data-widget-id="${CSS.escape(data.id)}"] iframe`);if(!widget||event.source!==frame?.contentWindow)return;if(data.type==="resize")frame.style.height=`${Math.max(60,Math.min(600,Number(data.size&&data.size.height)||120))}px`;if(data.type==="snapshot")event.source.postMessage({type:"telemetry",snapshot:state.snapshot},"*");},false);
  window.__codexUsageUpdate = payload => { state.snapshot=payload&&payload.snapshot; state.history=new Map((payload&&payload.history||[]).map(row=>[`${row.thread_id}:${row.item_id}`,row])); render(); };
  state.observer=new MutationObserver(scheduleScan); state.observer.observe(document.documentElement,{childList:true,subtree:true});
  addEventListener("scroll",updateFooters,true); addEventListener("resize",updateFooters); renderWidgets(); render(); scheduleScan(); send({type:"ready"});
  window.__codexUsageRuntime={destroy(){state.observer&&state.observer.disconnect();cancelAnimationFrame(state.frame);document.querySelectorAll("[data-codex-usage-slot]").forEach(node=>node.remove());host.remove();delete window.__codexUsageUpdate;}};
  };
  if (document.documentElement) mount();
  else addEventListener("DOMContentLoaded", mount, {once:true});
})();
