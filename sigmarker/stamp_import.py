"""PDF-XChange 图章集合追加: 保留名称树、页面、元数据, 验证后备份并原子替换。

集合格式依据厂商说明:
https://forum.pdf-xchange.com/viewtopic.php?t=19555
"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject, TextStringObject
from .sigextract import DEFAULT_OUTPUT_DPI, watermark_pdf_bytes


@dataclass(frozen=True)
class StampImportResult:
    collection_path: Path
    backup_path: Path
    title: str
    page_count: int


def _entries(root):
    try:
        tree = root['/Names']['/Pages']
    except (KeyError, TypeError):
        raise ValueError('所选 PDF 不是带图章登记信息的集合，请选择 Stamps 文件夹中的现有集合。') from None
    pairs = []
    visited = set()

    def walk(node):
        node = node.get_object()
        identity = id(node)
        if identity in visited:
            raise ValueError('图章名称树包含循环或重复节点，未修改文件。')
        visited.add(identity)
        if '/Kids' in node:
            if '/Names' in node:
                raise ValueError('图章名称树结构无效，未修改文件。')
            for child in node['/Kids']:
                walk(child)
        elif '/Names' in node:
            names = node['/Names']
            if len(names) % 2:
                raise ValueError('图章名称树不完整，未修改文件。')
            for key, page in zip(names[::2], names[1::2]):
                if not isinstance(key, TextStringObject) or not isinstance(page, IndirectObject):
                    raise ValueError('图章名称或页面引用无效，未修改文件。')
                pairs.append((key, page))
        else:
            raise ValueError('图章名称树缺少登记信息，未修改文件。')

    walk(tree)
    if len({str(name) for name, _ in pairs}) != len(pairs):
        raise ValueError('图章标识重复，未修改文件。')
    return pairs


def _page_map(reader, pairs):
    pages = {(page.indirect_reference.idnum, page.indirect_reference.generation): i
             for i, page in enumerate(reader.pages)}
    result = {}
    for name, reference in pairs:
        page_index = pages.get((reference.idnum, reference.generation))
        if page_index is None:
            raise ValueError('图章登记指向集合之外的页面，未修改文件。')
        result[str(name)] = page_index
    return result


def append_stamp(rgba_image, collection_path, title, dpi=DEFAULT_OUTPUT_DPI):
    """追加一个独立图章并备份; 实际锁定/并发修改会中止, 不按进程主动拦截。"""
    title = title.strip()
    if not title or len(title) > 160 or any(ord(char) < 32 for char in title):
        raise ValueError('请输入 1 到 160 个字符的图章名称。')
    rgba_image = rgba_image.convert('RGBA')
    if rgba_image.getchannel('A').getbbox() is None:
        raise ValueError('当前签名没有可导入的墨迹。')
    path = Path(collection_path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != '.pdf':
        raise ValueError('请选择现有 PDF 图章集合。')
    original = path.read_bytes()
    reader = PdfReader(BytesIO(original))
    if reader.is_encrypted:
        raise ValueError('暂不支持修改加密的图章集合。')
    root = reader.trailer['/Root']
    # 证书签名文件不能通过重写来追加页面; 动态图章的普通表单则保持原样。
    if '/Perms' in root:
        raise ValueError('该集合带有文档签名或权限登记，未修改文件。')
    fields = reader.get_fields() or {}
    if any(field.get('/FT') == '/Sig' and field.get('/V') for field in fields.values()):
        raise ValueError('不能改写带有证书签名的集合。')
    original_map = _page_map(reader, _entries(root))
    writer = PdfWriter(clone_from=reader)
    pairs = _entries(writer._root_object)

    stamp_reader = PdfReader(BytesIO(watermark_pdf_bytes(rgba_image, dpi=dpi)))
    added = writer.add_page(stamp_reader.pages[0])

    identifier = '#' + uuid4().hex + '=' + title
    pairs.append((TextStringObject(identifier), added.indirect_reference))
    names = ArrayObject()
    for key, reference in sorted(pairs, key=lambda pair: str(pair[0])):
        names.extend([key, reference])
    writer._root_object['/Names'][NameObject('/Pages')] = DictionaryObject({NameObject('/Names'): names})
    result_bytes = BytesIO()
    writer.write(result_bytes)
    writer.close()
    payload = result_bytes.getvalue()

    # 重新打开验证页面映射, 防止 PDF 可读却遗漏图章登记。
    verify = PdfReader(BytesIO(payload))
    verified_map = _page_map(verify, _entries(verify.trailer['/Root']))
    expected_map = {**original_map, identifier: len(reader.pages)}
    if len(verify.pages) != len(reader.pages) + 1 or verified_map != expected_map:
        raise RuntimeError('追加后的图章集合校验失败，原文件未修改。')

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.sigmarker-', suffix='.tmp', delete=False) as temp:
            temporary_path = Path(temp.name)
            temp.write(payload)
            temp.flush()
            os.fsync(temp.fileno())
        if sha256(path.read_bytes()).digest() != sha256(original).digest():
            raise RuntimeError('图章集合刚被其他程序修改，本次未替换原文件，请稍后重试。')
        backup_dir = path.parent / 'SigMarker-backups'
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f'{path.stem}.{datetime.now():%Y%m%d-%H%M%S}.{uuid4().hex[:8]}.pdf.bak'
        with backup.open('xb') as stream:
            stream.write(original)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return StampImportResult(path, backup, title, len(verify.pages))
