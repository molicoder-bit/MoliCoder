"""Convert the supplied logo into a Windows icon with multiple resolutions.

Run with the project Python environment; only the existing Qt dependency is used.
The light tile keeps the black logo legible on both light and dark desktops.
"""
from pathlib import Path
import struct

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter


def build_icon(source: Path, destination: Path) -> None:
    logo = QImage(str(source))
    if logo.isNull():
        raise ValueError(f'Cannot load logo: {source}')
    tile = QImage(256, 256, QImage.Format_ARGB32)
    tile.fill(Qt.transparent)
    painter = QPainter(tile)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor('#FFFFFF'))
    painter.drawRoundedRect(QRectF(0, 0, 256, 256), 32, 32)
    scaled = logo.scaled(232, 232, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    painter.drawImage((256 - scaled.width()) // 2, (256 - scaled.height()) // 2, scaled)
    painter.end()
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    entries, images = [], []
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        if not tile.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(buffer, 'PNG'):
            raise RuntimeError('Could not encode icon image')
        payload = bytes(data)
        entries.append(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(payload), offset))
        images.append(payload)
        offset += len(payload)
    destination.write_bytes(struct.pack('<HHH', 0, 1, len(sizes)) + b''.join(entries) + b''.join(images))


if __name__ == '__main__':
    assets = Path(__file__).resolve().parents[1] / 'src/local_model_chat/assets'
    build_icon(assets / 'molicodeLogo.png', assets / 'MoliCoder.ico')
