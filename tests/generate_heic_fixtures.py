"""可选：用独立开发环境的 pillow-heif==1.8.1 重建合成 HEIC 样本。

不属于测试入口，不给运行依赖增加 HEIC 编码器。
"""

from pathlib import Path

from PIL import Image, ImageDraw


def generate():
    from pillow_heif import register_heif_opener
    register_heif_opener(thumbnails=False)
    image = Image.new('RGB', (160, 96), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, 35, 35), fill='black')
    draw.line([(42, 70), (55, 40), (72, 72), (90, 42), (110, 68)], fill='black', width=4)
    folder = Path(__file__).parents[1] / 'sigmarker' / 'assets'
    folder.mkdir(exist_ok=True)
    image.save(folder / 'synthetic.heic', quality=-1)
    exif = Image.Exif()
    exif[274] = 6
    # 明确传 bytes，让编码器同时写入 HEIF 容器方向，避免仅有描述性 EXIF。
    image.save(folder / 'synthetic-oriented.heic', quality=-1, exif=exif.tobytes())
    Image.new('RGB', (16, 12), 'white').save(
        folder / 'synthetic-primary.heic', quality=-1, save_all=True,
        append_images=[image], primary_index=1)


if __name__ == '__main__':
    generate()
