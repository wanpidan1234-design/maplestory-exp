"""Entry point for the standalone Windows distribution."""
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile

VERSION = '1.0.0'


def configure_data_directory():
    local = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    os.environ.setdefault('MAPLE_EXP_DATA_DIR', str(local / 'MapleExpTracker' / 'data'))


def self_test(report_path):
    """Exercise bundled DLLs, OCR models, assets and the real UI in isolation."""
    report = {'version': VERSION, 'frozen': bool(getattr(sys, 'frozen', False))}
    try:
        with tempfile.TemporaryDirectory(prefix='maple-exp-check-') as directory:
            os.environ['LOCALAPPDATA'] = directory
            os.environ['MAPLE_EXP_DATA_DIR'] = str(Path(directory) / 'data')
            import tkinter as tk
            import cv2
            import dxcam
            import mss
            import numpy as np
            from PIL import Image, ImageDraw, ImageFont
            from experience_ocr import ExperienceOCR
            from image_assets import read_image
            from game_capture import GameCapture
            from app import App
            from orb import Orb
            from monster_portraits import name_ids

            base = Path(__file__).parent
            for name in ('exp-anchor.png', 'level-anchor.png', 'monster-level-prefix.png'):
                assert read_image(base / 'assets' / name) is not None, name
            assert GameCapture().anchor is not None
            with Image.open(base / 'assets/levelup-d-dynamic/orb-000.png') as image:
                image.load()
            assert name_ids(), 'Missing monster catalog'
            report['assets'] = 'ok'
            image = Image.new('RGB', (360, 70), 'black')
            font = ImageFont.truetype(str(Path(os.environ['WINDIR']) / 'Fonts' / 'arial.ttf'), 42)
            ImageDraw.Draw(image).text((10, 5), '1234567', font=font, fill='white')
            ocr = ExperienceOCR()
            text, confidence = ocr.recognize_exp(np.asarray(image))
            assert text.strip() == '1234567', 'Synthetic OCR recognition failed'
            report['ocr'] = {'text': text, 'confidence': float(confidence)}
            with mss.MSS() as capture:
                assert len(capture.monitors) > 1, 'No display found'
            report['capture_imports'] = 'ok'
            root = tk.Tk()
            root.withdraw()
            backend = App(root)
            companion = Orb(root, backend)
            companion.toggle_panel()
            root.update()
            companion.show_history()
            root.update()
            companion.show_combat()
            root.update()
            companion.show_multiscale()
            root.update()
            assert backend.write_status()
            assert backend.history.path.is_file()
            assert not backend.running, 'Self-test must not record the screen'
            report['ui_and_history'] = 'ok'
            companion.close()
            report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__, error=str(error))
    Path(report_path).write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['status'] == 'passed' else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test-report':
        return self_test(sys.argv[2])
    configure_data_directory()
    # Standalone releases never invoke the local development-review machinery.
    sys.argv.append('--no-startup-review')
    runpy.run_module('orb', run_name='__main__')
    return 0


if __name__ == '__main__':
    sys.exit(main())
