const fs=require('node:fs/promises');
const delay=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  const target=await(await fetch('http://127.0.0.1:9229/json/new?'+encodeURIComponent('http://127.0.0.1:8765/'),{method:'PUT'})).json();
  await delay(1500);
  const ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r));let seq=0;const pending=new Map(),errors=[];
  ws.addEventListener('message',({data})=>{const x=JSON.parse(data);if(x.method==='Runtime.exceptionThrown')errors.push(x.params.exceptionDetails.text);if(x.id){const p=pending.get(x.id);pending.delete(x.id);x.error?p.reject(new Error(JSON.stringify(x.error))):p.resolve(x.result);}});
  const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject:error=>reject(new Error(method+': '+error.message))});ws.send(JSON.stringify({id,method,params}));});
  const ev=async expression=>{const x=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(x.exceptionDetails)throw new Error(JSON.stringify(x.exceptionDetails));return x.result.value;};
  const assert=(ok,message)=>{if(!ok)throw new Error(message);console.log('OK:',message);};
  const shot=async name=>{const x=await send('Page.captureScreenshot',{format:'png'});await fs.writeFile('artifacts/ui-qa/'+name+'.png',Buffer.from(x.data,'base64'));};
  await send('Runtime.enable');await send('Page.enable');
  await delay(500);
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  assert(await ev('document.title==="Summary лекции"'),'Neutral interface loads');await shot('v2-home');
  const doc=await send('DOM.getDocument');const input=await send('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'#file-input'});
  await send('DOM.setFileInputFiles',{nodeId:input.nodeId,files:['C:\\ai-project-new\\artifacts\\ui-qa\\direct-upload\\note.md','C:\\ai-project-new\\artifacts\\ui-qa\\direct-upload\\slide.png']});
  for(let i=0;i<40;i++){if(await ev('!document.getElementById("workspace").hidden'))break;await delay(250);}
  assert(await ev('current?.image_count===1&&current?.requested_images===1&&current?.missing_images.length===0'),'Direct Markdown + screenshot upload resolves image');
  assert(await ev('!document.getElementById("start").disabled'),'Complete input can start');
  await ev('openJob("b7dad86d467346798cc7e8643bfa417c")');await delay(300);
  assert(await ev('!document.getElementById("missing-panel").hidden&&document.querySelectorAll("#missing-list li").length===42'),'Actual incomplete lecture shows all 42 missing screenshots');
  assert(await ev('document.getElementById("start").disabled&&document.getElementById("download").hidden'),'Incomplete lecture cannot produce a fake completed summary');await shot('v2-missing');
  await ev('openJob("5743b9629bab4fd1b12e2d21f8d6a4f1")');await delay(400);
  assert(await ev('current.images_read===32'),'All 32 image readings are visible');
  await ev('document.getElementById("tab-sources").click(); source("b009")');await delay(400);
  assert(await ev('document.getElementById("source-extraction").textContent.includes("PyMuPDF")'),'Read screenshot exposes table text absent from caption');
  assert(await ev('document.getElementById("source-image").complete&&document.getElementById("source-image").naturalWidth>0'),'Original screenshot displays');await shot('v2-read-screenshot');
  await ev('closeModal();showTab("summary")');
  for(let i=0;i<100;i++){if(await ev('current.status==="done"||current.status==="error"'))break;await delay(1000);}
  assert(await ev('current.status==="done"'),'Whole lecture synthesis completed');
  assert(await ev('current.sections.length>0&&current.sections.length<20&&document.querySelectorAll(".overview").length===1'),'One overview and grouped topics replace per-slide snippets');
  assert(await ev('current.sections.flatMap(s=>s.source_ids).join(",")===current.blocks.map(b=>b.id).join(",")'),'All source references remain in lecture order');
  const download=await ev('fetch(document.getElementById("download").href).then(r=>r.text())');assert(download.includes('## Источники')&&download.includes('source-b032'),'Markdown export contains the summary and all source references');await shot('v2-summary');
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await delay(250);assert(await ev('document.documentElement.scrollWidth<=innerWidth'),'Mobile has no horizontal overflow');await shot('v2-mobile');
  assert(await ev('(()=>{const d=document.createElement("div");d.append(markdown("<img src=x onerror=alert(1)>"));return !d.querySelector("img")})()'),'Model HTML stays inert');
  assert(errors.length===0,'No browser JavaScript exceptions');await fs.writeFile('artifacts/ui-qa/v2-results.json',JSON.stringify({status:'passed',errors},null,2));
  await send('Browser.close');ws.close();
})().catch(e=>{console.error(e);process.exitCode=1;process.exit(1);});
