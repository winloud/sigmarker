from io import BytesIO
import os

import fitz
import numpy as np
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject
import pytest

from sigmarker.sigextract import export_pdf
from sigmarker.stamp_import import append_stamp, _entries, _page_map


def make_collection(path, *, nested=False):
    with fitz.open() as source:
        for i in range(2):
            page = source.new_page(width=120, height=80)
            page.draw_rect(fitz.Rect(10 + i * 20, 15, 40 + i * 20, 60), color=(0, 0, 1))
        reader = PdfReader(BytesIO(source.tobytes()))
    writer = PdfWriter(clone_from=reader)
    writer.metadata = {'/Title': '原有图章集合'}
    children = []
    all_names = ArrayObject()
    for index, page in enumerate(writer.pages):
        key = TextStringObject(f'#old{index}=原有图章{index}')
        names = ArrayObject([key, page.indirect_reference])
        all_names.extend(names)
        children.append(DictionaryObject({NameObject('/Names'): names,
            NameObject('/Limits'): ArrayObject([key, key])}))
    tree = (DictionaryObject({NameObject('/Kids'): ArrayObject(children)}) if nested else
            DictionaryObject({NameObject('/Names'): all_names}))
    writer._root_object[NameObject('/Names')] = DictionaryObject({NameObject('/Pages'): tree,
        NameObject('/Dests'): DictionaryObject({NameObject('/Names'): ArrayObject()})})
    with path.open('wb') as output:
        writer.write(output)
    writer.close()


def signature():
    rgba = Image.new('RGBA', (300, 120))
    ImageDraw.Draw(rgba).line([(25, 80), (65, 20), (90, 90), (135, 35), (230, 70)],
                             fill=(0, 0, 0, 255), width=4)
    return rgba


@pytest.mark.parametrize('nested', [False, True])
def test_append_preserves_pages_names_metadata_and_alpha(tmp_path, nested):
    path = tmp_path / '个人图章.pdf'
    make_collection(path, nested=nested)
    original = path.read_bytes()
    with fitz.open(stream=original, filetype='pdf') as before:
        old_renders = [page.get_pixmap(alpha=True).samples for page in before]
    result = append_stamp(signature(), path, '签名 (姓名)=测试', dpi=600)
    assert result.backup_path.read_bytes() == original
    assert result.page_count == 3
    assert result.backup_path.parent.name == 'SigMarker-backups'
    assert result.backup_path.suffix == '.bak'
    reader = PdfReader(path)
    assert reader.metadata.title == '原有图章集合'
    assert '/Dests' in reader.trailer['/Root']['/Names']
    mapping = _page_map(reader, _entries(reader.trailer['/Root']))
    assert mapping['#old0=原有图章0'] == 0 and mapping['#old1=原有图章1'] == 1
    assert any(key.endswith('=签名 (姓名)=测试') and value == 2 for key, value in mapping.items())
    with fitz.open(path) as after:
        assert old_renders == [after[i].get_pixmap(alpha=True).samples for i in range(2)]
        assert after[2].get_images()[0][1] > 0
        assert after[2].rect.width == pytest.approx(300 / 600 * 72, abs=.001)
        assert after[2].rect.height == pytest.approx(120 / 600 * 72, abs=.001)
        pix = after[2].get_pixmap(dpi=600, alpha=True)
        rgba = np.array(Image.frombytes('RGBA', (pix.width, pix.height), pix.samples))
        assert rgba[0, 0, 3] == 0 and rgba[..., 3].max() > 200
    next_result = append_stamp(signature(), path, '另一枚签名')
    assert next_result.backup_path != result.backup_path
    assert next_result.page_count == 4


def test_rejects_plain_pdf_without_modification(tmp_path):
    path = tmp_path / '普通PDF.pdf'
    export_pdf(signature(), str(path))
    original = path.read_bytes()
    with pytest.raises(ValueError, match='不是带图章登记信息'):
        append_stamp(signature(), path, '签名')
    assert path.read_bytes() == original
    assert not (tmp_path / 'SigMarker-backups').exists()


@pytest.mark.parametrize('title', ['', ' ', '坏\n名称', 'a' * 161])
def test_invalid_title_does_not_modify_collection(tmp_path, title):
    path = tmp_path / '集合.pdf'
    make_collection(path)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        append_stamp(signature(), path, title)
    assert path.read_bytes() == original


def test_failed_replace_preserves_original_and_backup(tmp_path, monkeypatch):
    path = tmp_path / '集合.pdf'
    make_collection(path)
    original = path.read_bytes()

    def fail_replace(*args):
        raise PermissionError('file is in use')

    monkeypatch.setattr('sigmarker.stamp_import.os.replace', fail_replace)
    with pytest.raises(PermissionError):
        append_stamp(signature(), path, '签名')
    assert path.read_bytes() == original
    backups = list((tmp_path / 'SigMarker-backups').glob('*.bak'))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert not list(tmp_path.glob('.sigmarker-*.tmp'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing lock')
def test_real_windows_file_lock_preserves_original(tmp_path):
    import ctypes

    path = tmp_path / '锁定集合.pdf'
    make_collection(path)
    original = path.read_bytes()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    # 允许读取, 禁止替换/删除, 模拟编辑器持有集合的情况。
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        with pytest.raises(PermissionError):
            append_stamp(signature(), path, '测试运行中导入')
        assert path.read_bytes() == original
        backups = list((tmp_path / 'SigMarker-backups').glob('*.bak'))
        assert len(backups) == 1 and backups[0].read_bytes() == original
        assert not list(tmp_path.glob('.sigmarker-*.tmp'))
    finally:
        kernel.CloseHandle(handle)
