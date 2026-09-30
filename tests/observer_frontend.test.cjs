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
const search=observer.graphSelection(graph,{...options,query:"BETA"});
assert.deepEqual(search.nodes.map(node=>node.id),["alpha","beta"]);
assert.deepEqual(search.edges,[graph.edges[0]],"search must show matched word and its direct supported neighbors");
assert.equal(observer.graphSelection(graph,{...options,query:"no matching word"}).nodes.length,0);
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
console.log("Observer frontend contract checks passed.");
