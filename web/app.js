'use strict';
const $ = id => document.getElementById(id);
const VERSION = 'lecture-summary-v2';
let current = null, connection = {online:false,models:[]}, timer, rendered = '', modalTrigger;
let importReport = '';
let libraryReady = Promise.resolve();
const library = new LectureFiles.Library(state => {
  const connected = ['ready','session'].includes(state.state);
  $('library-name').textContent = state.name || 'Папка не выбрана';
  $('sidebar-folder-name').textContent = state.name || 'Выбрать папку';
  $('library-status').textContent = state.state === 'permission' ? 'Нужно разрешение на чтение' : state.state === 'session' ? 'Подключена на эту сессию' : connected ? 'Подключена · только чтение' : 'Выберите один раз — используйте для всех лекций';
  $('library-card').classList.toggle('connected', connected);
  const feedback = $('folder-feedback');
  feedback.hidden = state.state === 'empty';
  feedback.textContent = state.state === 'permission' ? 'Папка «'+state.name+'»: нужен доступ на чтение. Нажмите на папку в меню слева.' : connected ? 'Папка «'+state.name+'» подключена. '+(state.count === null ? 'Изображения проверим при поиске скриншотов.' : 'Изображений: '+state.count+'.')+(state.state === 'session' ? ' После закрытия браузера выберите папку снова.' : '') : '';
  if (connected && state.count === 0) feedback.textContent += ' PNG, JPG или WebP не найдены. Выберите папку со скриншотами.';
  if (connected && state.count !== null) $('library-status').textContent += ' · изображений: '+state.count;
  $('connect-library').textContent = state.state === 'permission' ? 'Разрешить доступ' : connected ? 'Изменить папку' : 'Подключить папку';
  $('forget-library').hidden = state.state === 'empty';
  $('library-hint').textContent = connected ? 'Скриншоты найдём в папке «' + state.name + '» автоматически.' : 'Для автоматического поиска подключите папку со скриншотами.';
});
const states = {ready:'Лекция загружена',queued:'В очереди',running:'Обработка',done:'Summary готов',error:'Обработка прервана'};
function node(tag, text, className) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(className)n.className=className; return n; }
function error(text='') { $('error-banner').textContent=text; $('error-banner').hidden=!text; }
async function api(path,options) { const response=await fetch(path,options); const data=await response.json(); if(!response.ok)throw new Error(data.error||'Не удалось выполнить запрос.'); return data; }
async function checkHealth() {
  const previous=$('model-select').value;
  try { connection=await api('/api/models'); } catch { connection={online:false,models:[]}; }
  $('health').className='health '+(connection.online&&connection.models.length?'online':'offline');
  $('health-text').textContent=connection.online?(connection.models.length?'Ollama подключена':'Нужна vision-модель'):'Ollama не отвечает';
  $('model-select').replaceChildren();
  for(const name of connection.models) { const option=node('option',name);option.value=name;$('model-select').append(option); }
  if(!connection.models.length) {const option=node('option','Нет доступной модели');option.value='';$('model-select').append(option);}
  const preferred=current?.model||previous||'qwen3-vl:4b-instruct';if(connection.models.includes(preferred))$('model-select').value=preferred;
  updateStart();
}
function updateStart() { $('start').disabled=!current||!connection.online||!connection.models.length||!!current.missing_images?.length||['running','queued'].includes(current.status); }
async function loadHistory() {
  try {
    const jobs=await api('/api/jobs');$('history').replaceChildren();$('history-count').textContent=jobs.length;
    if(!jobs.length)$('history').append(node('p','Загруженных лекций пока нет.','history-empty'));
    for(const job of jobs) {const b=node('button',undefined,'history-item'+(current?.id===job.id?' active':''));b.append(node('span',job.title,'history-title'),node('small',states[job.status]));b.title=job.title;b.addEventListener('click',()=>openJob(job.id));$('history').append(b);}
  } catch(exc) {error(exc.message);}
}
function home() { clearTimeout(timer);current=null;rendered='';location.hash='';error();$('welcome').hidden=false;$('workspace').hidden=true;document.body.classList.remove('nav-open');$('mobile-menu').setAttribute('aria-expanded','false');loadHistory(); }
async function openJob(id,keepReport=false) {
  if(!keepReport)importReport='';
  clearTimeout(timer);error();rendered='';document.body.classList.remove('nav-open');
  $('mobile-menu').setAttribute('aria-expanded','false');
  try {current=await api('/api/jobs/'+id);location.hash=id;$('welcome').hidden=true;$('workspace').hidden=false;showTab('summary');render();loadHistory();}
  catch(exc){error(exc.message);}
}
// ZIP STORE writer lets the browser send a Markdown file and its assets without
// a dependency, compression service, or any external upload.
const crcTable=Array.from({length:256},(_,i)=>{let c=i;for(let n=0;n<8;n++)c=(c&1)?0xedb88320^(c>>>1):c>>>1;return c>>>0;});
function crc32(data){let crc=0xffffffff;for(const b of data)crc=crcTable[(crc^b)&255]^(crc>>>8);return (crc^0xffffffff)>>>0;}
async function makeZip(files,imagesOnly=false) {
  if(!files.length||files.length>1000)throw new Error('Выберите файлы одной лекции (до 1000 файлов).');
  const allowed=files.filter(x=>/\.(md|png|jpe?g|webp)$/i.test(x.path));
  if(imagesOnly&&allowed.some(x=>/\.md$/i.test(x.path)))throw new Error('Добавьте только изображения.');
  if(!imagesOnly&&allowed.filter(x=>/\.md$/i.test(x.path)).length!==1)throw new Error('Нужен ровно один .md-файл вместе с его скриншотами.');
  if(!allowed.length||allowed.reduce((n,x)=>n+x.file.size,0)>30*1024*1024)throw new Error('Общий размер файлов должен быть до 30 МБ.');
  const chunks=[],central=[],names=new Set();let offset=0;
  for(const entry of allowed){
    const path=entry.path.replace(/\\/g,'/');if(names.has(path))throw new Error('Повторяющееся имя файла: '+path);names.add(path);
    const name=new TextEncoder().encode(path);const bytes=new Uint8Array(await entry.file.arrayBuffer());const crc=crc32(bytes);
    const local=new Uint8Array(30+name.length),v=new DataView(local.buffer);
    v.setUint32(0,0x04034b50,true);v.setUint16(4,20,true);v.setUint16(6,0x800,true);v.setUint32(14,crc,true);v.setUint32(18,bytes.length,true);v.setUint32(22,bytes.length,true);v.setUint16(26,name.length,true);local.set(name,30);
    const header=new Uint8Array(46+name.length),h=new DataView(header.buffer);
    h.setUint32(0,0x02014b50,true);h.setUint16(4,20,true);h.setUint16(6,20,true);h.setUint16(8,0x800,true);h.setUint32(16,crc,true);h.setUint32(20,bytes.length,true);h.setUint32(24,bytes.length,true);h.setUint16(28,name.length,true);h.setUint32(42,offset,true);header.set(name,46);
    chunks.push(local,bytes);central.push(header);offset+=local.length+bytes.length;
  }
  const centralSize=central.reduce((n,c)=>n+c.length,0),end=new Uint8Array(22),e=new DataView(end.buffer);
  e.setUint32(0,0x06054b50,true);e.setUint16(8,allowed.length,true);e.setUint16(10,allowed.length,true);e.setUint32(12,centralSize,true);e.setUint32(16,offset,true);
  const result=new Blob([...chunks,...central,end],{type:'application/zip'});
  if(result.size>30*1024*1024)throw new Error('Получившийся архив превышает 30 МБ.');return result;
}
function selectedFiles(list){return Array.from(list).map(file=>({file,path:file.webkitRelativePath||file.name}));}
async function upload(files,attach=false) {
  // A screenshots-only folder is a library, regardless of the upload entry point.
  if(!attach && files.length && files.every(e=>/\.(png|jpe?g|webp)$/i.test(e.path))) {
    library.useFiles(files);
    if(current?.missing_images?.length)await fillFromLibrary();
    return;
  }
  if(!files.length)return;error();$('upload-title').textContent='Читаем файлы…';$('dropzone').setAttribute('aria-busy','true');$('add-images').disabled=true;
  try {
    importReport='';
    if(!attach && !files.some(e=>/\.zip$/i.test(e.path))) {
      const notes=files.filter(e=>/\.md$/i.test(e.path));
      if(notes.length===1) {
        $('upload-title').textContent='Находим связанные скриншоты…';
        await libraryReady;
        const note=notes[0];
        const resolved=await library.resolve(LectureFiles.references(await note.file.text()),files,note.path);
        files=[{file:note.file,path:note.file.name},...resolved.files];
        importReport=resolved.files.length?'Автоматически подобрано изображений: '+resolved.files.length+'.':'';
        if(resolved.ambiguous.length)importReport+=' Неоднозначных имён: '+resolved.ambiguous.length+'. Выберите папку ближе к нужным изображениям.';
        if(library.state==='permission')importReport+=' Для сохранённой папки нужно нажать «Разрешить доступ».';
      }
    }
    const zip=files.length===1&&/\.zip$/i.test(files[0].path)&&!attach?files[0].file:await makeZip(files,attach);
    if(zip.size>30*1024*1024)throw new Error('Максимальный размер — 30 МБ.');
    const path=attach?'/api/jobs/'+current.id+'/attachments':'/api/jobs';
    const job=await api(path,{method:'POST',headers:{'Content-Type':'application/zip'},body:zip});await openJob(job.id,true);
  } catch(exc){error(exc.message);} finally{$('upload-title').textContent='Перетащите сюда конспект';$('dropzone').removeAttribute('aria-busy');$('add-images').disabled=false;for(const id of ['file-input','folder-input','attachments-input'])$(id).value='';}
}
async function chooseLibrary() {
  try {
    error();
    // Trigger fallback synchronously while the browser still has the click gesture.
    if(!window.showDirectoryPicker) { $('library-input').value=''; $('library-input').click(); return; }
    await library.choose();
    if(current?.missing_images?.length && ['ready','session'].includes(library.state))await fillFromLibrary();
  } catch(exc) { if(exc.name!=='AbortError')error('Не удалось подключить папку: '+exc.message); }
}
async function fillFromLibrary() {
  if(!current?.missing_images?.length)return;
  try {
    const resolved=await library.resolve(current.missing_images);
    if(resolved.files.length)await upload(resolved.files,true);
    else error(resolved.ambiguous.length?'В папке несколько файлов с одинаковыми именами. Выберите более точную папку.':'В выбранной папке недостающие скриншоты не найдены.');
  } catch(exc){error(exc.message);}
}
async function directoryFiles(entry,prefix='') {
  if(entry.isFile){const file=await new Promise((resolve,reject)=>entry.file(resolve,reject));return [{file,path:prefix+file.name}];}
  const reader=entry.createReader();let all=[];
  while(true){const batch=await new Promise((resolve,reject)=>reader.readEntries(resolve,reject));if(!batch.length)break;all.push(...batch);if(all.length>1000)throw new Error('Слишком много файлов в папке.');}
  const result=[];for(const child of all)result.push(...await directoryFiles(child,prefix+entry.name+'/'));return result;
}
function showTab(tab){$('summary-view').hidden=tab!=='summary';$('sources-view').hidden=tab!=='sources';$('tab-summary').classList.toggle('active',tab==='summary');$('tab-sources').classList.toggle('active',tab==='sources');}
// Render only text nodes and a small set of generated elements; model HTML and
// external image/link syntax cannot become active browser content.
function inline(parent,text){let end=0;for(const m of text.matchAll(/`([^`]+)`|\*\*([^*]+)\*\*/g)){parent.append(document.createTextNode(text.slice(end,m.index)),node(m[1]!==undefined?'code':'strong',m[1]??m[2]));end=m.index+m[0].length;}parent.append(document.createTextNode(text.slice(end)));}
function markdown(text){const result=document.createDocumentFragment();let list,code,table;
  for(const line of text.split('\n')){
    if(/^\s*```/.test(line)){if(code)code=null;else{code=node('pre','');result.append(code);}list=null;table=null;continue;}
    if(code){code.textContent+=line+'\n';continue;}if(!line.trim()){list=null;table=null;continue;}
    if(/^\s*\|/.test(line)){const cells=line.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());if(cells.every(c=>/^:?-+:?$/.test(c)))continue;const first=!table;if(!table){table=node('table');result.append(table);}const row=node('tr');for(const c of cells){const cell=node(first?'th':'td');inline(cell,c);row.append(cell);}table.append(row);list=null;continue;}
    table=null;const bullet=line.match(/^\s*(?:[-*+] |\d+[.)] )(.*)/);if(bullet){if(!list){list=node('ul');result.append(list);}const li=node('li');inline(li,bullet[1]);list.append(li);continue;}
    list=null;const heading=line.match(/^#{1,6}\s+(.*)/),p=node(heading?'h3':'p');inline(p,heading?heading[1]:line);result.append(p);
  }return result;
}
function render(){
  const job=current;if(!job)return;
  const missing=job.missing_images||[],busy=['running','queued'].includes(job.status),legacy=job.pipeline_version!==VERSION,done=job.status==='done'&&!legacy;
  $('lecture-title').textContent=job.title;$('job-status').textContent=missing.length?'НЕ ХВАТАЕТ ИЗОБРАЖЕНИЙ':states[job.status];
  $('import-report').hidden=!importReport;$('import-report').textContent=importReport;
  $('resolve-library').hidden=!missing.length;
  $('lecture-meta').textContent='Скриншоты: '+job.image_count+' из '+(job.requested_images??job.image_count)+' доступны · '+job.blocks.length+' блоков с подписями'+(done?' · '+Math.round(job.duration_seconds)+' с':'');
  $('missing-panel').hidden=!missing.length;$('missing-title').textContent='Не хватает скриншотов: '+missing.length;
  $('missing-list').replaceChildren(...missing.map(name=>node('li',name)));
  $('legacy-message').hidden=!legacy||!!missing.length||busy;
  $('setup').hidden=busy||done;$('start').textContent=job.status==='error'?'Повторить обработку':'Создать summary';
  $('download').hidden=!done;$('download').href='/api/jobs/'+job.id+'/download';
  $('progress-card').hidden=!busy;$('progress-title').textContent=job.status==='queued'?'Ожидаем модель':job.stage;
  const imageBlocks=job.blocks.filter(b=>b.image).length,read=job.images_read||0;
  $('progress-count').textContent=job.phase==='synthesis'?'Сборка summary':read+' / '+imageBlocks;
  $('progress-fill').style.width=(job.phase==='synthesis'?93:Math.round(read/Math.max(1,imageBlocks)*85))+'%';
  $('progress-help').textContent=job.phase==='synthesis'?'Изображения прочитаны. Объединяем их содержание и подписи в один текст.':'Каждый скриншот читается отдельно. Прочитанный текст можно открыть во вкладке источников.';
  $('read-count').textContent=read?'Прочитано изображений: '+read:'';$('source-count').textContent=job.blocks.length;
  const warnings=legacy?[]:job.warnings;$('warnings-card').hidden=!warnings.length;$('warnings-title').textContent='Замечания к материалам ('+warnings.length+')';$('warnings').replaceChildren(...warnings.map(w=>node('li',w)));
  if(job.error)error(job.error);
  $('empty-summary').hidden=done;
  $('empty-summary').querySelector('h2').textContent=missing.length?'Сначала добавьте скриншоты':'Здесь будет summary всей лекции';
  $('empty-summary').querySelector('p').textContent=missing.length?'Подписи не заменяют содержимое отсутствующих изображений.':busy?'Читаем материалы. После анализа всей лекции здесь появится общий результат.':'Результат объединит содержание изображений и ваши подписи.';
  const key=job.id+':'+(done?'done':'pending')+':'+read;
  if(rendered!==key){rendered=key;$('summary-content').replaceChildren();$('toc').replaceChildren();$('toc-card').hidden=!done;
    if(done){$('summary-content').append(node('div',job.overview,'overview'));for(const [index,section] of job.sections.entries()){const article=node('section',undefined,'summary-section');article.id='topic-'+index;article.append(node('h2',section.title),markdown(section.summary));const jump=node('button',String(index+1).padStart(2,'0')+'  '+section.title);jump.addEventListener('click',()=>{showTab('summary');article.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'instant':'smooth',block:'start'});});$('toc').append(jump);const refs=node('div',undefined,'source-links');refs.append(node('span','Источники:'));for(const id of section.source_ids){const b=node('button',id.replace(/^b0*/,''),'source-chip');b.addEventListener('click',()=>source(id));refs.append(b);}article.append(refs);$('summary-content').append(article);}}
    $('sources-view').replaceChildren();for(const block of job.blocks){const row=node('button',undefined,'source-row');if(block.image){const image=node('img',undefined,'source-thumb');image.src='/api/jobs/'+job.id+'/images/'+block.id;image.alt='';image.loading='lazy';row.append(image);}else row.append(node('span',block.attachment_requested?'?':'¶','source-thumb source-text-icon'));const text=node('div');text.append(node('strong',block.image?.split('/').pop()||block.attachment_target||'Текст заметки'));const extraction=job.extractions?.find(e=>e.source_id===block.id);text.append(node('p',block.attachment_requested&&!block.image?'Файл не приложен':extraction?'Скриншот прочитан':'Строки '+block.line_start+'–'+block.line_end,block.attachment_requested&&!block.image?'missing-label':extraction?'read-label':''));row.append(text);row.addEventListener('click',()=>source(block.id));$('sources-view').append(row);}
  }
  updateStart();
  if(busy){clearTimeout(timer);const id=job.id;timer=setTimeout(async()=>{try{const next=await api('/api/jobs/'+id);if(current?.id!==id)return;const changed=next.status!==current.status;current=next;render();if(changed)loadHistory();}catch(exc){error(exc.message);timer=setTimeout(()=>openJob(id),3000);}},1800);}
}
function source(id){const block=current.blocks.find(b=>b.id===id);if(!block)return;modalTrigger=document.activeElement;const read=current.extractions?.find(e=>e.source_id===id);$('source-modal-title').textContent=read?.title||block.image?.split('/').pop()||block.attachment_target||'Текст лекции';$('source-lines').textContent=block.note+' · строки '+block.line_start+'–'+block.line_end;$('source-image').hidden=!block.image;if(block.image)$('source-image').src='/api/jobs/'+current.id+'/images/'+id;else $('source-image').removeAttribute('src');$('source-notes').textContent=block.text||'Подписи к этому скриншоту нет.';$('source-extraction').textContent=read?.content||(block.image?'Скриншот ещё не прочитан.':block.attachment_requested?'Файл изображения не приложен.':'Этот блок содержит только текст.');$('source-modal').hidden=false;document.body.style.overflow='hidden';$('close-modal').focus();}
function closeModal(){$('source-modal').hidden=true;document.body.style.overflow='';modalTrigger?.focus();}
$('close-modal').addEventListener('click',closeModal);$('source-modal').addEventListener('click',event=>{if(event.target===$('source-modal'))closeModal();});
document.addEventListener('keydown',event=>{if(!$('source-modal').hidden){if(event.key==='Escape')closeModal();if(event.key==='Tab'){event.preventDefault();$('close-modal').focus();}}else if(event.key==='Escape'){document.body.classList.remove('nav-open');$('mobile-menu').setAttribute('aria-expanded','false');}});
$('new-note').addEventListener('click',home);$('health').addEventListener('click',checkHealth);
$('nav-new').addEventListener('click',home);$('mobile-menu').addEventListener('click',()=>{const open=document.body.classList.toggle('nav-open');$('mobile-menu').setAttribute('aria-expanded',String(open));});
$('connect-library').addEventListener('click',chooseLibrary);$('sidebar-library').addEventListener('click',chooseLibrary);$('resolve-library').addEventListener('click',chooseLibrary);
$('forget-library').addEventListener('click',()=>library.forget());
$('library-input').addEventListener('change',async event=>{library.useFiles(selectedFiles(event.target.files));if(current?.missing_images?.length)await fillFromLibrary();});
$('choose-files').addEventListener('click',()=>$('file-input').click());$('choose-folder').addEventListener('click',()=>$('folder-input').click());$('add-images').addEventListener('click',()=>$('attachments-input').click());
for(const id of ['file-input','folder-input'])$(id).addEventListener('change',event=>upload(selectedFiles(event.target.files)));
$('attachments-input').addEventListener('change',event=>upload(selectedFiles(event.target.files),true));
$('dropzone').addEventListener('click',event=>{if(!event.target.closest('button'))$('file-input').click();});
$('dropzone').addEventListener('keydown',event=>{if(event.target===$('dropzone')&&['Enter',' '].includes(event.key)){event.preventDefault();$('file-input').click();}});
for(const name of ['dragenter','dragover'])$('dropzone').addEventListener(name,event=>{event.preventDefault();$('dropzone').classList.add('drag');});
$('dropzone').addEventListener('dragleave',()=>$('dropzone').classList.remove('drag'));
$('dropzone').addEventListener('drop',async event=>{event.preventDefault();$('dropzone').classList.remove('drag');try{const entries=Array.from(event.dataTransfer.items||[]).map(i=>i.webkitGetAsEntry?.()).filter(Boolean);let files=[];if(entries.length){for(const entry of entries)files.push(...await directoryFiles(entry));}else files=selectedFiles(event.dataTransfer.files);await upload(files);}catch(exc){error(exc.message);}});
$('tab-summary').addEventListener('click',()=>showTab('summary'));$('tab-sources').addEventListener('click',()=>showTab('sources'));
$('start').addEventListener('click',async()=>{if(!current)return;error();$('start').disabled=true;try{current=await api('/api/jobs/'+current.id+'/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:$('model-select').value,detail:$('detail').value})});render();loadHistory();}catch(exc){error(exc.message);updateStart();}});
window.addEventListener('hashchange',()=>{const id=location.hash.slice(1);if(/^[a-f0-9]{32}$/.test(id)&&current?.id!==id)openJob(id);else if(!id&&current)home();});
libraryReady=library.restore();loadHistory();checkHealth();if(/^#[a-f0-9]{32}$/.test(location.hash))openJob(location.hash.slice(1));
