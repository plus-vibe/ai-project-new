/* Read-only attachment library. No file leaves the selected directory boundary. */
(function (root) {
  'use strict';
  const IMAGE = /\.(png|jpe?g|webp)$/i;
  function normalize(target) {
    let value = target.split('|', 1)[0].split('#', 1)[0].trim();
    try { value = decodeURIComponent(value); } catch { return null; }
    if (/^(?:[a-z][a-z0-9+.-]*:|\/)/i.test(value) || value.includes('\\')) return null;
    const parts = value.split('/').filter(p => p && p !== '.');
    if (!parts.length || parts.includes('..') || !IMAGE.test(parts.at(-1))) return null;
    return parts.join('/').normalize('NFC');
  }
  function references(markdown) {
    const found = new Set(); let fence = null;
    for (const line of markdown.split(/\r?\n/)) {
      const marker = line.match(/^\s*(`{3,}|~{3,})/);
      if (marker) { fence = fence === marker[1][0] ? null : fence || marker[1][0]; continue; }
      if (fence) continue;
      for (const match of line.matchAll(/!\[\[([^\]\n]+)\]\]|!\[[^\]\n]*\]\(<([^>\n]+)>\)|!\[[^\]\n]*\]\(([^)\n]+)\)/g)) {
        const target = normalize(match[1] ?? match[2] ?? match[3]); if (target) found.add(target);
      }
    }
    return [...found];
  }
  function match(target, entries, notePath = '') {
    const key = normalize(target); if (!key) return {status:'unsafe', entries:[]};
    notePath = notePath.normalize('NFC');
    const parent = notePath.includes('/') ? notePath.slice(0, notePath.lastIndexOf('/') + 1) : '';
    for (const candidate of [...new Set([parent + key, key])]) {
      const hits = entries.filter(e => e.path.normalize('NFC') === candidate);
      if (hits.length) return {status:hits.length === 1 ? 'found' : 'ambiguous', entries:hits};
    }
    const basename = key.split('/').at(-1);
    const hits = entries.filter(e => e.path.split('/').at(-1).normalize('NFC') === basename);
    return {status:hits.length === 1 ? 'found' : hits.length ? 'ambiguous' : 'missing', entries:hits};
  }
  async function database() {
    return new Promise((resolve,reject) => {
      const request = indexedDB.open('lecture-files', 1);
      let expired = false;
      const timeout = setTimeout(() => { expired = true; reject(new Error('Хранилище папки не отвечает.')); }, 2000);
      request.onupgradeneeded = () => request.result.createObjectStore('settings');
      request.onsuccess = () => { clearTimeout(timeout); if (expired) request.result.close(); else resolve(request.result); };
      request.onerror = () => { clearTimeout(timeout); reject(request.error); };
      request.onblocked = () => { expired = true; clearTimeout(timeout); reject(new Error('Закройте старые вкладки приложения и подключите папку ещё раз.')); };
    });
  }
  async function setting(action, value) {
    const db = await database();
    try { return await new Promise((resolve,reject) => {
      const transaction = db.transaction('settings', action === 'get' ? 'readonly' : 'readwrite');
      const store = transaction.objectStore('settings');
      const request = action === 'get' ? store.get('directory') : action === 'delete' ? store.delete('directory') : store.put(value, 'directory');
      let result; request.onsuccess = () => { result = request.result; };
      transaction.oncomplete = () => resolve(result); transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error || new Error('Настройки недоступны.'));
    }); } finally { db.close(); }
  }
  class Library {
    constructor(onChange = () => {}) { this.onChange = onChange; this.handle = null; this.files = []; this.state = 'empty'; this.name = ''; this.count = null; this.revision = 0; }
    notify() { this.onChange({state:this.state, name:this.name, count:this.count, persistent:!!this.handle}); }
    async restore() {
      const revision = this.revision;
      try {
        const handle = await setting('get');
        const permission = handle && await handle.queryPermission({mode:'read'});
        if (revision !== this.revision) return;
        this.handle = handle;
        if (handle) { this.name = handle.name; this.state = permission === 'granted' ? 'ready' : 'permission'; }
      } catch { if (revision !== this.revision) return; this.state = 'empty'; this.handle = null; }
      if (revision !== this.revision) return;
      this.notify();
    }
    async choose() {
      // Keep the picker/requestPermission in the user gesture, before other awaits.
      if (!root.showDirectoryPicker) return false;
      ++this.revision;
      let handle;
      if (this.handle && this.state === 'permission') {
        if (await this.handle.requestPermission({mode:'read'}) !== 'granted') { this.notify(); return true; }
        handle = this.handle;
      } else { handle = await root.showDirectoryPicker({id:'lecture-screenshots',mode:'read'}); }
      this.handle = handle; this.files = []; this.count = null; this.name = handle.name; this.state = 'ready';
      this.notify(); // Selection must be visible even if IndexedDB is blocked.
      await this.index();
      try { await setting('put', handle); } catch { this.state = 'session'; }
      this.notify(); return true;
    }
    useFiles(files) { ++this.revision; this.handle = null; this.files = files.filter(e => IMAGE.test(e.path)); this.count = this.files.length; this.name = files[0]?.path.split('/')[0] || 'Выбранная папка'; this.state = 'session'; this.notify(); setting('delete').catch(() => {}); }
    async forget() { ++this.revision; this.handle = null; this.files = []; this.count = null; this.name = ''; this.state = 'empty'; this.notify(); await setting('delete').catch(() => {}); }
    async index() {
      if (!this.handle) return this.files;
      if (await this.handle.queryPermission({mode:'read'}) !== 'granted') { this.state = 'permission'; this.notify(); return []; }
      const files = []; let count = 0;
      const walk = async (handle, prefix = '', depth = 0) => {
        if (depth > 20) throw new Error('Слишком глубокая структура. Выберите папку с изображениями.');
        for await (const [name, child] of handle.entries()) {
          if (++count > 20000) throw new Error('В папке слишком много файлов. Выберите папку со скриншотами, а не весь диск.');
          if (child.kind === 'directory' && !['.git','.venv','.obsidian','.trash','.codex','node_modules'].includes(name)) await walk(child, prefix + name + '/', depth + 1);
          else if (child.kind === 'file' && IMAGE.test(name)) files.push({path:prefix + name, handle:child});
        }
      };
      try { await walk(this.handle); }
      catch (error) { if (['NotAllowedError','SecurityError'].includes(error.name)) { this.state='permission'; this.notify(); return []; } throw error; }
      this.count = files.length; this.notify();
      return files;
    }
    async resolve(targets, supplied = [], notePath = '') {
      let index; const files = [], missing = [], ambiguous = [];
      const available = supplied.filter(e => IMAGE.test(e.path));
      for (const target of [...new Set(targets)]) {
        let hit = match(target, available, notePath);
        // Ambiguity in supplied files must not be hidden by a library match.
        if (hit.status === 'missing') { index ??= await this.index(); hit = match(target, index, notePath); }
        if (hit.status === 'found') {
          const item = hit.entries[0];
          files.push({path:normalize(target), file:item.file || await item.handle.getFile()});
        } else if (hit.status === 'ambiguous') ambiguous.push(target);
        else missing.push(target);
      }
      return {files, missing, ambiguous};
    }
  }
  const api = {normalize, references, match, Library};
  root.LectureFiles = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
