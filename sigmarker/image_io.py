"""本地图片与手机照片共用的读取入口，不缩放原始像素。"""

from PIL import Image, ImageOps
from pi_heif import register_heif_opener

register_heif_opener(thumbnails=False)

PHONE_FORMATS = {'JPEG': '.jpg', 'PNG': '.png', 'HEIF': '.heic'}
MAX_PHONE_PIXELS = 60_000_000


def load_image(source, *, phone=False):
    """返回独立 RGB 图像和实际格式；手机限制在解码前检查。"""
    with Image.open(source) as original:
        image_format = original.format
        if phone:
            if image_format not in PHONE_FORMATS:
                raise ValueError('仅支持 JPEG、PNG、HEIC/HEIF 照片。')
            if original.width * original.height > MAX_PHONE_PIXELS:
                raise ValueError('照片超过 6000 万像素，请选择较小的原图。')
        # HEIF 插件处理容器方向并重置 EXIF；其余图片由 Pillow 修正 EXIF。
        oriented = ImageOps.exif_transpose(original)
        rgba = oriented.convert('RGBA')
        result = Image.new('RGB', rgba.size, 'white')
        result.paste(rgba, mask=rgba.getchannel('A'))
        return result, image_format
