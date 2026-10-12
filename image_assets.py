"""OpenCV asset decoding that also works in non-ASCII Windows directories."""
from pathlib import Path

import cv2
import numpy as np


def read_image(path, flags=cv2.IMREAD_COLOR):
    try:
        data = Path(path).read_bytes()
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flags) if data else None
    except (OSError, cv2.error):
        return None
