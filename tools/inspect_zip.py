"""Read-only ZIP inventory; preliminary wiki-link support, not the product parser."""
import argparse
import json
from pathlib import PurePosixPath
import re
import stat
import zipfile


def inspect(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 1000 or sum(i.file_size for i in infos) > 200 * 1024**2:
            raise ValueError('Archive exceeds 1000 entries or 200 MiB uncompressed')
        names = []
        for item in infos:
            name = item.filename
            p = PurePosixPath(name)
            if '\\' in name or p.is_absolute() or '..' in p.parts or re.match(r'^[A-Za-z]:', name):
                raise ValueError('Unsafe archive path')
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('Symlink is not allowed')
            if item.file_size > 30 * 1024**2:
                raise ValueError('Entry exceeds 30 MiB')
            if not item.is_dir():
                names.append(name)
        if len(set(names)) != len(names):
            raise ValueError('Duplicate archive paths')
        notes = [n for n in names if n.lower().endswith('.md')]
        images = [n for n in names if PurePosixPath(n).suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}]
        if len(notes) != 1:
            raise ValueError('Expected exactly one Markdown note')
        note = notes[0]
        text = archive.read(note).decode('utf-8-sig')
        links = re.findall(r'!\[\[([^\]\n]+)\]\]', text)
        used, missing, ambiguous = set(), [], []
        for raw in links:
            target = raw.split('|', 1)[0].split('#', 1)[0]
            local = str(PurePosixPath(note).parent / target)
            matches = [n for n in images if n == local]
            if not matches:
                matches = [n for n in images if n == target or ('/' not in target and PurePosixPath(n).name == target)]
            if len(matches) == 1:
                used.add(matches[0])
            elif not matches:
                missing.append(target)
            else:
                ambiguous.append(target)
        return {'note': note, 'images': len(images), 'wiki_embeds': len(links),
                'missing': missing, 'ambiguous': ambiguous,
                'unused_images': sorted(set(images) - used),
                'limitations': 'Wiki embeds only; ordinary Markdown images and full Markdown parsing are not implemented.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive')
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(args.archive), ensure_ascii=False, indent=2))
    except (ValueError, OSError, zipfile.BadZipFile, UnicodeError, RuntimeError) as exc:
        parser.exit(1, f'Input error: {exc}\n')
