"use strict";
const assert = require("node:assert/strict");
const observer = require("../indeces/web/observer.js");
const graph = {
  nodes:[{id:"alpha",frequency:3},{id:"beta",frequency:2},{id:"archived",frequency:0},{id:"single",frequency:1},{id:"<img src=x onerror=alert(1)>",frequency:1}],
  edges:[
    {a:"alpha",b:"beta",active_source_support:true,static_weight:.24,dynamic_weight:2.22,effective_weight:2.22},
    {a:"alpha",b:"archived",active_source_support:false,static_weight:null,dynamic_weight:7.31,effective_weight:null},
    {a:"alpha",b:"<img src=x onerror=alert(1)>",active_source_support:true,static_weight:.13,dynamic_weight:null,effective_weight:.13}
  ]
};
const options = {layer:"effective",archived:false,minimum:0,query:""};
assert.equal(observer.graphSelection(graph,options).edges.length,2,"retired association must not appear as current support");
assert.equal(observer.edgeWeight(graph.edges[1],"effective"),null,"archived edge must keep null retrieval weight");
assert.equal(observer.graphSelection(graph,{...options,archived:true}).edges.length,3,"historical display can show preserved dynamic value");
assert.equal(observer.displayWeight(graph.edges[1],"effective"),7.31);
assert.deepEqual(observer.graphSelection(graph,{...options,minimum:1}).edges,[graph.edges[0]],"dynamic weight >1 must not be clamped to NPMI range");
assert.deepEqual(observer.graphSelection(graph,{...options,layer:"static",minimum:.2}).edges,[graph.edges[0]],"static filtering must not borrow dynamic score");
assert.equal(observer.graphSelection(graph,{...options,layer:"dynamic"}).edges.length,1,"absent dynamic values must not be fabricated");
assert.equal(observer.graphSelection({nodes:graph.nodes,edges:[{...graph.edges[0],static_weight:0}]},{...options,layer:"static"}).edges.length,0,"rounded zero cache weights are not usable positive static edges");
const search=observer.graphSelection(graph,{...options,query:"BETA"});
assert.deepEqual(search.nodes.map(node=>node.id),["alpha","beta"]);
assert.deepEqual(search.edges,[graph.edges[0]],"search must show matched word and its direct supported neighbors");
assert.equal(observer.graphSelection(graph,{...options,query:"no matching word"}).nodes.length,0);
assert.equal(observer.graphSelection(graph,{...options,query:"archived"}).nodes.length,0,"search must not reveal historical nodes until explicitly enabled");
assert.equal(observer.graphSelection(graph,{...options,archived:true,query:"archived"}).nodes.length,2);
assert.equal(observer.graphSelection(graph,{...options,query:"<img"}).nodes[1].id,"<img src=x onerror=alert(1)>","untrusted labels stay literal data");
assert.notEqual(observer.edgeId({a:"a,b",b:"c"}),observer.edgeId({a:"a",b:"b,c"}),"edge identity must not collapse punctuation");
const history=[{timestamp:3,change:{after_weight:2}},{timestamp:1,change:{after_weight:.2}},{timestamp:2,change:{after_weight:null}},{timestamp:4,change:{after_weight:Infinity}}];
assert.deepEqual(observer.historyPoints(history).map(point=>point.weight),[.2,2]);
assert.equal(observer.numberText(null),"—");assert.equal(observer.numberText(2.99),"2.99");assert.equal(observer.numberText(0),"0");
const now=Date.parse("2026-10-01T12:00:00Z");
assert.equal(observer.retainedAt("2026-09-30T12:00:00Z",now),false,"24-hour boundary is expired inclusively");
assert.equal(observer.retainedAt("2026-09-30T12:00:00.001Z",now),true);
assert.equal(observer.retainedAt("2026-10-01T12:00:00.001Z",now),false,"future timestamps cannot extend display retention");
assert.equal(observer.retainedAt("bad timestamp",now),false);
assert.deepEqual(observer.retainedRecords([{timestamp:"2026-09-30T11:00:00Z",private:"expired"},{timestamp:"2026-10-01T11:00:00Z",private:"retained"}],now).map(record=>record.private),["retained"]);
assert.equal(observer.connectionPresentation({paused:true,readFailed:true,snapshot:{}}).className,"status-chip error","pause must preserve failed snapshot status");
assert.match(observer.connectionPresentation({paused:true,readFailed:true,snapshot:{}}).label,/未更新/);
assert.equal(observer.connectionPresentation({paused:true,readFailed:false,snapshot:null}).className,"status-chip quiet","pause before a snapshot cannot claim successful read");
assert.match(observer.connectionPresentation({paused:false,readFailed:false,snapshot:{}}).label,/快照可读取/);
// A minimal offline DOM exercises the actual refresh/pause/detail callbacks.
// It never starts a server or reads user state.
const vm = require("node:vm"), fs = require("node:fs"), path = require("node:path");
class Element {
  constructor(tag="div") { this.tagName=tag;this.children=[];this.listeners={};this.dataset={};this.attributes={};this._text="";this.hidden=false;this.value="";this.scrollTop=0; }
  set textContent(value) {this._text=String(value);this.children=[];}
  get textContent() {return this._text+this.children.map(child=>child.textContent).join("");}
  appendChild(child) {this.children.push(child);child.parent=this;return child;}
  replaceChildren(...children) {this._text="";this.children=[];children.forEach(child=>this.appendChild(child));}
  remove() {if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
  setAttribute(name,value) {this.attributes[name]=value;}
  addEventListener(name,callback) {this.listeners[name]=callback;}
}
async function browserContracts() {
  const elements=new Map(), get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const tabs=["graph","knowledge","scratch"].map(view=>{const element=new Element("button");element.dataset.view=view;return element;});
  get("weight-layer").value="static";get("min-weight").value="0";
  const snapshot={generated_at:new Date().toISOString(),ranking_mode:"static",graph:{nodes:[{id:"alpha",frequency:1},{id:"beta",frequency:1}],edges:[{a:"alpha",b:"beta",static_weight:1,dynamic_weight:2,effective_weight:1,active_source_support:true}],diagnostics:{active_records:1,active_source_versions:1,active_marks:2,positive_edges:1,supported_pairs:1,supported_without_positive:0,isolated_marks:0,median_positive_weight:1,single_support_positive_edges:1,single_source_positive_edges:1,unit_weight_edges:1}},knowledge:{},scratch:{traces:[]}};
  let snapshotFails=false;
  const context={document:{getElementById:get,createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),createTextNode:value=>{const e=new Element();e.textContent=value;return e;},querySelectorAll:()=>tabs,addEventListener:()=>{}},window:{addEventListener:()=>{}},location:{href:"http://127.0.0.1:12345/synthetic-only/"},URL,AbortController,Date,console,setInterval:()=>1,setTimeout:()=>1,clearTimeout:()=>{},fetch:async url=>{if(url.pathname.endsWith("api/snapshot")&&!snapshotFails)return {ok:true,json:async()=>snapshot};return {ok:false,status:503};}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../indeces/web/observer.js"),"utf8"),context);
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  await flush();
  assert.match(get("connection-status").textContent,/快照可读取/);
  assert.match(get("npmi-summary").textContent,/单一来源支持 1/);
  snapshotFails=true;await get("refresh").listeners.click();
  assert.match(get("connection-status").className,/error/);
  get("pause").listeners.click();
  assert.match(get("connection-status").className,/error/,"actual pause callback must preserve failure");
  assert.match(get("connection-status").textContent,/未更新.*已暂停/);
  snapshotFails=false;await get("refresh").listeners.click();
  assert.match(get("connection-status").textContent,/已暂停/);
  const hit=get("network-edges").children[1];hit.listeners.click({stopPropagation:()=>{}});await flush();
  for(let i=0;i<3;i++)await get("refresh").listeners.click();
  assert.equal(get("detail-panel").children.filter(child=>child.className?.includes("inline-error")).length,1,"repeated detail failures must replace their warning rather than accumulate");
  assert.match(get("layer-description").textContent,/NPMI/);
}
browserContracts().then(()=>console.log("Observer frontend contract checks passed.")).catch(error=>{console.error(error);process.exitCode=1;});
