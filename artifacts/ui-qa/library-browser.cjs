const fs=require('node:fs/promises');
const delay=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const target=await(await fetch('http://127.0.0.1:9229/json/new?'+encodeURIComponent('http://127.0.0.1:8765/'),{method:'PUT'})).json();await delay(1200);
 const ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener('open',r));let seq=0;const pending=new Map(),exceptions=[];
 ws.addEventListener('message',({data})=>{const m=JSON.parse(data);if(m.method==='Runtime.exceptionThrown')exceptions.push(m.params.exceptionDetails.text);if(m.id){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result);}});
 const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
 const ev=async expression=>{const x=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(x.exceptionDetails)throw new Error(JSON.stringify(x.exceptionDetails));return x.result.value;};
 const assert=(ok,label)=>{if(!ok)throw new Error(label);console.log('OK:',label);};
 const shot=async name=>{const s=await send('Page.captureScreenshot',{format:'png'});await fs.writeFile('artifacts/ui-qa/'+name+'.png',Buffer.from(s.data,'base64'));};
 await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1050,deviceScaleFactor:1,mobile:false});
 assert(await ev('document.title.includes("Лекта")'),'Redesigned home loads');await shot('lecta-home');
 // Use real browser file handles from OPFS, with only the native picker substituted.
 // This exercises IndexedDB structured cloning and reload, not a fake persistence layer.
 const stored=await ev(`(async()=>{
   await library.forget();
   const root=await navigator.storage.getDirectory();const dir=await root.getDirectoryHandle('QA screenshots',{create:true});
   const nested=await dir.getDirectoryHandle('images',{create:true});
   const bytes=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lp0AAAAASUVORK5CYII='),c=>c.charCodeAt(0));
   for(const name of ['slide.png','unused.png']){const f=await nested.getFileHandle(name,{create:true});const w=await f.createWritable();await w.write(bytes);await w.close();}
   window.showDirectoryPicker=async()=>dir;await library.choose();return {state:library.state,name:library.name};
 })()`);
 assert(stored.state==='ready'&&stored.name==='QA screenshots','Read-only folder handle is stored');
 await send('Page.reload',{ignoreCache:true});await delay(1000);
 for(let i=0;i<20;i++){if(await ev('library.state!=="empty"'))break;await delay(100);}
 assert(await ev('library.name==="QA screenshots" && library.handle!==null'),'Folder survives page reload through IndexedDB');
 assert(await ev('library.state==="ready"'),'Restored read permission is respected');
 const doc=await send('DOM.getDocument');const input=await send('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'#file-input'});
 await send('DOM.setFileInputFiles',{nodeId:input.nodeId,files:['C:\\ai-project-new\\artifacts\\ui-qa\\direct-upload\\note.md']});
 for(let i=0;i<50;i++){if(await ev('current!==null'))break;await delay(150);}
 assert(await ev('current?.image_count===1&&current?.missing_images.length===0'),'Uploading only MD automatically attaches its screenshot');
 assert(await ev('current.total_images===1'),'Unreferenced library image is not uploaded');
 assert(await ev('document.getElementById("import-report").textContent.includes("1")'),'Automatic match count is visible');
 await ev('home()');await shot('lecta-connected');
 await ev('openJob("5743b9629bab4fd1b12e2d21f8d6a4f1")');await delay(350);
 assert(await ev('document.querySelectorAll(".summary-section").length===5'),'Existing summary still renders');
 assert(await ev('document.querySelectorAll("#toc button").length===5'),'Table of contents matches the summary');await shot('lecta-reader');
 await ev('document.getElementById("tab-sources").click();source("b009")');await delay(350);
 assert(await ev('document.getElementById("source-extraction").textContent.includes("PyMuPDF")'),'Source image and extraction remain available');await ev('closeModal();showTab("summary")');
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await delay(250);
 assert(await ev('document.documentElement.scrollWidth<=innerWidth'),'Reader has no mobile overflow');await shot('lecta-mobile-reader');
 await ev('home()');await delay(150);assert(await ev('document.documentElement.scrollWidth<=innerWidth'),'Home has no mobile overflow');await shot('lecta-mobile-home');
 await ev('document.getElementById("mobile-menu").click()');assert(await ev('document.body.classList.contains("nav-open")'),'Mobile navigation opens');await ev('document.getElementById("mobile-menu").click()');
 await ev('library.forget()');await send('Page.reload',{ignoreCache:true});await delay(600);assert(await ev('library.state==="empty"'),'Forget folder persists after reload');
 assert(exceptions.length===0,'No browser JavaScript exceptions');
 await fs.writeFile('artifacts/ui-qa/library-browser-results.json',JSON.stringify({status:'passed',exceptions,picker:'substituted with real OPFS handle; IndexedDB and file reads native'},null,2));
 await send('Browser.close');ws.close();
})().catch(error=>{console.error(error);process.exit(1);});
