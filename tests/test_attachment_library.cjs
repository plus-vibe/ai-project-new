const {test} = require('node:test');
const assert = require('node:assert/strict');
const {references, normalize, match, Library} = require('../web/attachments.js');

test('wiki and Markdown references preserve paths, Unicode and source order', () => {
  const text = '![[images/Слайд 1.png|400]]\n![slide](<images/two%20words.webp>)\n![[images/Слайд 1.png]]\n```md\n![[ignored.png]]\n```\n~~~\n![[also-ignored.png]]\n~~~';
  assert.deepEqual(references(text), ['images/Слайд 1.png', 'images/two words.webp']);
  assert.equal(normalize('images/./slide.png'), 'images/slide.png');
});
test('remote, absolute and traversal targets never become file reads', () => {
  for (const target of ['https://example.test/p.png','/p.png','../p.png','C:/p.png','data:image/png;base64,test','%2e%2e/p.png','bad%xx.png']) assert.equal(normalize(target), null);
  assert.deepEqual(references('![external](https://example.test/p.png)'), []);
});
test('exact relative path wins over duplicate basenames; ambiguity never guesses', () => {
  const files = [{path:'course/assets/slide.png'}, {path:'other/slide.png'}];
  assert.equal(match('assets/slide.png',files,'course/note.md').entries[0],files[0]);
  assert.equal(match('slide.png',files).status,'ambiguous');
  assert.equal(match('missing.png',files).status,'missing');
});
test('library recursively reads only referenced image bytes, including hidden attachment folders', async () => {
  const reads = [];
  const file = name => ({kind:'file',getFile:async()=>{reads.push(name);return {name};}});
  const tree = {kind:'directory',async *entries(){yield ['slide.png',file('slide.png')];yield ['unused.png',file('unused.png')];yield ['notes.md',file('notes.md')];}};
  const library = new Library();
  library.handle = {queryPermission:async()=> 'granted',async *entries(){yield ['.attachments',tree];}};
  const result = await library.resolve(['slide.png']);
  assert.equal(result.files.length,1);
  assert.equal(result.files[0].path,'slide.png');
  assert.deepEqual(reads,['slide.png']);
  const alias = await library.resolve(['.attachments/slide.png']);
  assert.equal(alias.files[0].path,'.attachments/slide.png');
});
test('supplied images are preferred, and their ambiguity is not hidden by a library', async () => {
  const library = new Library();
  library.index = async()=>{throw new Error('Folder must not be read');};
  const direct = await library.resolve(['slide.png'],[{path:'slide.png',file:{name:'slide.png'}}]);
  assert.equal(direct.files.length,1);
  const duplicate = await library.resolve(['slide.png'],[{path:'a/slide.png',file:{}},{path:'b/slide.png',file:{}}]);
  assert.deepEqual(duplicate.ambiguous,['slide.png']);
  assert.equal(duplicate.files.length,0);
});
test('revoked permission is exposed; upload does not request permission silently', async () => {
  const updates = [];
  const library = new Library(s=>updates.push(s));
  library.name='Screenshots';
  library.handle={queryPermission:async()=> 'prompt',requestPermission:async()=>{throw new Error('Must require a click');},entries(){throw new Error('No permission');}};
  const result=await library.resolve(['slide.png']);
  assert.deepEqual(result.missing,['slide.png']);
  assert.equal(library.state,'permission');
  assert.equal(updates.at(-1).state,'permission');
});
test('the index is refreshed for the next lecture, and unrelated folders are not scanned', async () => {
  const library=new Library();let version=1;
  library.handle={queryPermission:async()=> 'granted',async *entries(){yield [`${version}.png`,{kind:'file',getFile:async()=>({name:`${version}.png`})}];yield ['.git',{kind:'directory',entries(){throw new Error('Must skip .git');}}];}};
  assert.equal((await library.resolve(['1.png'])).files.length,1);
  version=2;
  assert.equal((await library.resolve(['2.png'])).files.length,1);
});

test('a connected native folder is announced and counted even when persistence is blocked', async t => {
  const previousPicker=global.showDirectoryPicker, previousDB=global.indexedDB;
  t.after(()=>{global.showDirectoryPicker=previousPicker;global.indexedDB=previousDB;});
  global.indexedDB={open(){const request={};queueMicrotask(()=>request.onblocked());return request;}};
  const updates=[];
  global.showDirectoryPicker=async()=>({name:'images',queryPermission:async()=> 'granted',async *entries(){yield ['slide.png',{kind:'file'}];yield ['notes.md',{kind:'file'}];}});
  const library=new Library(s=>updates.push(s));
  await library.choose();
  assert.equal(updates[0].name,'images');
  assert.equal(updates[0].state,'ready');
  assert.equal(updates.at(-1).count,1);
  assert.equal(library.state,'session');
});

test('late failed restoration cannot erase the folder just selected by the user', async t => {
  const previousDB=global.indexedDB;const requests=[];
  t.after(()=>{global.indexedDB=previousDB;});
  global.indexedDB={open(){const request={error:new Error('Storage unavailable')};requests.push(request);return request;}};
  const library=new Library();const restoring=library.restore();
  library.useFiles([{path:'images/slide.png',file:{name:'slide.png'}}]);
  for(const request of requests)request.onerror();
  await restoring;
  assert.equal(library.name,'images');
  assert.equal(library.state,'session');
  assert.equal(library.count,1);
  assert.equal((await library.resolve(['slide.png'])).files.length,1);
});
