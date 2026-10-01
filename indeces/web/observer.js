/* Read-only local observer. All source, mark, query and record text is untrusted. */
"use strict";
(() => {
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const list = value => Array.isArray(value) ? value : [];
  const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const text = value => value == null ? "—" : String(value);
  const numberText = value => finite(value) ? value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "") : "—";
  const edgeId = edge => JSON.stringify([edge.a, edge.b]);
  const edgeWeight = (edge, layer) => ({static:edge.static_weight, dynamic:edge.dynamic_weight, effective:edge.effective_weight})[layer];
  const displayWeight = (edge, layer) => layer==="effective"&&!edge.active_source_support ?
    (finite(edge.dynamic_weight)?edge.dynamic_weight:edge.static_weight) : edgeWeight(edge,layer);
  const timeMillis = value => typeof value === "number" ? value * 1000 : Date.parse(value);
  const retainedAt = (value, now = Date.now()) => { const timestamp=timeMillis(value);return finite(timestamp)&&timestamp>now-86400000&&timestamp<=now; };
  const retainedRecords = (records, now = Date.now()) => list(records).filter(record=>retainedAt(record.timestamp,now));
  function connectionPresentation({paused,readFailed,snapshot}) {
    if(readFailed)return {className:"status-chip error",label:"快照未更新"+(paused?" · 已暂停":"")};
    if(!snapshot)return {className:"status-chip quiet",label:paused?"尚未读取 · 已暂停":"正在读取"};
    return {className:"status-chip"+(paused?" quiet":""),label:paused?"快照已暂停":"快照可读取 · 只读"};
  }
  function graphSelection(graph, options) {
    const edges = list(graph.edges).filter(edge => (options.archived || edge.active_source_support === true) &&
      finite(displayWeight(edge, options.layer)) && (options.layer!=="static" || displayWeight(edge, options.layer)>0) && displayWeight(edge, options.layer) >= options.minimum);
    const query = options.query.trim().toLocaleLowerCase();
    const allNodes = list(graph.nodes), available = new Set(allNodes.map(node => String(node.id)));
    let names;
    if (query) {
      const matches = new Set(allNodes.filter(node => (options.archived || node.frequency > 0) && String(node.id).toLocaleLowerCase().includes(query)).map(node => String(node.id)));
      names = new Set(matches);
      edges.forEach(edge => { if (matches.has(edge.a) || matches.has(edge.b)) { names.add(edge.a); names.add(edge.b); } });
    } else {
      names = new Set();
      edges.forEach(edge => { names.add(edge.a); names.add(edge.b); });
      allNodes.forEach(node => { if (options.archived || node.frequency > 0) names.add(String(node.id)); });
    }
    return {nodes:allNodes.filter(node => names.has(String(node.id))),
      edges:edges.filter(edge => available.has(edge.a) && available.has(edge.b) && names.has(edge.a) && names.has(edge.b))};
  }
  function historyPoints(history) {
    return list(history).filter(item => finite(object(item.change).after_weight)).map(item => ({
      item, weight:item.change.after_weight, time:typeof item.timestamp === "number" ? item.timestamp * 1000 : Date.parse(item.timestamp)
    })).sort((a,b) => (finite(a.time) ? a.time : 0) - (finite(b.time) ? b.time : 0));
  }
  // Pure helpers are exposed only to offline Node tests; no browser global data API.
  if (typeof document === "undefined") {
    if (typeof module !== "undefined") module.exports = {graphSelection, historyPoints, edgeWeight, displayWeight, numberText, edgeId, retainedAt, retainedRecords, connectionPresentation};
    return;
  }
  const $ = id => document.getElementById(id), NS = "http://www.w3.org/2000/svg";
  const state = {snapshot:null,view:"graph",paused:false,refreshing:false,readFailed:false,selected:null,detailRequest:0,detailWarning:null,
    positions:new Map(),nodeElements:new Map(),edgeElements:[],transform:{x:0,y:0,k:1},drag:null,requestControllers:new Set()};
  function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.textContent = text(content);
    return node;
  }
  function svgEl(tag, attributes = {}) {
    const node = document.createElementNS(NS, tag);
    Object.entries(attributes).forEach(([name,value]) => node.setAttribute(name, String(value)));
    return node;
  }
  function append(parent, ...children) { children.filter(Boolean).forEach(child => parent.appendChild(child)); return parent; }
  function clear(node) { node.replaceChildren(); }
  function localTime(value, seconds = true) {
    const date = new Date(typeof value === "number" ? value*1000 : value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    return date.toLocaleString("zh-CN", {year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",...(seconds?{second:"2-digit"}:{})});
  }
  function timeNode(value) {
    const node = el("time", "", localTime(value));
    const date = new Date(typeof value === "number" ? value*1000 : value);
    if (!Number.isNaN(date.getTime())) { node.dateTime = date.toISOString(); node.title = date.toISOString(); }
    return node;
  }
  function facts(entries) {
    const dl = el("dl","facts");
    entries.forEach(([label,value]) => append(dl, append(el("div","fact-row"),el("dt","",label),el("dd","",value))));
    return dl;
  }
  function tags(values) {
    const container = el("div","tag-list");
    list(values).forEach(value => append(container,el("span","tag",value)));
    return container;
  }
  function jsonDetails(value, label = "查看原始 JSON") {
    const container = el("details"); append(container,el("summary","",label),el("pre","",JSON.stringify(value,null,2))); return container;
  }
  function warning(message, invalid = false) { return el("div", "inline-warning"+(invalid?" inline-error":""), message); }
  function section(title) { return append(el("section","detail-section"),el("h3","",title)); }
  function sourceButton(id, label) {
    const button = el("button","source-link",label || id); button.type="button";
    button.addEventListener("click",() => selectDetail("source",id)); return button;
  }
  const statusLabels = {ready:"标词完成",pending:"待标词",labelling:"标词中",labeling:"标词中",failed:"失败",delivered:"已送达",
    incomplete:"未完成",passive:"后台任务",retention_partial:"跨保留窗口",archived:"已归档",cancelled:"已取消",
    retained_checks_passed:"保留部分检查通过",invalid:"核验异常",no_retained_records:"没有保留记录"};
  function statusBadge(status) {
    const badge = el("span","status-chip quiet",statusLabels[status] || status || "未知");
    if (["failed","invalid"].includes(status)) badge.className="status-chip error";
    if (["ready","delivered","retained_checks_passed"].includes(status)) badge.className="status-chip";
    return badge;
  }
  async function api(path, parameters = {}) {
    const controller = new AbortController(); state.requestControllers.add(controller);
    const url = new URL("api/"+path, location.href);
    Object.entries(parameters).forEach(([key,value]) => url.searchParams.set(key,String(value)));
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, {method:"GET",cache:"no-store",credentials:"omit",signal:controller.signal,redirect:"error"});
      if (!response.ok) throw new Error("HTTP "+response.status);
      return await response.json();
    } finally { clearTimeout(timeout); state.requestControllers.delete(controller); }
  }
  function showError(message) { $("error-banner").hidden=false; $("error-banner").textContent=message; }
  function renderConnection() { const status=connectionPresentation(state);$("connection-status").className=status.className;$("connection-status").textContent=status.label; }
  function errorText(error) { return error.name === "AbortError" ? "读取超时" : error.message || "读取失败"; }
  async function refresh() {
    if (state.refreshing) return;
    state.refreshing=true; $("refresh").disabled=true;
    try {
      const snapshot = await api("snapshot"); state.snapshot=snapshot;state.readFailed=false;
      $("error-banner").hidden=true;
      renderConnection();
      const timestamp=snapshot.generated_at || snapshot.timestamp;
      $("freshness").textContent="快照 "+localTime(timestamp)+" · "+(state.paused?"手动刷新":"每 5 秒刷新");
      $("freshness").title=timestamp?text(timestamp):"";
      $("scope").textContent="本机 · "+(snapshot.scope || snapshot.knowledge_scope || "未建立知识 scope");
      const graph=object(snapshot.graph), knowledge=object(snapshot.knowledge), scratch=object(snapshot.scratch);
      $("graph-count").textContent=text(graph.total_nodes ?? list(graph.nodes).length);
      $("knowledge-count").textContent=text(object(knowledge.counts).published ?? list(knowledge.published).length);
      $("scratch-count").textContent=text(list(scratch.traces).length);
      const limits=[];
      if(graph.truncated) limits.push("图谱只显示受限快照");
      if(knowledge.truncated) limits.push("知识目录已截断");
      if(scratch.truncated) limits.push("运行目录已截断");
      list(snapshot.warnings).forEach(value=>limits.push(value==="database_snapshot_unavailable"?"数据库快照暂时不可读取，当前空图不能证明数据库为空":value==="database_snapshot_timeout"?"数据库读取达到协作时间上限，当前统计与图不可用":"读取警告："+text(value)));
      $("limit-banner").hidden=limits.length===0;
      $("limit-banner").textContent=limits.join("；")+"。受限快照的总数与展示数可能不同。";
      renderDiagnostics();renderGraph(); renderKnowledge(); renderScratch();
      if(state.selected) await loadDetail(state.selected, true);
    } catch(error) {
      showError("观察服务读取失败（"+errorText(error)+"）。页面保留上次快照，检查 Console 中的观察服务地址与状态。");
      state.readFailed=true;renderConnection();
    } finally { state.refreshing=false; $("refresh").disabled=false; }
  }
  function options() { return {layer:$("weight-layer").value,archived:$("show-archived").checked,
    minimum:Math.max(0,parseFloat($("min-weight").value)||0),query:$("mark-search").value}; }
  function renderDiagnostics() {
    const graph=object(object(state.snapshot).graph),stats=graph.diagnostics,metrics=$("npmi-metrics");clear(metrics);
    if(!stats){$("npmi-summary").textContent=graph.diagnostics_status==="unavailable"||list(object(state.snapshot).warnings).some(value=>value.startsWith("database_snapshot_"))?"统计暂不可用；读取失败不能证明知识库为空。":"尚未建立图统计。添加材料并显式 start，完成后台标词发布后可查阅。";return;}
    [["有效材料块",stats.active_records],["来源版本",stats.active_source_versions],["标注词",stats.active_marks],["正 NPMI 边",stats.positive_edges]].forEach(([label,value])=>append(metrics,append(el("div","overview-metric"),el("span","",label),el("strong","",value))));
    const parts=["共现支持 "+text(stats.supported_pairs)+" 对；其中 "+text(stats.supported_without_positive)+" 对未保存正边；孤立词 "+text(stats.isolated_marks)+" 个。"];
    if(stats.positive_edges>0){
      parts.push("正边中位数 "+numberText(stats.median_positive_weight)+"；单块支持 "+text(stats.single_support_positive_edges)+" 条、单一来源支持 "+text(stats.single_source_positive_edges)+" 条、保存值为 1 的边 "+text(stats.unit_weight_edges)+" 条。");
      if(stats.single_support_positive_edges||stats.single_source_positive_edges||stats.unit_weight_edges)parts.push("高分也可能来自稀少或同一来源的样本，不能据此证明稳健关联。");
    } else if(stats.active_records>0)parts.push("尚无正关联边；已发布材料仍可通过标注词的直接字面命中召回。");
    parts.push(graph.truncated?"以上为整个 scope 的聚合统计；下方图谱只展示受限子集。":"以上为整个 scope 的聚合统计，不随下方筛选条件改变。");
    $("npmi-summary").textContent=parts.join(" ");
  }
  function seedPositions(nodes) {
    const count=nodes.length;let inserted=false;
    nodes.forEach((node,index) => {
      const name=String(node.id);
      if(!state.positions.has(name)) {
        inserted=true;
        const angle=index*2.3999632297, radius=Math.sqrt((index+.5)/Math.max(1,count))*210;
        state.positions.set(name,{x:450+Math.cos(angle)*radius,y:275+Math.sin(angle)*radius});
      }
    });
    // Bounded deterministic relaxation, without altering or inventing graph weights.
    if(count>140 || !inserted) return;
    for(let step=0;step<32;step++) {
      const motion=new Map(nodes.map(node=>[String(node.id),{x:0,y:0}]));
      for(let i=0;i<count;i++) for(let j=i+1;j<count;j++) {
        const a=state.positions.get(String(nodes[i].id)), b=state.positions.get(String(nodes[j].id));
        let dx=a.x-b.x,dy=a.y-b.y, dist=Math.max(10,Math.hypot(dx,dy));
        if(dx===0 && dy===0){dx=1;dy=.5;dist=1;}
        const force=Math.min(8,340/dist**2),ma=motion.get(String(nodes[i].id)),mb=motion.get(String(nodes[j].id));
        ma.x+=dx/dist*force;ma.y+=dy/dist*force;mb.x-=dx/dist*force;mb.y-=dy/dist*force;
      }
      nodes.forEach(node=>{const name=String(node.id),p=state.positions.get(name),m=motion.get(name);
        p.x=Math.max(60,Math.min(840,p.x+m.x+(450-p.x)*.003));p.y=Math.max(55,Math.min(495,p.y+m.y+(275-p.y)*.003));});
    }
  }
  function renderGraph() {
    const graph=object(object(state.snapshot).graph), current=options(), selected=graphSelection(graph,current);
    const known=new Set(list(graph.nodes).map(node=>String(node.id)));
    state.positions.forEach((_,name)=>{if(!known.has(name))state.positions.delete(name);});
    seedPositions(selected.nodes); clear($("network-edges"));clear($("network-nodes"));state.nodeElements.clear();state.edgeElements=[];
    const maxWeight=Math.max(1,...selected.edges.map(edge=>displayWeight(edge,current.layer)));
    const chosen=state.selected&&state.selected.kind==="edge"?edgeId(state.selected.id):null;
    selected.edges.forEach(edge => {
      const weight=displayWeight(edge,current.layer), live=edge.active_source_support===true;
      const line=svgEl("line",{class:"network-edge"+(!live?" archived":"")+(current.layer==="static"&&live?" static-layer":"")+(chosen===edgeId(edge)?" selected":""),
        "stroke-width":1+Math.sqrt(Math.max(0,weight)/maxWeight)*4.5,opacity:live?.35+Math.sqrt(Math.max(0,weight)/maxWeight)*.45:.3});
      const hit=svgEl("line",{class:"network-edge-hit",tabindex:0,role:"button","aria-label":edge.a+" 与 "+edge.b+"，"+(live?"权重 ":"历史保留值，不能用于检索 ")+numberText(weight)});
      append(hit,append(svgEl("title"),document.createTextNode(edge.a+" ↔ "+edge.b+" · "+numberText(weight)+(live?" · 有效来源":" · 历史边，无有效来源"))));
      const choose=()=>selectDetail("edge",{a:edge.a,b:edge.b});
      hit.addEventListener("click",event=>{event.stopPropagation();choose();});
      hit.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();choose();}});
      append($("network-edges"),line,hit);state.edgeElements.push({edge,line,hit});
    });
    selected.nodes.forEach(node => {
      const name=String(node.id), radius=Math.min(21,10+Math.sqrt(Math.max(0,node.frequency||0))*2.1),
        isSelected=state.selected&&(state.selected.kind==="node"&&state.selected.id===name || state.selected.kind==="edge"&&(state.selected.id.a===name||state.selected.id.b===name));
      const group=svgEl("g",{class:"network-node"+(node.frequency>0?"":" archived")+(isSelected?" selected":""),tabindex:0,role:"button","aria-label":name+"，有效材料频次 "+text(node.frequency)});
      const label=svgEl("text",{class:"node-label",y:radius+20});label.textContent=name.length>24?name.slice(0,23)+"…":name;
      const frequency=svgEl("text",{class:"node-frequency",y:3});frequency.textContent=text(node.frequency??0);
      append(group,svgEl("circle",{class:"node-halo",r:radius+8}),svgEl("circle",{class:"node-circle",r:radius}),frequency,label);
      append(group,append(svgEl("title"),document.createTextNode(name)));
      group.addEventListener("pointerdown",event=>startNodeDrag(event,name));
      group.addEventListener("click",event=>{event.stopPropagation();if(!state.suppressClick)selectDetail("node",name);});
      group.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();selectDetail("node",name);}});
      append($("network-nodes"),group);state.nodeElements.set(name,group);
    });
    updateGraphPositions();
    $("graph-empty").hidden=selected.nodes.length>0;
    const unavailable=list(object(state.snapshot).warnings).some(value=>value.startsWith("database_snapshot_"));
    $("graph-empty-title").textContent=unavailable?"图谱暂不可读取":list(graph.nodes).length?"没有符合条件的节点":"还没有关联图谱";
    $("graph-empty-text").textContent=unavailable?"当前读取未完成，空图不能证明知识库为空。请稍后刷新或查看 Console 状态。":list(graph.nodes).length?"试试调整查找词或显示历史节点与边。":"在 knowledge 目录添加 PDF、Markdown 或文本，显式 start 后服务才会转换和标词。这里展示完成发布的版本。";
    $("visible-count").textContent=selected.nodes.length+" 词 · "+selected.edges.length+" 边";
    const descriptions={effective:"当前检索使用有有效来源支持的静态正 NPMI。历史值只供观察，不进入当前检索。",
      static:"NPMI 衡量超出词频基线的共现关联，范围 −1 至 1；当前仅保存正边。— 表示未保存正边，不是测得为零。",
      dynamic:"动态值仅作字面命中的观察与历史记录，当前不用于回复排序。可大于 1，不是概率。"};
    $("layer-description").textContent=descriptions[current.layer];
    if(selected.nodes.length>0&&!selected.edges.length)$("layer-description").textContent+=" 当前筛选无可展示的边，保留有效词节点供查阅；直接字面命中仍可召回材料。";
  }
  function updateGraphPositions() {
    state.nodeElements.forEach((group,name)=>{const position=state.positions.get(name);group.setAttribute("transform","translate("+position.x+" "+position.y+")");});
    state.edgeElements.forEach(({edge,line,hit})=>{const a=state.positions.get(edge.a),b=state.positions.get(edge.b);
      [line,hit].forEach(node=>{node.setAttribute("x1",a.x);node.setAttribute("y1",a.y);node.setAttribute("x2",b.x);node.setAttribute("y2",b.y);});});
    applyTransform();
  }
  function applyTransform() { const t=state.transform;$("network-transform").setAttribute("transform","translate("+t.x+" "+t.y+") scale("+t.k+")");$("zoom-value").textContent=Math.round(t.k*100)+"%"; }
  function svgPoint(event) { const point=$("network").createSVGPoint();point.x=event.clientX;point.y=event.clientY;return point.matrixTransform($("network").getScreenCTM().inverse()); }
  function startNodeDrag(event,name) {
    if(event.button!==0)return;event.stopPropagation();const point=svgPoint(event),position=state.positions.get(name),t=state.transform;
    state.drag={kind:"node",name,offsetX:(point.x-t.x)/t.k-position.x,offsetY:(point.y-t.y)/t.k-position.y,startX:event.clientX,startY:event.clientY};
    $("network").setPointerCapture(event.pointerId);state.suppressClick=false;
  }
  function zoom(factor,point={x:450,y:275}) {
    const t=state.transform,newScale=Math.max(.35,Math.min(4,t.k*factor));
    t.x=point.x-(point.x-t.x)*newScale/t.k;t.y=point.y-(point.y-t.y)*newScale/t.k;t.k=newScale;applyTransform();
  }
  function renderKnowledge() {
    const knowledge=object(object(state.snapshot).knowledge),published=list(knowledge.published),pending=list(knowledge.pending),container=$("knowledge-list");clear(container);
    const catalogue=new Map();
    published.forEach(version=>{const path=version.path || version.source_id;catalogue.set(path,{path,published:version});});
    pending.forEach(version=>{const path=version.path||version.source_id,entry=catalogue.get(path)||{path};entry.pending=version;catalogue.set(path,entry);});
    $("knowledge-summary").textContent=text(object(knowledge.counts).published??published.length)+" 已发布 · "+text(object(knowledge.counts).pending??pending.length)+" 待更新";
    if(!catalogue.size)append(container,el("p","plain-empty","还没有可展示的知识版本。显式 start 后，服务运行期间会持续监听 knowledge 目录。"));
    catalogue.forEach(entry=>{
      const card=append(el("article","catalogue-card"),el("h3","",entry.path));
      [["已发布",entry.published],["待更新",entry.pending]].forEach(([label,version])=>{
        if(!version)return;const button=el("button","version-button",version.source_id);button.type="button";
        button.addEventListener("click",()=>selectDetail("source",version.source_id));
        append(card,append(el("div","version-row"),el("span","version-label",label),button,statusBadge(version.status)));
      });
      if(entry.pending)append(card,el("p","muted",entry.published?"新版本完成前继续检索已发布原文与标词。":"尚无完整发布版本可供检索。"));
      append(container,card);
    });
  }
  function renderScratch() {
    const scratch=object(object(state.snapshot).scratch);$("scratch-directory").textContent=text(scratch.directory);
    const files=list(scratch.files);$("scratch-files").textContent="当前文件 "+files.length+" 个 · 窗口起点 "+localTime(scratch.cutoff)+(files.length?" · "+files.map(file=>typeof file==="string"?file:file.name||file.file||"日期日志").join("、"):"");
    const container=$("trace-list");clear(container);
    const traces=list(scratch.traces).filter(trace=>retainedAt(trace.timestamp));
    $("scratch-count").textContent=traces.length;
    if(!traces.length)append(container,el("p","plain-empty","过去 24 小时没有可展示的 trace。启动服务后，回复与后台任务记录会出现在这里。"));
    traces.forEach(trace=>{
      const button=el("button","trace-button"+(state.selected&&state.selected.kind==="trace"&&state.selected.id===trace.trace_id?" selected":""));button.type="button";
      append(button,append(el("span","trace-topline"),timeNode(trace.timestamp),statusBadge(trace.status)),
        el("span","trace-preview",list(trace.events).map(event=>eventLabels[event]||event).join(" · ")),
        el("span","trace-id",trace.trace_id+" · "+trace.event_count+" 条事件"));
      button.addEventListener("click",()=>selectDetail("trace",trace.trace_id));append(container,button);
    });
    if(scratch.errors&&list(scratch.errors).length)append(container,warning("部分日志无法读取或校验："+list(scratch.errors).map(error=>typeof error==="string"?error:error.reason||error.error||"未知错误").join("；"),true));
  }
  async function selectDetail(kind,id) { state.selected={kind,id};renderGraph();renderScratch();await loadDetail(state.selected); }
  async function loadDetail(selection,quiet = false) {
    const request=++state.detailRequest, panel=$("detail-panel");
    if(!quiet) {clear(panel);append(panel,el("div","loading","读取证据"));}
    try {
      let data;
      if(selection.kind==="node") {if(request===state.detailRequest)renderNode(selection.id);return;}
      if(selection.kind==="edge") data=await api("edge",selection.id);
      if(selection.kind==="source") data=await api("source",{id:selection.id});
      if(selection.kind==="trace") data=await api("trace",{id:selection.id});
      if(request!==state.detailRequest)return;
      const scroll=quiet?panel.scrollTop:0;clear(panel);
      if(selection.kind==="edge")renderEdge(data);
      if(selection.kind==="source")renderSource(data);
      if(selection.kind==="trace")renderTrace(data);
      panel.scrollTop=scroll;
    }catch(error){if(request!==state.detailRequest)return;
      if(!quiet)clear(panel);if(state.detailWarning)state.detailWarning.remove();
      state.detailWarning=warning("详情读取失败（"+errorText(error)+"）。"+(quiet?"上次详情保留，尚未更新。":"请刷新后重试。"),true);append(panel,state.detailWarning);}
  }
  function detailHeading(eyebrow,title,subtitle) {
    const node=el("div","detail-heading"),body=el("div");append(body,el("span","eyebrow",eyebrow),el("h2","",title),subtitle?el("p","",subtitle):null);append(node,body);return node;
  }
  function renderNode(name) {
    const panel=$("detail-panel");clear(panel);const graph=object(object(state.snapshot).graph),node=list(graph.nodes).find(item=>String(item.id)===name),related=list(graph.edges).filter(edge=>edge.a===name||edge.b===name);
    append(panel,detailHeading("标注词",name,"频次是有效材料中的出现次数；布局位置不代表语义距离。"),facts([["有效材料频次",node?node.frequency:0],["关联边",related.length]]));
    const relations=section("关联关系");
    related.forEach(edge=>{const button=el("button","source-link",(edge.a===name?edge.b:edge.a)+" · 检索权重 "+numberText(edge.effective_weight)+(edge.active_source_support?"":" · 历史"));button.type="button";button.addEventListener("click",()=>selectDetail("edge",{a:edge.a,b:edge.b}));append(relations,button);});
    if(!related.length)append(relations,el("p","","当前没有保存的关联边。"));append(panel,relations);
  }
  function renderEdge(data) {
    const panel=$("detail-panel"),edge=object(data.edge);
    append(panel,detailHeading("关联证据",text(data.a||edge.a)+" ↔ "+text(data.b||edge.b),"当前检索使用静态正 NPMI；动态值单独保留为观察历史。"));
    list(data.warnings).forEach(value=>append(panel,warning("读取警告："+text(value),true)));
    if(!data.edge){append(panel,warning("当前快照中未找到这条关联。可能已更新或不属于此 scope。"));return;}
    const metrics=el("div","metric-grid");
    [["静态 NPMI",edge.static_weight],["动态观察",edge.dynamic_weight],["检索权重",edge.effective_weight]].forEach(([label,value],index)=>append(metrics,append(el("div","metric"+(index===2?" emphasis":"")),el("span","",label),el("strong","",numberText(value)))));append(panel,metrics);
    append(panel,facts([["来源状态",edge.active_source_support?"有效来源支持":"无有效来源 · 历史保留"],["共同出现材料",edge.co_count??0],["最近更新事件",edge.last_event_id??"未经过动态更新"]]));
    if(!edge.active_source_support)append(panel,warning("这条历史边不能参与当前在线检索。动态权重保留不等于来源仍然有效。"));
    else if(!finite(edge.effective_weight))append(panel,warning("有共现来源，但没有保存的正 NPMI 边；此关联不能用于当前静态扩展。材料仍可被直接字面命中。"));
    const statistics=object(edge.npmi_statistics);
    if(finite(statistics.active_records)){
      const basis=section("NPMI 的样本依据");
      append(basis,facts([["有效材料块 N",statistics.active_records],[text(edge.a)+" 出现块数",statistics.frequency_a],[text(edge.b)+" 出现块数",statistics.frequency_b],["共同出现块数",statistics.co_count],["有效来源版本数",edge.source_count],["当前重算 NPMI",numberText(statistics.recomputed_npmi)]]),el("p","","NPMI = log(p(a,b) / (p(a)p(b))) / −log(p(a,b))；p(a,b)=1 时沿用基线规则取 1。保存的正边按原策略四舍五入到 4 位。"));
      if(statistics.co_count===1||edge.source_count===1||edge.static_weight===1)append(basis,warning("单块或单一来源支持，或保存值为 1，都不能单独证明关联稳健；块数不等于独立证据数量。"));
      append(panel,basis);
    }
    const sourceSection=section("来源版本");
    list(edge.source_ids).forEach(id=>append(sourceSection,sourceButton(id)));
    if(!list(edge.source_ids).length)append(sourceSection,el("p","","当前没有有效版本支持。"));
    append(sourceSection,el("p","","共现上下文"),tags(edge.context));append(panel,sourceSection);
    if(edge.truncated)append(sourceSection,warning("此边的来源或上下文列表已截断。来源总数 "+text(edge.source_count)+"，上下文词总数 "+text(edge.context_count)+"。"));
    const historySection=section("权重的变化");
    append(historySection,el("p","","图中每点为事件完成后的动态权重；下方分解种子、衰减与强化。"));
    const history=list(data.history),points=historyPoints(history);
    if(points.length)append(historySection,drawHistory(points));else append(historySection,el("p","","尚无可展示的动态变更事件。后台标词本身不会强化权重。"));
    if(data.truncated)append(historySection,warning("历史结果已截断；仅显示最近的受限事件，曲线不代表完整历史。"));
    const events=el("div","history-list");
    points.slice().reverse().forEach(point=>append(events,historyItem(point.item)));
    append(historySection,events);append(panel,historySection,jsonDetails(data));
  }
  function drawHistory(points) {
    const width=300,height=155,padding={left:37,right:13,top:17,bottom:26};
    const chart=svgEl("svg",{viewBox:"0 0 "+width+" "+height,class:"history-chart",role:"img","aria-label":"动态权重随事件的变化"});
    const weights=points.map(point=>point.weight),min=Math.min(...weights),max=Math.max(...weights),range=Math.max(.01,max-min),lo=min-range*.12,hi=max+range*.12;
    const x=index=>padding.left+(width-padding.left-padding.right)*(points.length===1?.5:index/(points.length-1)),
      y=value=>height-padding.bottom-(height-padding.top-padding.bottom)*(value-lo)/(hi-lo);
    [min,(min+max)/2,max].forEach((value,index)=>{if(max===min&&index!==1)return;
      append(chart,svgEl("line",{x1:padding.left,y1:y(value),x2:width-padding.right,y2:y(value),class:"chart-grid"}));
      const label=svgEl("text",{x:padding.left-5,y:y(value)+3,"text-anchor":"end"});label.textContent=value.toFixed(2);append(chart,label);});
    append(chart,svgEl("path",{d:points.map((point,index)=>(index?"L":"M")+x(index)+" "+y(point.weight)).join(" "),class:"chart-line"}));
    points.forEach((point,index)=>{
      const circle=svgEl("circle",{cx:x(index),cy:y(point.weight),r:3.5,tabindex:0,role:"button","aria-label":localTime(point.item.timestamp)+"，权重 "+numberText(point.weight)});
      append(circle,append(svgEl("title"),document.createTextNode(localTime(point.item.timestamp)+" · "+numberText(point.weight)+"\n"+text(point.item.query))));
      const choose=()=>{const item=document.getElementById("history-"+index);if(item)item.scrollIntoView({block:"nearest"});};
      circle.addEventListener("click",choose);circle.addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();choose();}});
      // Event IDs are not put in DOM selectors; chronological ordinal is sufficient.
      point.item._chartOrdinal=index;append(chart,circle);
    });
    const left=svgEl("text",{x:padding.left,y:height-8}),right=svgEl("text",{x:width-padding.right,y:height-8,"text-anchor":"end"});
    left.textContent=localTime(points[0].item.timestamp,false);right.textContent=points.length===1?"":localTime(points[points.length-1].item.timestamp,false);append(chart,left,right);
    return chart;
  }
  function historyItem(item) {
    const change=object(item.change),parameters=object(item.parameters),entry=el("article","history-item");
    if(Number.isInteger(item._chartOrdinal))entry.id="history-"+item._chartOrdinal;
    append(entry,append(el("h4"),timeNode(item.timestamp)),el("div","weight-change",numberText(change.before_weight)+" → "+numberText(change.after_weight)));
    const steps=[];
    if(change.created_by==="static_seed" || change.before_weight==null && finite(change.after_seed))steps.push("静态种子 "+numberText(change.after_seed));
    if(change.decay_applied)steps.push("衰减 × "+numberText(parameters.decay??.99)+" = "+numberText(change.after_decay));
    if(finite(change.reinforcement_added)&&change.reinforcement_added!==0)steps.push("强化 +"+numberText(change.reinforcement_added));
    if(!steps.length)steps.push("记录值未发生强化变化");
    append(entry,el("div","history-step",steps.join(" · ")));
    if(item.query)append(entry,el("blockquote","",item.query));
    append(entry,el("p","", "事件 "+text(item.event_id)),jsonDetails({...item,_chartOrdinal:undefined},"展开这次变化的原始证据"));return entry;
  }
  function renderSource(data) {
    const panel=$("detail-panel"),version=object(data.version);
    append(panel,detailHeading("知识来源",version.path||data.source_id,data.source_id));
    list(data.warnings).forEach(value=>append(panel,warning("读取警告："+text(value),true)));
    if(data.found===false || !data.version){append(panel,warning("未找到这个知识版本。"));return;}
    append(panel,statusBadge(version.status),facts([["发布指针",data.published?"当前发布版本":"不是当前发布版本"],["更新指针",data.desired?"当前期望版本":"不是当前期望版本"],
      ["检索状态",data.active?"有有效材料":"未激活或已归档"],["原文件 SHA-256",version.digest],["创建时间",localTime(version.created_at)],
      ["标词输入 / 输出",text(version.input_tokens??0)+" / "+text(version.output_tokens??0)+" tokens"],["标词耗时",finite(version.elapsed_seconds)?version.elapsed_seconds.toFixed(3)+" 秒":"—"]]));
    if(version.error)append(panel,warning("版本处理失败："+text(version.error),true));
    if(data.truncated)append(panel,warning("来源详情已截断，不能把当前页面视为完整原文。"));
    if(data.pdf_archive) {
      const pdf=object(data.pdf_conversion),archive=object(data.pdf_archive),extractor=object(pdf.extractor),provenance=section("PDF 转换来源");
      const archiveStates={digest_matches_metadata:"归档字节 hash 与转换声明一致",digest_mismatch:"归档字节 hash 不一致",
        not_verified_size_limit:"超过单次 16 MiB 核验上限，未读取归档字节",invalid_archive_type:"归档类型异常",missing_archive:"PDF 归档及转换来源记录缺失",metadata_unavailable:"转换声明不可用，未建立 hash 关联",not_verified:"尚未核验"};
      const conversionStates={validated:"Markdown hash 与页范围声明一致",not_verified_markdown_truncated:"Markdown 被截断，未核验转换声明",invalid_or_unavailable:"转换声明异常或不可用",
        not_available_yet:"转换尚未产生声明",not_verified_metadata_limit:"声明超过读取上限，未核验",not_verified:"尚未核验"};
      append(provenance,facts([["PDF 物理页数",pdf.page_count],["提取器",text(extractor.name)+" "+text(extractor.version)],
        ["提取模式 / 策略",text(extractor.mode)+" / "+text(extractor.policy_version)],["原 PDF SHA-256",pdf.original_pdf_sha256],
        ["Markdown SHA-256",pdf.markdown_sha256],["转换声明核验",conversionStates[data.pdf_conversion_validation]||data.pdf_conversion_validation],
        ["归档 PDF 大小",finite(archive.byte_count)?archive.byte_count.toLocaleString()+" bytes":"—"],
        ["归档 PDF 字节核验",archiveStates[archive.verification_status]||archive.verification_status],
        ["归档字节 SHA-256",archive.sha256||"未计算"]]));
      list(pdf.warnings).forEach(value=>append(provenance,warning("提取声明："+text(value))));
      append(provenance,el("p","","物理页序与论文印刷页码可能不同。hash 与页范围的一致性不证明双栏阅读顺序、公式、表格或纸面语义已正确还原。"));
      append(provenance,jsonDetails(pdf,"查看冻结转换声明与页范围"));append(panel,provenance);
    }
    const source=section("版本原文");append(source,el("pre","source-text",version.raw_text||"（空文本）"));append(panel,source);
    const chunks=section("分块与标注词");
    list(data.chunks).forEach(chunk=>{const card=el("article","material-card");append(card,el("strong","","分块 "+text(chunk.chunk_index)+" · 字符起点 "+text(chunk.start_character)),el("p","",chunk.text),tags(chunk.marks));append(chunks,card);});
    if(!list(data.chunks).length)append(chunks,el("p","","尚无完成的标注分块。"));append(panel,chunks,jsonDetails(data));
  }
  const eventLabels={turn_start:"收到消息",memory_observation:"权重与排序",retrieval_record:"冻结召回材料",knowledge_retrieved:"召回耗时",context_watermark:"上下文水位",checkpoint_saved:"摘要存档",
    reply_context:"实际回复上下文",model_input:"模型输入",model_request:"模型请求",model_result:"模型输出",model_output:"模型输出",model_start:"模型调用开始",model_end:"模型调用完成",model_error:"模型调用失败",
    answer_generated:"生成回复与字面引用",delivery_start:"开始送达",answer_delivered:"实际送达与字面引用",turn_end:"本轮回执",knowledge_version_queued:"知识更新入队",knowledge_version_ready:"知识版本发布",
    label_start:"后台标词开始",label_end:"后台标词结束",scratch_retention:"24 小时清理",call_start:"调用阶段与独立预算",call_end:"调用计量与回执",call_rejected:"调用被熔断",
    input_gate:"输入 token 门限",http_request:"实际模型请求",http_response:"实际模型响应",http_error:"模型请求失败"};
  function renderTrace(data) {
    const panel=$("detail-panel"),verification=object(data.verification),records=retainedRecords(data.records);
    append(panel,detailHeading("运行证据",data.trace_id,"窗口起点 "+localTime(data.cutoff)),statusBadge(verification.status),
      el("p","muted","只核验仍保留的记录；完整生命周期状态与语义支持不能由这个状态推断。"));
    if(data.truncated)append(panel,warning("本 trace 已截断；页面展示的事件不是完整调用证据。"));
    list(verification.issues).forEach(issue=>append(panel,warning(typeof issue==="string"?issue:JSON.stringify(issue),true)));
    list(verification.warnings).forEach(item=>append(panel,warning(typeof item==="string"?item:JSON.stringify(item))));
    if(list(data.checkpoints).length)append(panel,warning("存在清理 checkpoint。窗口之前的原文已清除，跨窗口记录只能作部分核验。"));
    if(!records.length)append(panel,el("p","plain-empty","这条 trace 已不在当前 24 小时展示窗口内。"));
    records.forEach(record=>{
      const card=el("article","event-card"),fields=object(record.fields),title=eventLabels[record.event]||record.event;
      card.dataset.expiresAt=String(timeMillis(record.timestamp)+86400000);
      append(card,el("h4","",title),append(el("div","event-meta"),timeNode(record.timestamp),document.createTextNode(" · 序号 "+record.sequence+" · "+record.event)));
      if(record.event==="turn_start")append(card,el("p","event-text",object(fields.input).text));
      if(record.event==="retrieval_record")renderRecall(card,object(fields.record));
      if(record.event==="memory_observation")renderObservation(card,object(fields.audit));
      if(["answer_generated","answer_delivered"].includes(record.event))renderAnswer(card,object(fields.record));
      if(record.event==="turn_end")append(card,facts([["回执",fields.status],["错误码",fields.code||"—"]]));
      if(record.event==="context_watermark")append(card,facts([["原始上下文预留",fields.raw_reservation],["原始容量",fields.raw_capacity],["低水位",fields.low_watermark],["拟摘要来源序号",list(fields.planned_source_seqs).join("、")||"无"]]));
      if(record.event==="call_start")append(card,facts([["模型阶段",fields.stage],["模型",fields.model],["离线预留估计",fields.planning_reservation]]));
      if(record.event==="input_gate")append(card,facts([["实际输入 token",fields.input_tokens],["输入 token 上限",fields.limit],["是否放行",fields.admitted?"是":"否"]]));
      if(record.event==="call_end"){
        const result=object(fields.result);append(card,facts([["模型阶段",fields.stage],["调用状态",fields.status],["错误码",fields.code||"—"],
          ["实际输入 / 输出",finite(result.input_tokens)&&finite(result.output_tokens)?result.input_tokens+" / "+result.output_tokens+" tokens":"未提供"],
          ["响应 ID",result.response_id||"—"],["实际耗时",finite(result.elapsed_seconds)?result.elapsed_seconds.toFixed(3)+" 秒":"—"]]));
        if(result.text)append(card,el("p","event-text",result.text));
        if(fields.remote_usage_unknown)append(card,warning("远端计量未知；此处不能据本地失败推断远端没有执行或计费。"));
      }
      if(record.event==="http_request"){
        const payload=object(fields.payload);append(card,facts([["请求路径",fields.path],["输出 token 上限",payload.max_output_tokens??"该请求不生成输出"]]));
        const input=el("details");append(input,el("summary","","查看实际指令与输入"),el("pre","",JSON.stringify({instructions:payload.instructions,input:payload.input},null,2)));append(card,input);
      }
      if(record.event==="reply_context"){
        const input=el("details");append(input,el("summary","","查看回复指令与短期上下文"),el("pre","",JSON.stringify({instructions:fields.instructions,messages:fields.messages},null,2)));append(card,input);
      }
      const usage=object(fields.usage||object(fields.result).usage||object(fields.model_result).usage||object(fields.payload).usage);
      const budget=object(fields.budget||fields.limits);
      if(Object.keys(usage).length)append(card,facts(Object.entries(usage).map(([key,value])=>[key,value])));
      if(Object.keys(budget).length)append(card,facts(Object.entries(budget).map(([key,value])=>["预算 "+key,value])));
      if(finite(fields.elapsed_seconds))append(card,facts([["耗时",fields.elapsed_seconds.toFixed(3)+" 秒"]]));
      append(card,jsonDetails(record,"展开完整事件（输入 / 输出 / hash）"));append(panel,card);
    });
    append(panel,jsonDetails({verification:data.verification,checkpoints:data.checkpoints,truncated:data.truncated},"查看核验与截断声明"));
  }
  function renderRecall(card,retrieval) {
    append(card,el("p","event-text","查询："+text(retrieval.query)));
    if(!list(retrieval.materials).length)append(card,el("p","muted","本轮未召回知识材料。"));
    list(retrieval.materials).forEach(material=>{
      const item=el("div","material-card");append(item,append(el("strong"),el("span","citation-label","["+material.citation_id+"]"),document.createTextNode("材料 "+material.record_id)),
        sourceButton(material.source_id),el("p","",material.stored_text),tags(material.marks));
      const source=object(object(retrieval.sources)[material.source_id]);
      append(item,facts([["冻结版本",source.path||material.source_id],["文件版本 hash",source.original_file_bytes_sha256||"无版本元数据"]]));
      if(material.pdf_page_numbers) {
        append(item,facts([["PDF 物理页序",list(material.pdf_page_numbers).join("、")||"没有对应页范围"],
          ["相等分块出现次数",list(material.pdf_page_occurrences).length],["原 PDF hash 声明",object(source.pdf_conversion).original_pdf_sha256]]));
        append(item,el("p","muted","页序来自转换时的物理页范围；相等内容可能出现多处，记录保留全部匹配。Scratch 没有 PDF 二进制，不能独立核验原 PDF 字节。"));
      }
      append(item,jsonDetails({material,frozen_source:source},"查看本轮冻结原文与版本"));append(card,item);
    });
    append(card,el("p","muted","下方完整事件保留召回时的版本；来源按钮展示数据库中的同一版本，不代替本轮冻结证据。"));
  }
  function renderObservation(card,audit) {
    const observation=object(audit.observation),match=object(audit.match),selection=object(audit.selection);
    append(card,facts([["更新状态",observation.status],["字面直接命中",list(match.direct_hits).join("、")||"无"],
      ["扩展标注词",list(selection.expanded_marks).join("、")||"无"],["排除本体词",list(match.excluded_self_marks).join("、")||"无"],
      ["变更边数",list(observation.changed_edges).length],["选中材料",list(selection.selected_record_ids).join("、")||"无"]]));
    list(observation.changed_edges).forEach(change=>append(card,el("p","event-text",change.a+" ↔ "+change.b+"："+numberText(change.before_weight)+" → "+numberText(change.after_weight)+"（强化 +"+numberText(change.reinforcement_added??0)+"）")));
  }
  function renderAnswer(card,answer) {
    append(card,el("p","event-text",answer.text));
    list(answer.citations).forEach(citation=>{
      const row=el("div","citation-row",citation.marker+" · "+(citation.status==="resolved"?"字面关联已解析":"字面关联未解析")+" · 字符 "+citation.start_character+"–"+citation.end_character);
      if(citation.pdf_page_numbers)append(row,el("p","muted","材料候选物理页序："+list(citation.pdf_page_numbers).join("、")));
      if(citation.source_id)append(row,sourceButton(citation.source_id));append(card,row);
    });
    if(!list(answer.citations).length)append(card,el("p","muted","没有字面引用标记。"));
    append(card,el("p","muted","语义支持：未评估。字面引用关联不等于结论已经得到材料支持。"));
  }
  function setView(view) {
    state.view=view;
    ["graph","knowledge","scratch"].forEach(name=>{$("view-"+name).hidden=name!==view;});
    document.querySelectorAll(".tab").forEach(tab=>{const active=tab.dataset.view===view;tab.classList.toggle("active",active);tab.setAttribute("aria-pressed",String(active));});
  }
  function expireScratchView() {
    const now=Date.now();let removed=false;
    document.querySelectorAll(".event-card[data-expires-at]").forEach(card=>{
      if(Number(card.dataset.expiresAt)<=now){card.remove();removed=true;}
    });
    if(removed){
      const panel=$("detail-panel");
      if(!panel.querySelector(".retention-expiry-notice")){
        const notice=warning("部分记录已超过 24 小时，浏览器已移除其原文。刷新可读取当前保留窗口。");
        notice.classList.add("retention-expiry-notice");panel.prepend(notice);
      }
    }
    const scratch=object(object(state.snapshot).scratch);
    if(list(scratch.traces).some(trace=>!retainedAt(trace.timestamp,now))){
      scratch.traces=list(scratch.traces).filter(trace=>retainedAt(trace.timestamp,now));renderScratch();
    }
  }
  document.querySelectorAll(".tab").forEach(tab=>tab.addEventListener("click",()=>setView(tab.dataset.view)));
  ["mark-search","weight-layer","min-weight","show-archived"].forEach(id=>$(id).addEventListener(id==="mark-search"||id==="min-weight"?"input":"change",renderGraph));
  $("refresh").addEventListener("click",refresh);
  $("pause").addEventListener("click",()=>{state.paused=!state.paused;$("pause").textContent=state.paused?"恢复刷新":"暂停刷新";$("pause").setAttribute("aria-pressed",String(state.paused));
    renderConnection();
    if(state.snapshot)$("freshness").textContent="快照 "+localTime(state.snapshot.generated_at||state.snapshot.timestamp)+" · "+(state.paused?"手动刷新":"每 5 秒刷新");
    if(!state.paused)refresh();});
  $("fit-graph").addEventListener("click",()=>{state.transform={x:0,y:0,k:1};applyTransform();});
  $("zoom-in").addEventListener("click",()=>zoom(1.2));$("zoom-out").addEventListener("click",()=>zoom(1/1.2));
  $("network").addEventListener("wheel",event=>{event.preventDefault();zoom(Math.exp(-event.deltaY*.001),svgPoint(event));},{passive:false});
  $("network").addEventListener("pointerdown",event=>{if(event.button!==0||event.target.closest(".network-node")||event.target.closest(".network-edge-hit"))return;
    const point=svgPoint(event);state.drag={kind:"pan",x:point.x,y:point.y,origin:{...state.transform}};$("network").setPointerCapture(event.pointerId);$("network").classList.add("panning");});
  $("network").addEventListener("pointermove",event=>{if(!state.drag)return;const point=svgPoint(event),drag=state.drag;
    if(drag.kind==="node"){const t=state.transform;state.positions.set(drag.name,{x:(point.x-t.x)/t.k-drag.offsetX,y:(point.y-t.y)/t.k-drag.offsetY});
      if(Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)>4)state.suppressClick=true;updateGraphPositions();}
    else{state.transform.x=drag.origin.x+point.x-drag.x;state.transform.y=drag.origin.y+point.y-drag.y;applyTransform();}});
  function endDrag(event){const drag=state.drag,moved=state.suppressClick;state.drag=null;$("network").classList.remove("panning");
    if($("network").hasPointerCapture(event.pointerId))$("network").releasePointerCapture(event.pointerId);
    if(drag&&drag.kind==="node"&&!moved&&event.type==="pointerup")selectDetail("node",drag.name);
    setTimeout(()=>{state.suppressClick=false;},0);}
  $("network").addEventListener("pointerup",endDrag);$("network").addEventListener("pointercancel",endDrag);
  setInterval(expireScratchView,1000);
  setInterval(()=>{expireScratchView();if(!state.paused&&!document.hidden&&!state.drag)refresh();},5000);
  document.addEventListener("visibilitychange",()=>{expireScratchView();if(!document.hidden&&!state.paused)refresh();});
  window.addEventListener("pagehide",()=>{state.requestControllers.forEach(controller=>controller.abort());});
  refresh();
})();
